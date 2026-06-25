from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    # Stripe
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_lite: str = ""    # $9.99/mo entry tier — read-only WARDOG + 1 AI synthesis/cycle
    stripe_price_starter: str = ""
    stripe_price_pro: str = ""
    stripe_price_team: str = ""
    stripe_price_wallet: str = ""  # one-time price for a real FASS Wallet .pkpass unlock

    # Upstash Redis
    upstash_redis_rest_url: str
    upstash_redis_rest_token: str

    # App
    app_env: str = "development"
    frontend_url: str = "http://localhost:5173"
    jwt_secret: str = "change-me"
    admin_secret: str = ""  # required to use /admin/* routes; set in Railway, never commit
    sam_gov_api_key: str = ""  # SAM.gov opportunities API key; set in Railway, never commit
    google_places_api_key: str = ""  # Google Places API (New); set in Railway, never commit

    # Apple Wallet (.pkpass signing) — all base64-encoded PEM blobs, set in
    # Railway, never committed. Blank-default: wallet router returns 503 and
    # the frontend falls back to the free preview-only card until these are set.
    apple_pass_cert_pem_b64: str = ""   # Pass Type ID certificate (PEM, base64)
    apple_pass_key_pem_b64: str = ""    # matching private key (PEM, base64, unencrypted)
    apple_wwdr_pem_b64: str = ""        # Apple WWDR intermediate cert (PEM, base64)
    apple_pass_key_password: str = ""   # only needed if the key above is password-protected
    apple_team_id: str = ""             # 10-char Apple Developer Team ID
    apple_pass_type_id: str = ""        # e.g. pass.systems.fass.wallet

    # AI providers — all optional. The LLM router skips any provider
    # whose key is blank and falls through to the next one in order.
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    deep_seek_api_key: str = ""  # field name matches Railway's DEEP_SEEK_API_KEY env var
    llm_provider_order: str = "anthropic,deepseek,openai,gemini"

    class Config:
        env_file = ".env"


settings = Settings()
