"""Unified solicitation ingest pipe.

Any input method — the Chrome capture extension, a future email-forward
webhook, or a manual upload — posts the captured solicitation here. Keyed by
BPM ID, this creates or backfills the opportunity (the solicitation_inbox row)
with the real content the portal hides behind a login, and — when that
opportunity already has a Pipeline proposal — backfills the proposal's
description/NAICS so R-E-A-D, FASS FILL, and the Estimator finally have the
actual bid to reason over instead of an email notification.

Auth here is a per-user capture key (a bearer secret the extension stores),
not a Supabase session: the extension runs in the browser on a government
portal, where shipping a refreshable JWT would be awkward and riskier than a
single revocable key scoped to exactly this one write path.
"""
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.database import get_supabase, single_data

router = APIRouter(prefix="/ingest", tags=["ingest"])


def _get_or_create_key(user_id: str) -> str:
    sb = get_supabase()
    row = single_data(
        sb.table("ingest_keys").select("key").eq("user_id", user_id).maybe_single().execute()
    )
    if row and row.get("key"):
        return row["key"]
    key = "fci_" + secrets.token_urlsafe(24)
    sb.table("ingest_keys").upsert({"user_id": user_id, "key": key}).execute()
    return key


def _user_for_key(key: str) -> str | None:
    if not key:
        return None
    sb = get_supabase()
    row = single_data(
        sb.table("ingest_keys").select("user_id").eq("key", key).maybe_single().execute()
    )
    return row.get("user_id") if row else None


# ── Lightweight deterministic field extraction (mirrors solicitationParser.js) ──
def _extract_bpm(text: str) -> str | None:
    m = re.search(r"BPM\s*ID[:#]?\s*(\d{3,})", text, re.I)
    return m.group(1) if m else None


def _extract_naics(text: str) -> str | None:
    m = re.search(r"NAICS(?:\s*code)?[^\d]{0,15}(\d{6})", text, re.I)
    return m.group(1) if m else None


def _extract_set_aside(text: str) -> str | None:
    if re.search(r"8\(a\)", text, re.I):
        return "8(a)"
    if re.search(r"HUBZone", text, re.I):
        return "HUBZone"
    if re.search(r"service[- ]disabled veteran|SDVOSB", text, re.I):
        return "SDVOSB"
    if re.search(r"women?[- ]owned|WOSB|EDWOSB", text, re.I):
        return "WOSB"
    if re.search(r"veteran[- ]owned|VOSB", text, re.I):
        return "VOSB"
    if re.search(r"small business set[- ]aside|total small business|100% small business", text, re.I):
        return "Small Business"
    if re.search(r"full and open|unrestricted", text, re.I):
        return "Full & Open"
    return None


def _extract_title(text: str) -> str | None:
    m = re.search(r"RFx name:\s*([^\n]+)", text, re.I)
    return m.group(1).strip() if m else None


class CapturePayload(BaseModel):
    bpm_id: str | None = None
    title: str | None = None
    source: str = "extension"
    text: str = ""
    link: str | None = None
    naics: str | None = None
    set_aside: str | None = None
    pdf_links: list[str] = []


@router.get("/key")
async def get_key(user_id: str, current_user: CurrentUser = Depends(get_current_user)):
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    require_owner(current_user, user_id, detail="You can only view your own capture key")
    return {"key": _get_or_create_key(user_id)}


@router.post("/solicitation")
async def ingest_solicitation(payload: CapturePayload, x_ingest_key: str = Header(default="")):
    user_id = _user_for_key(x_ingest_key)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or missing capture key")

    text = (payload.text or "").strip()
    bpm = (payload.bpm_id or _extract_bpm(text) or "").strip()
    if not bpm:
        raise HTTPException(
            status_code=422,
            detail="No BPM ID found — can't map this capture to an opportunity. "
            "Capture from the solicitation page that shows the BPM ID.",
        )

    naics = payload.naics or _extract_naics(text)
    set_aside = payload.set_aside or _extract_set_aside(text)
    title = payload.title or _extract_title(text)

    sb = get_supabase()

    existing = single_data(
        sb.table("solicitation_inbox")
        .select("id, proposal_id")
        .eq("user_id", user_id)
        .eq("bpm_id", bpm)
        .maybe_single()
        .execute()
    )

    captured_fields = {
        "captured_text": text[:200000] or None,
        "naics": naics,
        "set_aside": set_aside,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload.link:
        captured_fields["link"] = payload.link

    if existing:
        inbox_id = existing["id"]
        proposal_id = existing.get("proposal_id")
        sb.table("solicitation_inbox").update(captured_fields).eq("id", inbox_id).execute()
    else:
        new_row = {
            "user_id": user_id,
            "source_portal": "eMMA",
            "rfx_name": title or f"Solicitation {bpm}",
            "bpm_id": bpm,
            "status": "new",
            **captured_fields,
        }
        ins = sb.table("solicitation_inbox").insert(new_row).execute()
        inbox_id = ins.data[0]["id"] if ins.data else None
        proposal_id = None

    # When the opportunity already has a Pipeline proposal, backfill it so
    # everything downstream (R-E-A-D synthesis/auto-fill, FASS FILL, Estimator)
    # reads the real solicitation instead of the email notification.
    backfilled_proposal = False
    if proposal_id and text:
        prop_update = {
            "description": text[:200000],
            "solicitation_meta": {"set_aside": set_aside, "naics": naics, "captured": True},
        }
        if naics:
            prop_update["naics_code"] = naics
        sb.table("proposals").update(prop_update).eq("id", proposal_id).execute()
        backfilled_proposal = True

    return {
        "matched": bool(existing),
        "opportunity_id": inbox_id,
        "proposal_id": proposal_id,
        "bpm_id": bpm,
        "naics": naics,
        "set_aside": set_aside,
        "backfilled_proposal": backfilled_proposal,
    }
