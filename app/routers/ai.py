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
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth_deps import CurrentUser, get_current_user, require_owner
from app.services.llm import llm_router, extract_json, LLMUnavailableError
from app.services.retrieval import rank_passages
from app.services.quota import check_and_consume_ai_quota

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
    user_id: str | None = None  # when present, the draft costs 1 AI credit


DRAFT_SYSTEM_PROMPT = """You are a proposal writer for a small government contractor. Draft a \
single proposal section paragraph (150-250 words) for the section named below. Use ONLY the \
company information and past-performance excerpts provided — do not invent contracts, clients, \
dollar values, or capabilities that aren't given to you. If the provided past performance is \
thin or doesn't clearly support the section, say so plainly in the draft rather than padding it \
with generic claims. Write in a confident, plain, specific register — no buzzword filler \
("synergy," "world-class," "cutting-edge"). Respond with ONLY the paragraph text, no preamble."""


@router.post("/draft-section")
async def draft_section(body: DraftSectionRequest):
    # Meter against AI credits when a user is identified. 402 = out of credits,
    # so the client can prompt a refill instead of silently failing.
    remaining_credits = None
    if body.user_id:
        from app.routers.credits import consume_credits
        ok, remaining_credits = consume_credits(body.user_id, 1, reason="proposal_draft")
        if not ok:
            raise HTTPException(status_code=402, detail="Out of AI credits — refill to keep drafting.")

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
        "remaining_credits": remaining_credits,
    }


# ── /read-synthesis ──────────────────────────────────────────────────
# R-E-A-D's six sections (eligibility, requirements, availability,
# deadlines, economics, documentation) used to show the same generic
# guidance copy no matter what solicitation a user was scoring — someone
# could land on the worksheet straight from WARDOG and have no way to
# know what THIS solicitation actually requires without leaving the page
# to re-read it. This endpoint takes the real solicitation text (now
# carried on the proposal row by WARDOG/FASS FILL) and returns one
# grounded synthesis per section in a single call, so each question card
# can show what the text actually says instead of a generic prompt.

READ_CATEGORIES = {
    "eligibility": "Registration & Eligibility — SAM.gov registration status, NAICS code match, and any set-aside qualification required.",
    "requirements": "Experience & Mandatory Requirements — required licenses/certifications/bonds/clearances, and any other pass/fail mandatory qualification.",
    "availability": "Availability & Capacity — staffing, equipment, and bandwidth needed to perform the work as described.",
    "deadlines": "Deadlines & Timing — the response due date, performance start date, and period of performance.",
    "economics": "Economics & Margin — pricing structure, contract value/ceiling if stated, and any cost risk implied by the scope.",
    "documentation": "Documentation & Substantiation — required past performance references, key personnel, and technical approach content.",
}

# The 18 yes/partial/no sub-questions behind R-E-A-D's six categories. The
# frontend owns the canonical labels; these condensed versions exist so the
# model can pre-suggest an answer per sub-question in the same call that does
# the per-section synthesis — turning the worksheet from "fill 18 blanks" into
# "review what's pre-filled." Deterministic answers the app can compute itself
# (naics_match from the two NAICS codes, response_time from the due date) are
# done client-side and OVERRIDE whatever the model suggests here.
READ_SUBQUESTIONS = {
    "sam_active": "SAM.gov registration is active and won't expire before award.",
    "naics_match": "The business's NAICS code matches the solicitation's NAICS.",
    "setaside_qual": "The business meets the set-aside qualification, or none is required.",
    "licenses": "The business holds all required licenses, certifications, bonds, or clearances.",
    "past_perf": "The business can demonstrate relevant past performance.",
    "mandatory_met": "Every other mandatory qualification in the solicitation is met.",
    "staff": "The business can staff this contract within the mobilization window.",
    "equipment": "The business has or can acquire the required equipment/vehicles/supplies.",
    "bandwidth": "Current workload won't prevent full-quality performance.",
    "response_time": "There's enough time to prepare a competitive proposal before the due date.",
    "start_date": "The performance start date is realistic to mobilize for.",
    "period": "The period of performance is manageable.",
    "cost_known": "The scope is clear enough to estimate costs with reasonable confidence.",
    "margin": "At a competitive price, estimated margin is at least 12-15%.",
    "risk": "No unusual cost risks could erase the margin.",
    "references": "At least one strong past-performance reference is available.",
    "personnel": "Key personnel are identified and documentable.",
    "approach": "A specific, credible technical approach can be written (not boilerplate).",
}

READ_SYNTHESIS_SYSTEM_PROMPT = """You are a government contracts analyst helping a small business owner \
quickly understand a specific solicitation AND pre-fill a bid/no-bid scoring worksheet. You are given \
the solicitation's raw text, a few facts about the business, a list of six CATEGORIES, and a list of \
yes/partial/no SUB-QUESTIONS. Do both of the following and return them in ONE JSON object:

1. "synthesis": for EACH category id, a 2-3 sentence synthesis of what THIS solicitation specifically \
says relevant to that category — cite concrete details (specific certs, NAICS, dollar figures, dates, \
named requirements) rather than generic advice. If the text doesn't address a category, say so plainly \
rather than inventing anything.

2. "suggestions": for EACH sub-question id, an object {"answer": "yes"|"partial"|"no"|null, \
"confidence": "high"|"medium"|"low", "rationale": "one short sentence"}.
   - Base answers ONLY on the solicitation text and the business facts you were given.
   - For sub-questions about the business's INTERNAL capabilities (staffing, equipment, licenses held, \
references, key personnel) that the given facts do NOT establish, return "answer": null, confidence \
"low", and a rationale telling the owner what to verify. NEVER invent facts about the business.
   - For naics_match: compare the business's NAICS to the solicitation's (exact = yes, same first 4 \
digits = partial, different = no, either unknown = null).
   - For timing sub-questions, reason from any dates in the text.

Return ONLY the JSON object, no prose before or after, shaped exactly:
{"synthesis": {"eligibility": "...", "requirements": "...", "availability": "...", "deadlines": "...", "economics": "...", "documentation": "..."}, "suggestions": {"sam_active": {"answer": null, "confidence": "low", "rationale": "..."}}}"""


class ReadSynthesisRequest(BaseModel):
    solicitation_text: str
    title: str = ""
    agency: str = ""
    business_naics: str = ""   # the user's own NAICS, to ground naics_match/eligibility
    business_name: str = ""
    user_id: str | None = None  # used only to enforce the Lite plan's quota


@router.post("/read-synthesis")
async def read_synthesis(body: ReadSynthesisRequest, current_user: CurrentUser = Depends(get_current_user)):
    if not body.solicitation_text.strip():
        raise HTTPException(status_code=400, detail="solicitation_text is required")
    if body.user_id:
        require_owner(current_user, body.user_id, detail="You can only use your own account's AI quota")
    check_and_consume_ai_quota(body.user_id)

    categories_block = "\n".join(f"- {cid}: {desc}" for cid, desc in READ_CATEGORIES.items())
    subs_block = "\n".join(f"- {sid}: {desc}" for sid, desc in READ_SUBQUESTIONS.items())
    facts = []
    if body.business_name:
        facts.append(f"Business name: {body.business_name}")
    if body.business_naics:
        facts.append(f"Business NAICS code: {body.business_naics}")
    facts_block = "\n".join(facts) or "(no business profile facts provided)"

    prompt = f"""Solicitation: {body.title or '(untitled)'}{f' — {body.agency}' if body.agency else ''}

Business facts:
{facts_block}

Categories to synthesize:
{categories_block}

Sub-questions to suggest answers for:
{subs_block}

Solicitation text:
{body.solicitation_text[:12000]}"""

    try:
        result = await llm_router.complete(system=READ_SYNTHESIS_SYSTEM_PROMPT, prompt=prompt)
        parsed = extract_json(result.text)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Model returned unparseable output: {e}") from e

    if not isinstance(parsed, dict):
        parsed = {}
    synthesis = parsed.get("synthesis") if isinstance(parsed.get("synthesis"), dict) else {}
    suggestions_raw = parsed.get("suggestions") if isinstance(parsed.get("suggestions"), dict) else {}

    # Only return known category keys — drop anything hallucinated outside
    # the requested set rather than passing it through to the frontend.
    clean_synth = {cid: synthesis.get(cid, "") for cid in READ_CATEGORIES if synthesis.get(cid)}

    # Keep only well-formed suggestions with a real yes/partial/no answer; a
    # null/garbage answer means "the model couldn't tell" — drop it so the
    # frontend leaves that sub-question blank for the user instead of guessing.
    clean_sugg = {}
    for sid in READ_SUBQUESTIONS:
        s = suggestions_raw.get(sid)
        if isinstance(s, dict) and s.get("answer") in ("yes", "partial", "no"):
            conf = s.get("confidence")
            clean_sugg[sid] = {
                "answer": s["answer"],
                "confidence": conf if conf in ("high", "medium", "low") else "low",
                "rationale": str(s.get("rationale") or "")[:300],
            }

    return {
        "synthesis": clean_synth,
        "suggestions": clean_sugg,
        "provider": result.provider,
        "model": result.model,
    }


# ── /cost-breakdown ──────────────────────────────────────────────────
# Show Me The Money's calculator is deliberately deterministic — award
# amount, period of performance, and sub %  are inputs the USER supplies,
# because nobody but the contractor knows their real numbers. What the
# calculator can't do is read the *scope of work* and reason about what
# kind of job it actually is. This endpoint is the judgment-call layer on
# top of that: given the same scope text WARDOG/Inbox/FASS FILL already
# saved on the proposal, produce a rough cost breakdown, a complexity/
# effort read, and risk flags — explicitly framed as a starting estimate
# for the contractor's own pricing process, not a quote.

class CostBreakdownRequest(BaseModel):
    scope_text: str
    title: str = ""
    agency: str = ""
    award_amount: float | None = None
    user_id: str | None = None  # used only to enforce the Lite plan's quota


COST_BREAKDOWN_SYSTEM_PROMPT = """You are helping a small government contractor get a rough, \
first-pass read on a job before they price it themselves. You will be given the scope of work \
(from a solicitation, RFP excerpt, or invitation email) and possibly a known award ceiling. \
Produce a single JSON object, no prose before or after it:

{
  "cost_estimate": {
    "labor_pct": number,        // rough % of total cost that is labor
    "materials_pct": number,    // rough % that is materials/parts/supplies
    "equipment_pct": number,    // rough % that is equipment/tools/rental
    "overhead_profit_pct": number, // rough % that is overhead + profit margin
    "total_low": number or null,   // low end of a rough dollar estimate, if enough scope detail exists to guess
    "total_high": number or null,  // high end of that estimate
    "basis": string             // 1-2 sentences: what assumptions this estimate rests on, and what's missing that would sharpen it
  },
  "complexity": {
    "level": "small" | "medium" | "large",
    "crew_size": string,        // rough headcount/trade mix, e.g. "2-3 technicians, 1 supervisor"
    "estimated_duration": string, // rough timeline to complete the described work, e.g. "2-3 weeks per site visit"
    "rationale": string         // why this level, in 1-2 sentences grounded in the actual scope text
  },
  "risk_flags": [string]        // scope items that are unusually demanding, costly to get wrong, or easy to underbid (e.g. "requires after-hours access", "bonding likely required", "recurring inspection cadence not fully specified")
}

The four cost_estimate percentages should sum to roughly 100. If the scope text doesn't give enough \
detail to produce a dollar range, set total_low and total_high to null rather than guessing wildly — \
say so in "basis" instead. Do not invent contract values, site counts, or requirements not present in \
the text. This is a rough order-of-magnitude read to help a contractor start their own estimate, not \
a bid price. Respond with ONLY the JSON object."""


@router.post("/cost-breakdown")
async def cost_breakdown(body: CostBreakdownRequest, current_user: CurrentUser = Depends(get_current_user)):
    if not body.scope_text.strip():
        raise HTTPException(status_code=400, detail="scope_text is required")
    if body.user_id:
        require_owner(current_user, body.user_id, detail="You can only use your own account's AI quota")
    check_and_consume_ai_quota(body.user_id)

    award_line = f"Known award ceiling: ${body.award_amount:,.0f}" if body.award_amount else "Known award ceiling: not provided"
    prompt = f"""Title: {body.title or '(untitled)'}
Agency: {body.agency or '(not provided)'}
{award_line}

Scope of work text:
{body.scope_text[:12000]}"""

    try:
        result = await llm_router.complete(system=COST_BREAKDOWN_SYSTEM_PROMPT, prompt=prompt, max_tokens=1200)
        fields = extract_json(result.text)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Model returned unparseable output: {e}") from e

    return {
        "cost_estimate": fields.get("cost_estimate", {}),
        "complexity": fields.get("complexity", {}),
        "risk_flags": fields.get("risk_flags") or [],
        "provider": result.provider,
        "model": result.model,
    }


# ── /scope-takeoff ───────────────────────────────────────────────────
# The Estimator's completeness assistant used to key off the proposal
# TITLE with keyword rules — which hallucinates: "Fire & life safety
# INSPECTION, testing and maintenance" matched "fire" and suggested
# fire-rated CONSTRUCTION materials, when an inspection/maintenance
# services contract installs no fire-rated walls at all. This endpoint
# fixes that at the root: it reads the actual solicitation scope text,
# classifies what KIND of job it is, and only proposes materials/
# equipment/consumables consistent with that job type — grounded in the
# text, never invented. The frontend shows the understood scope for the
# user to confirm before anything is added.

class ScopeTakeoffRequest(BaseModel):
    scope_text: str
    title: str = ""
    agency: str = ""
    naics_code: str = ""
    user_id: str | None = None  # used only to enforce the Lite plan's quota


SCOPE_TAKEOFF_SYSTEM_PROMPT = """You are a senior estimator helping a small government \
contractor build a materials takeoff WITHOUT over-ordering things the job doesn't need. You \
will be given a solicitation's scope text. Work in this exact order:

1. CLASSIFY the job type from what the scope actually asks for. One of:
   "inspection", "testing", "maintenance", "repair", "construction", "renovation",
   "installation", "supply", "services", or "mixed".
2. Only AFTER classifying, list supporting/auxiliary materials, equipment, and consumables \
that THIS kind of job genuinely needs. The job type gates everything:
   - inspection / testing: test instruments, tags/labels, forms & documentation, access \
gear, minor consumables — NOT new construction materials.
   - maintenance / repair: replacement parts, consumables, sealants/lubricants, small \
quantities — sized to "as-needed", not full new installs.
   - construction / renovation / installation: the real material line items, fasteners, \
finishes, and the auxiliary items rookies forget (caulk, primer, waste).
   - supply: the deliverable items themselves; little or no labor consumables.

Return a SINGLE JSON object, no prose before or after it:
{
  "job_type": one of the strings above,
  "job_type_reason": string,   // 1 sentence, cite what in the scope tells you this
  "scope_summary": string,     // 2-3 plain sentences: what this job actually involves
  "scope_items": [string],     // the discrete tasks/work the solicitation calls for
  "materials": [
    {
      "name": string,          // specific item
      "category": string,      // e.g. "Test equipment", "Consumables", "Fasteners"
      "why": string,           // why this job needs it, grounded in the scope
      "for_item": string,      // which scope task it supports
      "qty_basis": string      // how to size it, e.g. "1 per device tested", "as-needed"
    }
  ],
  "excluded": [string]         // categories you deliberately did NOT suggest and why, e.g. "No fire-rated construction materials — this is an inspection/maintenance contract, not new construction"
}

Hard rules: Do NOT suggest construction/installation materials for an inspection, testing, or \
maintenance contract unless the scope explicitly calls for installing or replacing built \
components. Do NOT invent quantities, site counts, or requirements not in the text. If the \
scope is too vague to tell, set job_type to "services", keep materials minimal, and say so in \
scope_summary. The "excluded" list is important — it's how the contractor sees what you ruled \
out and why. Respond with ONLY the JSON object."""


@router.post("/scope-takeoff")
async def scope_takeoff(body: ScopeTakeoffRequest, current_user: CurrentUser = Depends(get_current_user)):
    if not body.scope_text.strip():
        raise HTTPException(status_code=400, detail="scope_text is required")
    if body.user_id:
        require_owner(current_user, body.user_id, detail="You can only use your own account's AI quota")
    check_and_consume_ai_quota(body.user_id)

    naics_line = f"NAICS: {body.naics_code}" if body.naics_code else "NAICS: not provided"
    prompt = f"""Title: {body.title or '(untitled)'}
Agency: {body.agency or '(not provided)'}
{naics_line}

Solicitation scope text:
{body.scope_text[:12000]}"""

    try:
        result = await llm_router.complete(
            system=SCOPE_TAKEOFF_SYSTEM_PROMPT, prompt=prompt, max_tokens=1800
        )
        fields = extract_json(result.text)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Model returned unparseable output: {e}") from e

    # Normalize: only keep well-formed material rows, never pass through junk.
    materials = []
    for m in (fields.get("materials") or []):
        if isinstance(m, dict) and m.get("name"):
            materials.append({
                "name": str(m.get("name", "")),
                "category": str(m.get("category", "")),
                "why": str(m.get("why", "")),
                "for_item": str(m.get("for_item", "")),
                "qty_basis": str(m.get("qty_basis", "")),
            })

    return {
        "job_type": fields.get("job_type", "services"),
        "job_type_reason": fields.get("job_type_reason", ""),
        "scope_summary": fields.get("scope_summary", ""),
        "scope_items": [str(s) for s in (fields.get("scope_items") or [])],
        "materials": materials,
        "excluded": [str(s) for s in (fields.get("excluded") or [])],
        "provider": result.provider,
        "model": result.model,
    }


# ── /score-opportunity ───────────────────────────────────────────────
# WARDOG search results and the Opportunity Workspace header used to show
# nothing about a found solicitation beyond what SAM.gov already gives you
# (title, agency, NAICS) — a contractor opening a result had no sense of
# whether it was actually worth their time before doing the full 18-question
# R-E-A-D worksheet by hand. This endpoint is the "first five seconds" read:
# given the resolved solicitation text plus what we already know about the
# business (NAICS, certs, past performance), it produces a real fit score,
# a rough revenue range, a win-probability/competition read, which required
# certifications the text implies, and a short "why this opportunity"
# explanation — grounded in the actual text, same as every other endpoint
# in this file, never invented. R-E-A-D's worksheet remains the deliberate,
# user-verified score; this is the fast triage score that decides whether
# someone opens the worksheet at all.

class ScoreOpportunityRequest(BaseModel):
    solicitation_text: str
    title: str = ""
    agency: str = ""
    solicitation_naics: str = ""
    set_aside: str = ""
    due_date: str = ""
    award_amount: float | None = None
    business_name: str = ""
    business_naics: str = ""
    business_certifications: list[str] = []
    past_performance: list[PastPerformanceItem] = []
    user_id: str | None = None  # used only to enforce the Lite plan's quota


SCORE_OPPORTUNITY_SYSTEM_PROMPT = """You are a government contracts analyst giving a small \
business owner a fast, honest "should I even look at this?" read on a solicitation, BEFORE they \
spend time on a full bid/no-bid worksheet. You are given the solicitation text, basic facts about \
the solicitation (NAICS, set-aside, due date, award ceiling if known), and facts about the \
business (their NAICS, certifications they hold, past performance). Return a SINGLE JSON object, \
no prose before or after it:

{
  "fit_score": number 0-100,        // overall fit: NAICS match, set-aside eligibility, scope match to past performance, certs held vs required
  "fit_label": "strong fit" | "good fit" | "stretch" | "poor fit",
  "win_probability": number 0-100,  // realistic chance of winning if they bid, given competition level and how well-matched they are — NOT the same number as fit_score
  "competition_level": "low" | "medium" | "high",
  "competition_reason": string,     // 1 sentence: what in the text/set-aside/scope suggests this competition level
  "estimated_revenue": {
    "low": number or null,          // rough total contract value range; null if the text/award ceiling gives no basis
    "high": number or null,
    "basis": string                 // what the range is based on (award ceiling stated, scope size, period of performance) or why it's null
  },
  "required_certifications": [string],  // certs/registrations/clearances/bonds the text explicitly or implicitly requires
  "ai_summary": string,             // 2-3 plain sentences: what this contract actually is and whether it's worth pursuing
  "why_bullets": [string],          // 3-5 short bullets, each a CONCRETE reason this is or isn't a good match (cite specific NAICS/cert/past-performance/scope facts, not generic advice)
  "risk_flags": [string]            // things that would hurt this business's odds or effort if they bid (missing cert, thin past performance, tight timeline, unclear scope)
}

Ground every field in the solicitation text and the business facts given — if a business fact \
(certifications, past performance) isn't provided, say so in why_bullets/risk_flags rather than \
assuming the business lacks or has it. Do not invent dollar values, certifications, or NAICS codes \
that aren't in the text. Respond with ONLY the JSON object."""


@router.post("/score-opportunity")
async def score_opportunity(body: ScoreOpportunityRequest, current_user: CurrentUser = Depends(get_current_user)):
    if not body.solicitation_text.strip():
        raise HTTPException(status_code=400, detail="solicitation_text is required")
    if body.user_id:
        require_owner(current_user, body.user_id, detail="You can only use your own account's AI quota")
    check_and_consume_ai_quota(body.user_id)

    pp_lines = [
        f"- {pp.contract or pp.description} ({pp.client}, {pp.period})".strip()
        for pp in body.past_performance
        if pp.contract or pp.description
    ] or ["(none on file)"]

    facts = f"""Solicitation facts:
Title: {body.title or '(untitled)'}
Agency: {body.agency or '(not provided)'}
Solicitation NAICS: {body.solicitation_naics or '(not provided)'}
Set-aside: {body.set_aside or '(none stated)'}
Due date: {body.due_date or '(not provided)'}
Award ceiling: {f"${body.award_amount:,.0f}" if body.award_amount else "(not stated)"}

Business facts:
Name: {body.business_name or '(not provided)'}
NAICS: {body.business_naics or '(not provided)'}
Certifications held: {", ".join(body.business_certifications) if body.business_certifications else "(none on file)"}
Past performance on file:
{chr(10).join(pp_lines)}"""

    prompt = f"""{facts}

Solicitation text:
{body.solicitation_text[:12000]}"""

    try:
        result = await llm_router.complete(
            system=SCORE_OPPORTUNITY_SYSTEM_PROMPT, prompt=prompt, max_tokens=1400
        )
        fields = extract_json(result.text)
    except LLMUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Model returned unparseable output: {e}") from e

    required_certs = [str(c) for c in (fields.get("required_certifications") or [])]
    held = {c.strip().lower() for c in body.business_certifications}
    # Deterministic diff, not left to the model: a cert is a "gap" only if it's
    # required and genuinely absent from the held list (case-insensitive,
    # substring match since solicitations phrase certs inconsistently, e.g.
    # "8(a)" vs "SBA 8(a) certified").
    cert_gaps = [c for c in required_certs if not any(h in c.lower() or c.lower() in h for h in held)]

    revenue = fields.get("estimated_revenue") or {}

    return {
        "fit_score": fields.get("fit_score"),
        "fit_label": fields.get("fit_label", ""),
        "win_probability": fields.get("win_probability"),
        "competition_level": fields.get("competition_level", ""),
        "competition_reason": fields.get("competition_reason", ""),
        "estimated_revenue": {
            "low": revenue.get("low"),
            "high": revenue.get("high"),
            "basis": revenue.get("basis", ""),
        },
        "required_certifications": required_certs,
        "cert_gaps": cert_gaps,
        "ai_summary": fields.get("ai_summary", ""),
        "why_bullets": [str(b) for b in (fields.get("why_bullets") or [])],
        "risk_flags": [str(r) for r in (fields.get("risk_flags") or [])],
        "provider": result.provider,
        "model": result.model,
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
