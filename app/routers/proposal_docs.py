"""Proposal review persistence — comments + section approvals.

The Proposal Editor's review loop (approve a section, drop a comment on it)
was client-only state that vanished on reload. This persists it against a
`proposal_document`, so a review survives sessions and — because every
comment carries an author_id and the schema is keyed by document, not by a
single user — a BD Partner / Team Up collaborator can be allowed to comment
later (Phase 4) as a permission change, not a migration.

A "document" is identified by (user_id, proposal_id), where proposal_id is a
free-text key: the real opportunity/proposal id when the editor is opened on
one, or 'sample' for the demo doc. /ensure is get-or-create so the editor
can call it on open without worrying whether the row exists yet.

Owner-scoped like the rest of the app (require_owner against the document's
owner); comments/state inherit ownership through their document.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.database import get_supabase, single_data

router = APIRouter(prefix="/proposal-docs", tags=["proposal-docs"])


def _owned_doc(sb, document_id: str, user_id: str) -> dict:
    """Fetch a document and assert the caller owns it. Raises 404/403."""
    doc = single_data(
        sb.table("proposal_documents").select("id, user_id").eq("id", document_id).maybe_single().execute()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if doc["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your document")
    return doc


class EnsureDocRequest(BaseModel):
    user_id: str
    proposal_id: str = "sample"
    title: str | None = None


@router.post("/ensure")
async def ensure_doc(req: EnsureDocRequest, current_user: CurrentUser = Depends(get_current_user)):
    """Get-or-create the document for (user_id, proposal_id). Idempotent —
    the editor calls this on open."""
    require_owner(current_user, req.user_id, detail="You can only open your own documents")
    sb = get_supabase()
    existing = single_data(
        sb.table("proposal_documents")
        .select("*")
        .eq("user_id", req.user_id)
        .eq("proposal_id", req.proposal_id)
        .maybe_single()
        .execute()
    )
    if existing:
        return existing
    row = {"user_id": req.user_id, "proposal_id": req.proposal_id, "title": (req.title or "").strip()[:200] or None}
    return single_data(sb.table("proposal_documents").insert(row).execute())


@router.get("/{document_id}/comments")
async def list_comments(document_id: str, user_id: str,
                         current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="Not your document")
    sb = get_supabase()
    _owned_doc(sb, document_id, user_id)
    comments = (
        sb.table("proposal_comments")
        .select("*, profiles(full_name, company_name)")
        .eq("document_id", document_id)
        .order("created_at")
        .execute()
        .data
        or []
    )
    return {"comments": comments}


class AddCommentRequest(BaseModel):
    user_id: str
    section_key: str
    body: str


@router.post("/{document_id}/comments")
async def add_comment(document_id: str, req: AddCommentRequest,
                       current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, req.user_id, detail="Not your document")
    if not req.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    sb = get_supabase()
    _owned_doc(sb, document_id, req.user_id)
    row = {
        "document_id": document_id,
        "section_key": req.section_key,
        "author_id": req.user_id,
        "body": req.body.strip(),
    }
    return single_data(sb.table("proposal_comments").insert(row).execute())


class UpdateCommentRequest(BaseModel):
    user_id: str
    status: str


@router.patch("/comments/{comment_id}")
async def update_comment(comment_id: str, req: UpdateCommentRequest,
                          current_user: CurrentUser = Depends(get_current_user)):
    """Resolve / reopen a comment thread."""
    require_owner(current_user, req.user_id, detail="Not your comment")
    if req.status not in ("open", "resolved"):
        raise HTTPException(status_code=400, detail="status must be 'open' or 'resolved'")
    sb = get_supabase()
    comment = single_data(
        sb.table("proposal_comments").select("document_id").eq("id", comment_id).maybe_single().execute()
    )
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    _owned_doc(sb, comment["document_id"], req.user_id)
    return single_data(
        sb.table("proposal_comments").update({"status": req.status}).eq("id", comment_id).execute()
    )


@router.delete("/comments/{comment_id}")
async def delete_comment(comment_id: str, user_id: str,
                          current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, user_id, detail="Not your comment")
    sb = get_supabase()
    comment = single_data(
        sb.table("proposal_comments").select("document_id").eq("id", comment_id).maybe_single().execute()
    )
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    _owned_doc(sb, comment["document_id"], user_id)
    sb.table("proposal_comments").delete().eq("id", comment_id).execute()
    return {"status": "deleted"}


@router.get("/{document_id}/state")
async def get_state(document_id: str, user_id: str,
                     current_user: CurrentUser = Depends(get_current_user)):
    """Per-section approval state — backs the review checkmarks + the
    compliance matrix's 'addressed' status across reloads."""
    require_owner(current_user, user_id, detail="Not your document")
    sb = get_supabase()
    _owned_doc(sb, document_id, user_id)
    rows = (
        sb.table("proposal_section_state").select("section_key, approved").eq("document_id", document_id).execute().data
        or []
    )
    return {"sections": rows}


class SetStateRequest(BaseModel):
    user_id: str
    section_key: str
    approved: bool


@router.put("/{document_id}/state")
async def set_state(document_id: str, req: SetStateRequest,
                     current_user: CurrentUser = Depends(get_current_user)):
    require_owner(current_user, req.user_id, detail="Not your document")
    sb = get_supabase()
    _owned_doc(sb, document_id, req.user_id)
    row = {
        "document_id": document_id,
        "section_key": req.section_key,
        "approved": req.approved,
    }
    # Upsert on the (document_id, section_key) primary key.
    sb.table("proposal_section_state").upsert(row, on_conflict="document_id,section_key").execute()
    return {"section_key": req.section_key, "approved": req.approved}
