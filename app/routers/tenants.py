"""White-label enterprise product — Phase 1: tenant records + admin CRUD.

Scoped per Maurice's 2026-06-30/07-01 direction: one shared Supabase
project, tenant isolation via a `tenant_id` FK (see migrations/
tenants_white_label.sql) rather than a separate database per client — same
pattern already used everywhere else in this app, much faster to spin up a
new client than provisioning real infrastructure per tenant. The framework
is tool-agnostic: `enabled_tools` is a flexible string array, not a schema
change per tool, so any client can be handed any subset of what FASS Flow
already builds. `management_mode` captures the three business models
Maurice wants to offer per client (he doesn't want to pick one globally):
we-stand-it-up-they-run-it, we-run-it-fully-managed, or an enterprise
partner runs the client relationship.

What this IS: the admin portal backend — create a tenant, brand it, turn
on the tools it gets, pick how it's managed. This is the literal
"admin portal where I can create white label spin up" ask.

What this is NOT (yet): actually re-skinning each tool's UI at runtime per
tenant, subdomain routing/DNS, or per-tenant billing. Those are real,
separate follow-on work — deliberately not built here, this is Phase 1.
"""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from app.config import settings
from app.database import get_supabase

router = APIRouter(prefix="/tenants", tags=["tenants"])

# Canonical list of white-label-able tools, mirrored from Dashboard.jsx's
# TOOLS array (GovCon) plus the Regulars/Store product lines. Kept as a
# flat key/label catalog here so the admin UI can render a checklist
# without hardcoding it a second time in the frontend, and so a new tool
# added to the platform later just needs one new entry here.
TOOL_CATALOG = [
    {"key": "wardog", "label": "WARDOG — Opportunity intelligence"},
    {"key": "passport", "label": "Passport — Business ID"},
    {"key": "funding", "label": "Funding — Award calculator"},
    {"key": "glossary", "label": "Glossary"},
    {"key": "read", "label": "R-E-A-D — Bid discipline"},
    {"key": "pipeline", "label": "Pipeline — CRM & tracking"},
    {"key": "fass_fill", "label": "FASS Fill — Execution capacity"},
    {"key": "classroom", "label": "Classroom — Masterclass"},
    {"key": "witness", "label": "Witness — Execute the award"},
    {"key": "estimator", "label": "Estimator"},
    {"key": "foreman", "label": "Foreman — Construction management"},
    {"key": "restoration", "label": "Restoration — Loss & claims documentation"},
    {"key": "camera", "label": "Contractor Camera"},
    {"key": "comms", "label": "Comms Hub"},
    {"key": "affiliates", "label": "Affiliates"},
    {"key": "store", "label": "Store"},
    {"key": "regulars", "label": "Regulars — Wallet/loyalty suite"},
]
VALID_TOOL_KEYS = {t["key"] for t in TOOL_CATALOG}
MANAGEMENT_MODES = {"self_managed", "fass_managed", "partner_managed"}


def _check_admin_secret(x_admin_secret: str | None):
    if not settings.admin_secret:
        raise HTTPException(status_code=503, detail="Admin tools not configured")
    if not x_admin_secret or x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Invalid admin secret")


@router.get("/catalog")
async def get_tool_catalog(x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    return {"tools": TOOL_CATALOG, "management_modes": sorted(MANAGEMENT_MODES)}


class TenantCreate(BaseModel):
    slug: str
    name: str
    custom_domain: str = ""
    logo_url: str = ""
    primary_color: str = ""
    secondary_color: str = ""
    accent_color: str = ""
    enabled_tools: list[str] = []
    management_mode: str = "self_managed"
    partner_name: str = ""
    contact_name: str = ""
    contact_email: str = ""
    notes: str = ""


class TenantUpdate(BaseModel):
    name: str | None = None
    custom_domain: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    enabled_tools: list[str] | None = None
    management_mode: str | None = None
    partner_name: str | None = None
    status: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    notes: str | None = None


def _validate_tools(tools: list[str]):
    unknown = set(tools) - VALID_TOOL_KEYS
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown tool key(s): {', '.join(sorted(unknown))}")


def _validate_mode(mode: str):
    if mode not in MANAGEMENT_MODES:
        raise HTTPException(status_code=400, detail=f"management_mode must be one of {sorted(MANAGEMENT_MODES)}")


@router.get("/admin/list")
async def list_tenants(x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    sb = get_supabase()
    result = sb.table("tenants").select("*").order("created_at", desc=True).execute()
    return {"tenants": result.data}


@router.post("/admin/create")
async def create_tenant(body: TenantCreate, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    _validate_tools(body.enabled_tools)
    _validate_mode(body.management_mode)

    slug = body.slug.strip().lower()
    if not slug or not slug.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="slug must be alphanumeric (hyphens allowed), e.g. 'acme-construction'")

    sb = get_supabase()
    existing = sb.table("tenants").select("id").eq("slug", slug).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail=f"Slug '{slug}' is already in use")

    payload = {
        "slug": slug,
        "name": body.name.strip(),
        "custom_domain": body.custom_domain.strip() or None,
        "logo_url": body.logo_url.strip() or None,
        "primary_color": body.primary_color.strip() or None,
        "secondary_color": body.secondary_color.strip() or None,
        "accent_color": body.accent_color.strip() or None,
        "enabled_tools": body.enabled_tools,
        "management_mode": body.management_mode,
        "partner_name": body.partner_name.strip() or None,
        "contact_name": body.contact_name.strip() or None,
        "contact_email": body.contact_email.strip() or None,
        "notes": body.notes.strip() or None,
    }
    result = sb.table("tenants").insert(payload).execute()
    return {"tenant": result.data[0] if result.data else None}


@router.patch("/admin/{tenant_id}")
async def update_tenant(tenant_id: str, body: TenantUpdate, x_admin_secret: str = Header(None)):
    _check_admin_secret(x_admin_secret)
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "enabled_tools" in updates and updates["enabled_tools"] is not None:
        _validate_tools(updates["enabled_tools"])
    if "management_mode" in updates and updates["management_mode"] is not None:
        _validate_mode(updates["management_mode"])
    if "status" in updates and updates["status"] not in (None, "active", "paused", "archived"):
        raise HTTPException(status_code=400, detail="status must be one of active, paused, archived")
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    sb = get_supabase()
    result = sb.table("tenants").update(updates).eq("id", tenant_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"tenant": result.data[0]}


@router.delete("/admin/{tenant_id}")
async def archive_tenant(tenant_id: str, x_admin_secret: str = Header(None)):
    """Archives rather than hard-deletes — a tenant may have real users
    (profiles.tenant_id) attached; deleting the row out from under them
    would orphan the FK. Archived tenants stop resolving branding but
    keep their history."""
    _check_admin_secret(x_admin_secret)
    sb = get_supabase()
    result = sb.table("tenants").update({"status": "archived"}).eq("id", tenant_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {"tenant": result.data[0]}


@router.get("/resolve")
async def resolve_tenant_branding(slug: str):
    """Public, unauthenticated, deliberately minimal — only branding
    fields a logged-out visitor on a white-labeled login page needs.
    Never returns contact info, notes, or management_mode."""
    sb = get_supabase()
    result = (
        sb.table("tenants")
        .select("slug,name,logo_url,primary_color,secondary_color,accent_color,enabled_tools")
        .eq("slug", slug.strip().lower())
        .eq("status", "active")
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="No active tenant with that slug")
    return result.data[0]
