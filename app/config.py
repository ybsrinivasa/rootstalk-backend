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
    # V1.1 share-payment-link: Razorpay sends a webhook signed with
    # this secret. Set in the Razorpay dashboard → Webhooks → Secret.
    razorpay_webhook_secret: str = ""
    razorpay_webhook_secret_test: str = ""
    # 2026-06-24 — Independent Razorpay-mode toggle. Production picks
    # live keys automatically (environment == "production"). Setting
    # RAZORPAY_LIVE_MODE=true on staging/dev forces live keys without
    # turning the whole environment into "production" (so dev_otp,
    # CORS, frontend_base_url defaults, etc. stay staging-flavoured).
    # Use case: testing demos that need a real Razorpay sheet (no
    # "Test Mode" banner) while keeping every other staging affordance.
    razorpay_live_mode: bool = False

    @property
    def _razorpay_use_live(self) -> bool:
        return self.environment == "production" or self.razorpay_live_mode

    @property
    def razorpay_active_key_id(self) -> str:
        return self.razorpay_key_id if self._razorpay_use_live else self.razorpay_key_id_test

    @property
    def razorpay_active_key_secret(self) -> str:
        return self.razorpay_key_secret if self._razorpay_use_live else self.razorpay_key_secret_test

    @property
    def razorpay_active_webhook_secret(self) -> str:
        return (
            self.razorpay_webhook_secret if self._razorpay_use_live
            else self.razorpay_webhook_secret_test
        )

    # FCM
    fcm_server_key: str = ""

    # SMS (Draft4SMS — phone OTP for PWA)
    draft_sms_key: str = ""
    draft_sms_sender_id: str = "EYFARM"
    draft_sms_base_url: str = "https://text.draft4sms.com/vb/apikey.php?"

    # ── Pricing (paise, integer) — env-driven 2026-06-24 ──
    # Defaults match production spec: ₹199 / subscription, ₹20 / expert query.
    # Testing overrides via .env so demos can transact for ₹1. All three live
    # in one place so a single config edit moves all surfaces (Razorpay order
    # amount, pool-formula per-unit, /assignment subscription_price echo).
    subscription_amount_paise: int = 199_00
    query_amount_paise: int = 20_00

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

    # Frontend base URL — used to build CA-facing email links (onboarding
    # magic link, portal login). Same Next.js app serves the SA portal at
    # `/` and per-client portals at `/{short_name}` (path-based routing).
    # In development this defaults to localhost:3004; in non-dev envs it
    # MUST be set explicitly (e.g. https://rstalk.eywa.farm for testing,
    # https://rootstalk.in for production) — startup logs a warning if
    # unset, and `_base_url()` falls back to the production value.
    frontend_base_url: str = ""

    # PWA base URL — used to compose the QR-encoded crop-record URL
    # (`{base}/crop-record/{reference_number}`) printed on share +
    # save buttons in the History page. Defaults to the production
    # host `https://rootstalk.in`; testing MUST set this to
    # `https://rstalk-pwa.eywa.farm` so QR scans don't land on prod.
    # Dev falls back to `http://localhost:3003` (the PWA dev port —
    # see project_rootstalk_ports.md).
    pwa_base_url: str = ""

    # SA portal base URL — used by the Neytiri (SA / CM / RM)
    # welcome email. Production tops both portals on the same host
    # (`rootstalk.eywa.farm` serves SA at `/` and CA at `/{short_name}`),
    # so this can be left empty in production and the welcome
    # email falls back to `frontend_base_url`. Testing topology
    # splits them by subdomain (SA at `rstalk.eywa.farm`, CA at
    # `rstalk-ca.eywa.farm`) — set `SA_PORTAL_URL` explicitly there.
    sa_portal_url: str = ""

    # Environment
    environment: str = "development"

    # 2026-06-24 — Opt-out for the staging dev_otp leak. Default True
    # preserves legacy staging behaviour (dev_otp in response). Setting
    # SURFACE_DEV_OTP=false on the testing .env makes staging behave
    # like production for OTP only — SMS-only, no dev_otp in response —
    # so demos read realistically. Production NEVER surfaces dev_otp
    # regardless of this flag (see _surface_dev_otp).
    surface_dev_otp: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
