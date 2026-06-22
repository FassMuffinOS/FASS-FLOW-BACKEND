"""
FASS FILL AI layer.

Product framing matters here as much as the model calls: FASS FILL's core
compliance matrix is built by deterministic regex (see the frontend's
solicitationParser.js) and works with zero API key, zero latency, zero
marginal cost, on every paste. That's deliberate — a contractor shouldn't
need an LLM call just to find a due date in a PDF.

The LLM is layered on *on top* as an opt-in upgrade for the two things
regex genuinely can't do:
  1. Judgment calls regex can't make — flagging ambiguous or missing
     requirements, summarizing intent in plain English, calling out risks
     a first-time bidder would miss. (`/analyze-solicitation`)
  2. Generation grounded in the user's own record — drafting a proposal
     section paragraph using their actual past-performance history, not
     invented experience. (`/draft-section`, RAG via app.services.retrieval)

Deterministic fields (due date, page limit, submission method) keep the
regex value as the source of truth even when the LLM also extracts them —
the LLM's extraction of those fields is only used as a fallback, and is
tagged as such, because a hallucinated date is worse than no date.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.llm import llm_router, extract_json, LLMUnavailableError
from app.services.retrieval import rank_passages

router = APIRouter(prefix="/ai", tags=["ai"])


# ── /analyze-solicitation ────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    raw_text: str
    regex_parsed: dict = {}


ANALYZE_SYSTEM_PROMPT = """You are a government contracts analyst helping a small business \
respond to a solicitation. You will be given raw solicitation text (Section L/M, PWS/SOW, \
or excerpts). Extract the following as a single JSON object, with no prose before or after it:

{
  "due_date": string or null,
  "due_time": string or null,
  "page_limit": number or null,
  "submission_method": string or null,
  "volumes": [{"id": string, "name": string}],
  "required_docs": [string],
  "eval_criteria": [{"name": string, "weight": number, "unit": "%"|"pts"}],
  "plain_summary": string,        // 2-3 plain-English sentences: what is this contract, who's evaluating, what matters most
  "risk_flags": [string],         // things a first-time bidder would likely miss or get wrong (e.g. tight page limits, unusual format rules, missing certs)
  "ambiguities": [string]         // requirements stated unclearly or that the text doesn't fully specify, worth a question to the contracting officer
}

If a field cannot be determined from the text, use null or an empty array. Do not invent dates, \
numbers, or requirements that are not in the text. Respond with ONLY the JSON object."""


def _merge_analysis(regex_parsed: dict, llm_fields: dict) -> dict:
    """Regex wins on deterministic fields when it has a value; LLM fills
    gaps and owns the judgment-call fields regex has no equivalent for."""
    merged = {}
    source = {}

    for field in ("due_date", "due_time", "page_limit", "submission_method"):
        regex_key = {
            "due_date": "dueDate", "due_time": "dueTime",
            "page_limit": "pageLimit", "submission_method": "submissionMethod",
        }[field]
        regex_val = regex_parsed.get(regex_key)
        if regex_val:
            merged[field] = regex_val
            source[field] = "regex"
        else:
            merged[field] = llm_fields.get(field)
            source[field] = "llm" if llm_fields.get(field) else "none"

    # Union list fields, de-duped, tagged by which side found them
    regex_docs = {d.get("label", d) if isinstance(d, dict) else d for d in regex_parsed.get("requiredDocs", [])}
    llm_docs = set(llm_fields.get("required_docs") or [])
    merged["required_docs"] = sorted(regex_docs | llm_docs)
    source["required_docs"] = "both" if (regex_docs and llm_docs) else ("regex" if regex_docs else "llm")

    merged["volumes"] = regex_parsed.get("volumes") or llm_fields.get("volumes") or []
    merged["eval_criteria"] = regex_parsed.get("evalCriteria") or llm_fields.get("eval_criteria") or []

    # LLM-only judgment fields — no regex equivalent exists
    merged["plain_summary"] = llm_fields.get("plain_summary", "")
    merged["risk_flags"] = llm_fields.get("risk_flags") or []
    merged["ambiguities"] = llm_fields.get("ambiguities") or []

    return {"fields": merged, "source": source}


@router.post("/analyze-solicitation")
async def analyze_solicitation(body: AnalyzeRequest):
    if not body.raw_text.strip():
        raise HTTPException(status_code=400, detail="raw_text is required")

    try:
        result = await llm_router.complete(
            system=ANALYZE_SYSTEM_PROMPT,
            prompt=body.raw_text[:12000],  # keep prompts bounded; this isn't a full-document summarizer
        )
        llm_fields = extract_json(result.text)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Model returned unparseable output: {e}") from e

    merged = _merge_analysis(body.regex_parsed, llm_fields)
    return {
        "provider": result.provider,
        "model": result.model,
        **merged,
    }


# ── /draft-section ───────────────────────────────────────────────────

class PastPerformanceItem(BaseModel):
    contract: str = ""
    client: str = ""
    value: str = ""
    period: str = ""
    description: str = ""


class DraftSectionRequest(BaseModel):
    section_name: str
    section_description: str = ""
    solicitation_summary: str = ""
    company_name: str = ""
    core_competencies: str = ""
    differentiators: str = ""
    past_performance: list[PastPerformanceItem] = []


DRAFT_SYSTEM_PROMPT = """You are a proposal writer for a small government contractor. Draft a \
single proposal section paragraph (150-250 words) for the section named below. Use ONLY the \
company information and past-performance excerpts provided — do not invent contracts, clients, \
dollar values, or capabilities that aren't given to you. If the provided past performance is \
thin or doesn't clearly support the section, say so plainly in the draft rather than padding it \
with generic claims. Write in a confident, plain, specific register — no buzzword filler \
("synergy," "world-class," "cutting-edge"). Respond with ONLY the paragraph text, no preamble."""


@router.post("/draft-section")
async def draft_section(body: DraftSectionRequest):
    passages = [
        f"{pp.contract} — {pp.client}. {pp.description} ({pp.period}, {pp.value})".strip()
        for pp in body.past_performance
        if pp.contract or pp.description
    ]
    query = f"{body.section_name}. {body.section_description}. {body.solicitation_summary}"
    grounded = rank_passages(query, passages, top_k=3)

    context_block = "\n".join(f"- {g['text']}" for g in grounded) or "(no clearly relevant past performance on file)"

    prompt = f"""Section to draft: {body.section_name}
What this section needs to cover: {body.section_description or '(not specified)'}
Solicitation context: {body.solicitation_summary or '(not provided)'}

Company: {body.company_name or '(not provided)'}
Core competencies: {body.core_competencies or '(not provided)'}
Differentiators: {body.differentiators or '(not provided)'}

Most relevant past performance on file (use only these, do not invent others):
{context_block}"""

    try:
        result = await llm_router.complete(system=DRAFT_SYSTEM_PROMPT, prompt=prompt)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return {
        "draft": result.text.strip(),
        "provider": result.provider,
        "model": result.model,
        "grounded_in": [{"text": g["text"], "score": round(g["score"], 3)} for g in grounded],
    }


# ── /extract-from-image ──────────────────────────────────────────────
# Continuity feature for WARDOG's "Other Sources" directory: FedConnect,
# Unison Marketplace, and DIBBS sit behind vendor logins, so a server-side
# fetch can't pull their content (and we don't script around vendor auth).
# A screenshot sidesteps that entirely — the student is already looking at
# the page in their own logged-in browser. This transcribes the image back
# into plain text and hands it to the exact same parseSolicitation/AI
# pipeline a regular paste would.

MAX_IMAGES = 6  # generous for a multi-page solicitation excerpt, bounded against abuse/cost

ALLOWED_MEDIA_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


class ImageInput(BaseModel):
    data: str = Field(..., description="Base64-encoded image bytes, no data: URI prefix")
    media_type: str = "image/png"


class ExtractImageRequest(BaseModel):
    images: list[ImageInput]


EXTRACT_IMAGE_SYSTEM_PROMPT = """You are transcribing screenshots of a government contracting \
solicitation (or a portal page listing one) so the text can be processed by a compliance tool. \
Transcribe ALL readable text verbatim, in reading order, preserving section headings, numbering, \
and bullet structure as plain text. Do not summarize, paraphrase, translate, or add commentary of \
your own. If multiple images are provided, transcribe them in order and separate each with a line \
reading "--- next image ---". If a region is blurry, cut off, or illegible, write [illegible] in \
that spot rather than guessing at the content. Respond with ONLY the transcribed text."""


@router.post("/extract-from-image")
async def extract_from_image(body: ExtractImageRequest):
    if not body.images:
        raise HTTPException(status_code=400, detail="At least one image is required")
    if len(body.images) > MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Max {MAX_IMAGES} images per request")
    for img in body.images:
        if img.media_type not in ALLOWED_MEDIA_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported image type: {img.media_type}")

    try:
        result = await llm_router.complete_vision(
            system=EXTRACT_IMAGE_SYSTEM_PROMPT,
            prompt="Transcribe the solicitation text from the image(s) above.",
            images=[{"data": img.data, "media_type": img.media_type} for img in body.images],
        )
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return {
        "raw_text": result.text.strip(),
        "provider": result.provider,
        "model": result.model,
    }
