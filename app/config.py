from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7

    apps_script_url: str = ""
    apps_script_secret: str = ""

    cors_origins: str = "http://localhost:5173"

    # S3-primary / Drive-backup document pipeline -- see s3-drive-sync-plan.md.
    s3_endpoint_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket_name: str = ""
    s3_region: str = "us-east-1"
    sync_worker_poll_seconds: int = 20
    sync_worker_max_retries: int = 5
    sync_worker_stuck_timeout_seconds: int = 300

    # Local OCR fallback (see document_extract.ocr_pdf_bytes) -- runs
    # entirely on this server, no Drive/Apps Script involved. Left blank on
    # Linux (the VPS) once `apt install tesseract-ocr` puts it on PATH;
    # Windows dev machines need the full path since winget doesn't put it on
    # PATH for an already-running shell.
    tesseract_cmd: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
