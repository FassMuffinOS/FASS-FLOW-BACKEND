from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supabase
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    # Stripe
    stripe_secret_key: str
    stripe_webhook_secret: str
    stripe_price_starter: str = ""
    stripe_price_pro: str = ""
    stripe_price_team: str = ""

    # Upstash Redis
    upstash_redis_rest_url: str
    upstash_redis_rest_token: str

    # App
    app_env: str = "development"
    frontend_url: str = "http://localhost:5173"
    jwt_secret: str = "change-me"
    admin_secret: str = ""  # required to use /admin/* routes; set in Railway, never commit

    # AI providers — all optional. The LLM router skips any provider
    # whose key is blank and falls through to the next one in order.
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    llm_provider_order: str = "anthropic,openai,gemini"

    class Config:
        env_file = ".env"


settings = Settings()
