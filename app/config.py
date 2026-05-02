from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str
    database_url_sync: str

    # Redis / Celery
    redis_url: str
    celery_broker_url: str
    celery_result_backend: str

    # Security
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 43200  # 30 days — reduces re-login friction for farmers
    refresh_token_expire_days: int = 7

    # Cosh sync
    cosh_sync_api_key: str = ""

    # AWS S3
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket_name: str = ""
    aws_s3_region: str = "ap-south-1"
    aws_cloudfront_url: str = ""

    # Anthropic (Claude) — image analysis + problem descriptions
    anthropic_api_key: str = ""

    # Google APIs (translate still used; Vision AI replaced by Claude)
    google_translate_api_key: str = ""
    google_vision_api_key: str = ""

    # RazorPay (live + test)
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_key_id_test: str = ""
    razorpay_key_secret_test: str = ""

    @property
    def razorpay_active_key_id(self) -> str:
        return self.razorpay_key_id if self.environment == "production" else self.razorpay_key_id_test

    @property
    def razorpay_active_key_secret(self) -> str:
        return self.razorpay_key_secret if self.environment == "production" else self.razorpay_key_secret_test

    # FCM
    fcm_server_key: str = ""

    # SMS (Draft4SMS — phone OTP for PWA)
    draft_sms_key: str = ""
    draft_sms_sender_id: str = "EYFARM"
    draft_sms_base_url: str = "https://text.draft4sms.com/vb/apikey.php?"

    # Email (for portal user credentials)
    email_smtp_host: str = "smtp.gmail.com"
    email_smtp_port: int = 587
    email_smtp_user: str = ""
    email_smtp_pass: str = ""
    email_from: str = ""

    # Super Admin
    sa_email: str
    sa_password: str

    # CORS
    allowed_origins: str = "http://localhost:3000"

    # Environment
    environment: str = "development"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
