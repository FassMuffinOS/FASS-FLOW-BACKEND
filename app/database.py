from supabase import create_client, Client
from app.config import settings

_supabase: Client | None = None


def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        _supabase = create_client(settings.supabase_url, settings.supabase_service_role_key)
    return _supabase


def single_data(execute_result):
    """Safely pull .data off a .maybe_single().execute() result.

    In this supabase-py version, .maybe_single().execute() returns None
    outright (not a response object with .data = None) when zero rows
    match the filter — so `.execute().data` chained directly crashes with
    AttributeError: 'NoneType' object has no attribute 'data' the moment a
    lookup legitimately finds nothing (e.g. a brand-new user with no row
    yet). Always route maybe_single() results through this helper instead
    of chaining .data straight onto .execute().
    """
    return execute_result.data if execute_result is not None else None
