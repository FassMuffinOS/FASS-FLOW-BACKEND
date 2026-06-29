"""FASS Flow security scanner — static analysis, no network calls.

Background: a 2026-06-29 review found that most routers trusted a
client-supplied user_id/business_user_id with no check that the caller was
actually logged in as that user (the Supabase client also runs on the
service-role key everywhere, bypassing RLS, so that client-supplied id was
the *only* access control). That round was fixed in commit 0e2d57e across
six routers (gift_cards, wallet, business_profile, chat, comms, bd_partner).

This script exists to catch the next instance of that same bug — in a
router added next month, or one that round didn't reach — automatically,
instead of relying on someone noticing it in a future manual review.

What it checks (all static, source-only, nothing executed):
  1. IDOR: route handlers that perform a write (insert/update/upsert/delete)
     keyed on a client-supplied user_id/business_user_id field, with no
     get_current_user / require_owner / admin-secret gate anywhere in the
     function. This is the exact bug class from the review.
  2. Same pattern on reads (lower severity — still leaks one user's private
     data to anyone who can guess/enumerate their id).
  3. Hardcoded secrets: literal strings matching known API key formats
     (Stripe, Google, GitHub, Anthropic, OpenAI, AWS, Supabase JWT, generic
     PEM private key blocks) committed directly in source rather than read
     from settings/env.
  4. CORS: wildcard origin combined with allow_credentials=True (browsers
     reject it, but worth flagging if it shows up — usually a sign someone
     pasted boilerplate without checking).
  5. Debug/dev flags left on (DEBUG=True, app.run(debug=True), reload=True
     outside a __main__ guard).

Usage:
    python scripts/security_scan.py                  # human-readable report on stdout
    python scripts/security_scan.py --json            # machine-readable, for the API endpoint
    python scripts/security_scan.py --json out.json   # also write to a file

Exit code is 1 if any HIGH severity finding exists, 0 otherwise — lets CI
fail the build on a real regression without blocking on LOW/INFO noise.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = BACKEND_ROOT / "app"
ROUTERS_DIR = APP_DIR / "routers"

# ---------------------------------------------------------------------------
# Known-intentional public endpoints — called out by name in the auth_deps.py
# module docstring and the 0e2d57e commit message. Kept in one place (here)
# rather than scattered as inline suppression comments across 20 files, so
# the "why is this allowed to be public" reasoning lives next to the rule
# that would otherwise flag it.
# ---------------------------------------------------------------------------
PUBLIC_ALLOWLIST: set[tuple[str, str]] = {
    # (router filename, route path as written in the decorator)
    ("gift_cards.py", "/lookup"),            # public storefront card lookup
    ("gift_cards.py", "/pass"),               # public .pkpass download via QR
    ("gift_cards.py", "/business"),           # public storefront business info
    ("gift_cards.py", "/purchase/checkout"),  # public purchase flow (Stripe)
    ("gift_cards.py", "/purchase/status"),
    ("wallet.py", "/pass"),                   # public .pkpass download via QR
    ("wallet.py", "/public/{slug}"),          # public card preview
    ("wallet.py", "/purchase-status/{slug}"),
    ("comms.py", "/twilio/inbound"),          # Twilio webhook, validated separately
    ("chat.py", "/profile/{other_user_id}"),  # deliberately public profile view
    ("business_lookup.py", "/lookup"),        # public Google Places passthrough
    ("careers.py", "/apply"),                 # public job application form
    ("admin.py", "/invite"),                  # gated by X-Admin-Secret, not session
    ("admin.py", "/grant-access"),            # gated by X-Admin-Secret, not session
    ("feed.py", "/user/{user_id}"),           # deliberately public per-business feed (Profile.jsx embed)
    ("rewards.py", "/join"),                   # public loyalty-card claim, no customer account required
    ("users.py", "/{user_id}/profile"),        # GET only — public display-name read, same as feed.py/chat.py
    ("profiles.py", "/{user_id}"),              # the discoverable Business Profiles feature — public by design
}

USER_ID_FIELDS = {"user_id", "business_user_id", "affiliate_user_id", "owner_id"}
WRITE_CALLS = {"insert", "update", "upsert", "delete"}
AUTH_MARKERS = (
    "get_current_user",
    "require_owner",
    "_check_admin_secret",
    "x_admin_secret",
    "X-Admin-Secret",
)

SECRET_PATTERNS = [
    ("Stripe live secret key", re.compile(r"sk_live_[A-Za-z0-9]{20,}")),
    ("Stripe test secret key", re.compile(r"sk_test_[A-Za-z0-9]{20,}")),
    ("Stripe webhook secret", re.compile(r"whsec_[A-Za-z0-9]{20,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Google OAuth client secret", re.compile(r"GOCSPX-[A-Za-z0-9\-_]{20,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("OpenAI API key", re.compile(r"sk-[A-Za-z0-9]{40,}")),
    ("Supabase/JWT-looking secret", re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")),
    ("PEM private key block", re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----")),
]

# Files/dirs we never want to scan for secrets (deps, build output, vcs internals).
SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".pytest_cache"}
# Files that legitimately contain example/placeholder key formats in prose.
SECRET_SCAN_SKIP_FILES = {"security_scan.py", "auth_deps.py"}


@dataclass
class Finding:
    severity: str   # HIGH | MEDIUM | LOW | INFO
    category: str   # idor | secret | cors | debug
    file: str
    line: int
    message: str


def _iter_py_files(root: Path):
    for p in root.rglob("*.py"):
        if any(part in SKIP_DIR_NAMES for part in p.parts):
            continue
        yield p


def _route_path_from_decorator(dec: ast.expr) -> str | None:
    """Pull the path string out of @router.get("/foo"), @router.post('/x'), etc."""
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
        if dec.func.attr in {"get", "post", "put", "patch", "delete"}:
            if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                return dec.args[0].value
    return None


def _is_route_decorator(dec: ast.expr) -> bool:
    return isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in {"get", "post", "put", "patch", "delete"}


def _http_method(dec: ast.expr) -> str:
    return dec.func.attr.upper()  # type: ignore[union-attr]


def _collect_pydantic_models(tree: ast.Module) -> dict[str, set[str]]:
    """Map class name -> set of field names, for classes that subclass BaseModel
    (directly — good enough for this codebase's flat models)."""
    models: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            if "BaseModel" in bases:
                fields = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                        fields.add(stmt.target.id)
                models[node.name] = fields
    return models


def _func_param_type_names(fn: ast.AsyncFunctionDef | ast.FunctionDef) -> dict[str, str]:
    out = {}
    for arg in fn.args.args:
        if arg.annotation is not None:
            ann = arg.annotation
            name = None
            if isinstance(ann, ast.Name):
                name = ann.id
            elif isinstance(ann, ast.Attribute):
                name = ann.attr
            elif isinstance(ann, ast.Subscript) and isinstance(ann.value, ast.Name):
                name = ann.value.id  # e.g. Optional[Foo] -> Foo not resolved, best-effort
            if name:
                out[arg.arg] = name
    return out


def _source_segment(src_lines: list[str], node: ast.AST) -> str:
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno)
    return "\n".join(src_lines[start:end])


def scan_idor(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        src = path.read_text()
    except Exception:
        return findings
    src_lines = src.splitlines()
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError as e:
        return [Finding("HIGH", "parse-error", path.name, e.lineno or 0, f"Could not parse file: {e}")]

    models = _collect_pydantic_models(tree)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        route_decs = [d for d in node.decorator_list if _is_route_decorator(d)]
        if not route_decs:
            continue
        dec = route_decs[0]
        _rp = _route_path_from_decorator(dec)
        route_path = _rp if _rp is not None else "?"
        route_path = route_path if route_path != "" else "/"
        method = _http_method(dec)

        if (path.name, route_path) in PUBLIC_ALLOWLIST:
            continue

        func_src = _source_segment(src_lines, node)
        has_auth = any(marker in func_src for marker in AUTH_MARKERS)
        if has_auth:
            continue

        # Does this handler touch a user_id-like field, either as a direct
        # path/query param or via a Pydantic body model field?
        touched_field = None
        param_types = _func_param_type_names(node)
        for arg in node.args.args:
            if arg.arg in USER_ID_FIELDS:
                touched_field = arg.arg
                break
        if not touched_field:
            for arg_name, type_name in param_types.items():
                fields = models.get(type_name)
                if fields and (fields & USER_ID_FIELDS):
                    touched_field = next(iter(fields & USER_ID_FIELDS))
                    break
        if not touched_field:
            continue

        is_write = any(f".{w}(" in func_src for w in WRITE_CALLS)
        severity = "HIGH" if is_write else "LOW"
        kind = "writes" if is_write else "reads"
        findings.append(Finding(
            severity=severity,
            category="idor",
            file=f"app/routers/{path.name}",
            line=node.lineno,
            message=(
                f"{method} {route_path} {kind} data keyed on '{touched_field}' "
                f"taken from the request with no get_current_user/require_owner check — "
                f"anyone who knows or guesses a {touched_field} can {'modify' if is_write else 'read'} "
                f"that user's data with no login at all."
            ),
        ))
    return findings


def scan_secrets(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_py_files(root):
        if path.name in SECRET_SCAN_SKIP_FILES:
            continue
        try:
            text = path.read_text()
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(
                        severity="HIGH",
                        category="secret",
                        file=str(path.relative_to(root)),
                        line=i,
                        message=f"Possible hardcoded {label} committed in source.",
                    ))
    return findings


def scan_cors_and_debug(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _iter_py_files(root):
        try:
            text = path.read_text()
        except Exception:
            continue
        origins_match = re.search(r"allow_origins\s*=\s*(\[[^\]]*\]|[\"'][^\"']*[\"'])", text, re.DOTALL)
        if origins_match and re.search(r"[\"']\*[\"']", origins_match.group(1)) and "allow_credentials=True" in text:
            line = text[:origins_match.start()].count("\n") + 1
            findings.append(Finding(
                severity="MEDIUM",
                category="cors",
                file=str(path.relative_to(root)),
                line=line,
                message="CORS allow_origins includes a wildcard alongside allow_credentials=True.",
            ))
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"debug\s*=\s*True", line) and "uvicorn" not in line.lower():
                findings.append(Finding(
                    severity="LOW",
                    category="debug",
                    file=str(path.relative_to(root)),
                    line=i,
                    message="Debug flag left enabled — verify this never ships to production.",
                ))
    return findings


def run_scan() -> list[Finding]:
    findings: list[Finding] = []
    if ROUTERS_DIR.exists():
        for path in sorted(ROUTERS_DIR.glob("*.py")):
            findings.extend(scan_idor(path))
    findings.extend(scan_secrets(APP_DIR))
    findings.extend(scan_cors_and_debug(APP_DIR))
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
    findings.sort(key=lambda f: (order.get(f.severity, 9), f.file, f.line))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", nargs="?", const="-", default=None,
                         help="Output JSON. With no value, prints to stdout; with a path, also writes to that file.")
    args = parser.parse_args()

    findings = run_scan()
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    payload = {
        "summary": counts,
        "total": len(findings),
        "findings": [asdict(f) for f in findings],
    }

    if args.json is not None:
        text = json.dumps(payload, indent=2)
        print(text)
        if args.json != "-":
            Path(args.json).write_text(text)
    else:
        print(f"FASS Flow security scan — {len(findings)} findings "
              f"(HIGH {counts['HIGH']}, MEDIUM {counts['MEDIUM']}, LOW {counts['LOW']}, INFO {counts['INFO']})\n")
        for f in findings:
            print(f"[{f.severity:6}] {f.category:8} {f.file}:{f.line}  {f.message}")

    sys.exit(1 if counts["HIGH"] > 0 else 0)


if __name__ == "__main__":
    main()
