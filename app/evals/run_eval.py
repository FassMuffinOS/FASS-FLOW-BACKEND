"""
Eval harness: regex-only extraction vs. LLM-enhanced extraction.

Run: python -m app.evals.run_eval

This exists because "the LLM seems to work on the demo solicitation" is not
a basis for shipping it. The gold set below is hand-labeled against three
solicitation excerpts with different shapes of missing/awkward formatting
(the kind real SAM.gov postings actually have — inconsistent date phrasing,
missing volume labels, eval criteria stated as prose instead of a table) so
the comparison reflects real failure modes, not the one clean example.

Regex extraction here is a deliberately trimmed Python port of the same
rules in the frontend's solicitationParser.js — just the fields needed for
this comparison — so the two approaches are scored on identical input.

Scoring: exact match for scalar fields (due_date, page_limit,
submission_method); set precision/recall for list fields (required_docs,
eval_criteria names). Requires at least one LLM provider key configured —
without one it still prints the regex-only baseline and explains why the
LLM column is skipped.
"""
import asyncio
import re

from app.services.llm import llm_router, extract_json, LLMUnavailableError
from app.routers.ai import ANALYZE_SYSTEM_PROMPT


# ── Gold-labeled eval set ────────────────────────────────────────────

GOLD_SET = [
    {
        "name": "clean_sf1449",
        "text": """
SOLICITATION SSA-BAL-2026-0142 — Janitorial and Custodial Services

SECTION L — INSTRUCTIONS TO OFFERORS
Proposals are due no later than July 15, 2026, 2:00 PM EST via SAM.gov electronic submission.
The Technical Volume shall not exceed 20 pages, single-spaced, 12-point Times New Roman, 1-inch margins.

Volume I - Technical Approach (15 pages)
Volume II - Past Performance (3 pages)
Volume III - Price Proposal (no limit)

Offerors shall submit: Past Performance References, Key Personnel Resumes, a Safety Plan,
and a current Certificate of Insurance.

SECTION M — EVALUATION FACTORS
Technical Approach - 40%
Past Performance - 30%
Price - 20%
Management Approach - 10%
""",
        "expected": {
            "due_date": "July 15, 2026",
            "page_limit": 20,
            "submission_method": "SAM.gov electronic submission",
            "required_docs": {"Past Performance References", "Key Personnel Resumes", "Safety Plan", "Certificate of Insurance"},
            "eval_criteria": {"Technical Approach", "Past Performance", "Price", "Management Approach"},
        },
    },
    {
        "name": "awkward_phrasing",
        "text": """
RFQ 2026-0098 - IT Support Services, Region 3

All quotes must be received by the contracting office no later than 09/30/2026. Quotes
should be emailed to the contract specialist listed in this notice; no hand deliveries
will be accepted at this time.

The technical narrative is limited to 10 pages total, and shall use Arial font.

Vendors need to have an active SAM.gov registration and should include their most
recent past performance write-up along with a staffing plan covering all proposed labor
categories.

This will be evaluated primarily on price, with technical merit and past performance
considered secondarily as advantages rather than scored line items.
""",
        "expected": {
            "due_date": "09/30/2026",
            "page_limit": 10,
            "submission_method": "Email submission",
            "required_docs": {"Active SAM.gov Registration", "Past Performance References", "Staffing Plan"},
            "eval_criteria": set(),  # stated as prose, not weighted — neither approach should fabricate percentages
        },
    },
    {
        "name": "missing_fields",
        "text": """
PERFORMANCE WORK STATEMENT - Facility Maintenance, Building 4

The contractor shall provide routine maintenance, HVAC servicing, and grounds upkeep
for the facility described in Attachment A. Work shall be performed during normal
business hours unless otherwise coordinated with the COR.

Offerors are reminded that all submissions require a valid CAGE code and current
business license. A capability statement should accompany the technical proposal.
""",
        "expected": {
            "due_date": None,
            "page_limit": None,
            "submission_method": None,
            "required_docs": {"Business License / Certifications", "Capability Statement"},
            "eval_criteria": set(),
        },
    },
]


# ── Trimmed Python port of solicitationParser.js (regex baseline) ───

DOC_PATTERNS = [
    ("Past Performance References", re.compile(r"past[- ]performance", re.I)),
    ("Key Personnel Resumes", re.compile(r"resumes?\b|key personnel", re.I)),
    ("Staffing Plan", re.compile(r"staffing plan", re.I)),
    ("Safety Plan", re.compile(r"safety plan", re.I)),
    ("Certificate of Insurance", re.compile(r"certificate of insurance|insurance certificate", re.I)),
    ("Active SAM.gov Registration", re.compile(r"SAM\.gov registration|active registration in SAM", re.I)),
    ("Business License / Certifications", re.compile(r"business license|professional license", re.I)),
    ("Capability Statement", re.compile(r"capability statement", re.I)),
]


def regex_extract(text: str) -> dict:
    # Faithful port of the due-date pattern in solicitationParser.js — deliberately
    # NOT extended with extra phrasing, so this eval measures the regex the product
    # actually ships, not a strengthened version that would flatter the comparison.
    due_date_match = re.search(
        r"(?:due (?:no later than|by)|deadline(?:\s+is)?|proposals?\s+(?:are|is)\s+due)[^\n.]{0,60}?"
        r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
        text, re.I,
    )
    page_limit_match = re.search(
        r"(?:shall not exceed|not to exceed|limited to|no more than|maximum of)\s*(\d{1,3})\s*(?:single[- ]spaced\s*)?pages?",
        text, re.I,
    )

    submission_method = None
    if re.search(r"sam\.gov", text, re.I):
        submission_method = "SAM.gov electronic submission"
    elif re.search(r"email|e-mail", text, re.I) and re.search(r"submit|emailed", text, re.I):
        submission_method = "Email submission"
    elif re.search(r"hand[- ]deliver", text, re.I):
        submission_method = "Hand delivery"

    required_docs = {label for label, pattern in DOC_PATTERNS if pattern.search(text)}

    eval_criteria = {
        m.group(1).strip()
        for m in re.finditer(r"([A-Z][A-Za-z\s/&-]{2,40}?)\s*[-–:(]\s*(\d{1,3})\s*(%|points?)", text)
    }

    return {
        "due_date": due_date_match.group(1) if due_date_match else None,
        "page_limit": int(page_limit_match.group(1)) if page_limit_match else None,
        "submission_method": submission_method,
        "required_docs": required_docs,
        "eval_criteria": eval_criteria,
    }


# ── Scoring ───────────────────────────────────────────────────────────

def score_scalar(predicted, expected) -> float:
    return 1.0 if predicted == expected else 0.0


def score_set(predicted: set, expected: set) -> tuple[float, float]:
    if not expected:
        return (1.0 if not predicted else 0.0), 1.0  # nothing to find; penalize fabrication, recall trivially perfect
    if not predicted:
        return 0.0, 0.0
    tp = len(predicted & expected)
    precision = tp / len(predicted)
    recall = tp / len(expected)
    return precision, recall


async def run():
    print(f"{'case':<20} {'field':<18} {'regex':>8} {'llm':>8}")
    print("-" * 58)

    has_llm = bool(llm_router.available_providers())
    if not has_llm:
        print("(no LLM provider key configured — showing regex baseline only)\n")

    regex_totals, llm_totals, n = {}, {}, 0

    for case in GOLD_SET:
        text = case["text"]
        expected = case["expected"]
        regex_result = regex_extract(text)

        llm_fields = {}
        if has_llm:
            try:
                result = await llm_router.complete(system=ANALYZE_SYSTEM_PROMPT, prompt=text)
                llm_fields = extract_json(result.text)
            except (LLMUnavailableError, ValueError) as e:
                print(f"  [{case['name']}] LLM call failed: {e}")

        rows = []
        for field in ("due_date", "page_limit", "submission_method"):
            r_score = score_scalar(regex_result[field], expected[field])
            l_score = score_scalar(llm_fields.get(field), expected[field]) if has_llm else None
            rows.append((field, r_score, l_score))

        for field, llm_key in (("required_docs", "required_docs"), ("eval_criteria", "eval_criteria")):
            r_p, r_r = score_set(regex_result[field], expected[field])
            r_f1 = 0.0 if (r_p + r_r) == 0 else 2 * r_p * r_r / (r_p + r_r)
            l_f1 = None
            if has_llm:
                llm_set = set(llm_fields.get(llm_key) or []) if llm_key == "required_docs" else \
                    {c.get("name", "") for c in (llm_fields.get(llm_key) or [])}
                l_p, l_r = score_set(llm_set, expected[field])
                l_f1 = 0.0 if (l_p + l_r) == 0 else 2 * l_p * l_r / (l_p + l_r)
            rows.append((field, r_f1, l_f1))

        for field, r_score, l_score in rows:
            n_key = field
            regex_totals[n_key] = regex_totals.get(n_key, 0) + r_score
            if l_score is not None:
                llm_totals[n_key] = llm_totals.get(n_key, 0) + l_score
            print(f"{case['name']:<20} {field:<18} {r_score:>8.2f} {'' if l_score is None else f'{l_score:>8.2f}'}")
        n += 1

    print("-" * 58)
    for field in regex_totals:
        r_avg = regex_totals[field] / n
        line = f"{'AVERAGE':<20} {field:<18} {r_avg:>8.2f}"
        if has_llm and field in llm_totals:
            line += f"{llm_totals[field] / n:>8.2f}"
        print(line)


if __name__ == "__main__":
    asyncio.run(run())
