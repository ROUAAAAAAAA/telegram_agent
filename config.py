
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str
    webhook_url: str = ""

    # ── AI ────────────────────────────────────────────────────────────────────
    openrouter_api_key: str = ""

    # ── Redis (session store) ─────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Session ───────────────────────────────────────────────────────────────
    session_ttl_seconds: int = 3600

    # ── App ───────────────────────────────────────────────────────────────────
    debug: bool = False
    dry_run: bool = False

    # ── Facebook Page ─────────────────────────────────────────────────────────
    facebook_page_id: str = ""
    facebook_page_access_token: str = ""

    # ── LinkedIn ──────────────────────────────────────────────────────────────
    linkedin_access_token: str = ""
    linkedin_person_urn: str = ""

    # ── Firebase (Firestore only) ───────────────────────────────────────────────
    firebase_service_account_path: str = "firebase-service-account.json"

    # ── Website image upload (terrasol.tn) ────────────────────────────────────
    website_upload_url: str = ""   # e.g. https://terrasol.tn/api/upload.php
    website_upload_token: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
