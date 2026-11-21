"""Application settings loaded from environment variables.

Every tunable lives here and is documented in `.env.example`. In production mode the
validator refuses to start with default secrets, demo passwords or test-only switches.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-insecure-change-me"  # noqa: S105 - sentinel compared against, never used in prod
DEV_PROVIDER_TOKEN = "dev-provider-token-change-me"  # noqa: S105 - development sentinel
DEV_PASSWORDS = frozenset({"Admin#Dev2026!", "User#Dev2026!", "Viewer#Dev2026!"})
DEV_DB_PASSWORDS = frozenset({"", "ticketing", "ticketing_app", "postgres", "test"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", frozen=True, hide_input_in_errors=True
    )

    app_env: Literal["development", "test", "production"] = "development"
    service_name: str = "api"

    # --- database / redis -------------------------------------------------------------
    database_url: str = "postgresql://ticketing:ticketing@postgres:5432/ticketing"
    db_pool_min: int = Field(default=2, ge=1)
    db_pool_max: int = Field(default=10, ge=1)
    db_pool_acquire_timeout_s: float = Field(default=1.0, gt=0)
    db_statement_timeout_ms: int = Field(default=2000, gt=0)
    db_lock_timeout_ms: int = Field(default=500, gt=0)
    db_idle_in_tx_timeout_ms: int = Field(default=5000, gt=0)
    redis_url: str = "redis://redis:6379/0"

    # --- security ---------------------------------------------------------------------
    secret_key: SecretStr = SecretStr(DEV_SECRET)
    payment_webhook_secret: SecretStr = SecretStr(DEV_SECRET)
    payment_provider_token: SecretStr = SecretStr(DEV_PROVIDER_TOKEN)
    cookie_secure: bool = False
    allowed_origins: tuple[str, ...] = ("http://localhost:8080", "http://127.0.0.1:8080")
    session_idle_minutes: int = Field(default=30, ge=1)
    session_absolute_hours: int = Field(default=12, ge=1)
    csrf_key_version: int = Field(default=1, ge=1)

    # --- password hashing (argon2id) --------------------------------------------------
    argon2_time_cost: int = Field(default=2, ge=1)
    argon2_memory_kib: int = Field(default=19456, ge=8192)
    argon2_parallelism: int = Field(default=1, ge=1)
    argon2_max_concurrency: int = Field(default=4, ge=1)

    # --- rate limits (Redis fixed windows; all tunable for load tests) -------------------
    rate_limit_login_per_email: int = Field(default=10, ge=1)  # per 15 min
    rate_limit_login_per_ip: int = Field(default=50, ge=1)  # per minute
    rate_limit_register_per_ip: int = Field(default=10, ge=1)  # per hour
    rate_limit_login_global_per_s: int = Field(default=300, ge=1)

    # --- seed accounts ----------------------------------------------------------------
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: SecretStr = SecretStr("Admin#Dev2026!")
    seed_user_email: str = "user@example.com"
    seed_user_password: SecretStr = SecretStr("User#Dev2026!")
    seed_viewer_email: str = "viewer@example.com"
    seed_viewer_password: SecretStr = SecretStr("Viewer#Dev2026!")

    # --- booking ----------------------------------------------------------------------
    hold_ttl_seconds: int = Field(default=600, ge=30)
    max_tickets_per_user_event: int = Field(default=4, ge=1, le=4)

    # --- runtime ----------------------------------------------------------------------
    log_level: str = "INFO"
    log_dir: str | None = (
        None  # when set, JSON logs are also written to <log_dir>/<service>-<boot>.json
    )
    # nginx routes by DNS and ignores readiness, so the default is to stop accepting immediately
    # and let in-flight requests finish within Uvicorn's graceful timeout.
    shutdown_drain_seconds: float = Field(default=0.0, ge=0)
    payment_provider_url: str = "http://paymock:8000"
    # Public base URL of the payment provider's checkout page (through nginx).
    payment_checkout_base_url: str = "http://localhost:8080/pay"

    # --- files -----------------------------------------------------------------------
    import_dir: str = "/data/imports"
    export_dir: str = "/data/exports"
    import_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    import_max_rows: int = Field(default=100_000, ge=1)
    import_validate_timeout_ms: int = Field(default=35_000, gt=0, le=55_000)
    import_finalize_timeout_ms: int = Field(default=45_000, gt=0, le=55_000)
    export_disk_quota_bytes: int = Field(default=2 * 1024**3, ge=1)
    nginx_log_dir: str = "/data/nginx-logs"

    # Test-only switch; production refuses to start if it is enabled.
    enable_test_hooks: bool = False

    @model_validator(mode="after")
    def _security_guards(self) -> Settings:
        provider_token = self.payment_provider_token.get_secret_value()
        if (
            not provider_token.strip()
            or provider_token == self.payment_webhook_secret.get_secret_value()
        ):
            raise ValueError(
                "PAYMENT_PROVIDER_TOKEN must be nonempty and distinct from PAYMENT_WEBHOOK_SECRET"
            )
        if self.app_env != "production":
            return self
        problems: list[str] = []
        if self.secret_key.get_secret_value() in {DEV_SECRET, ""}:
            problems.append("SECRET_KEY must be set to a non-default value")
        if self.payment_webhook_secret.get_secret_value() in {DEV_SECRET, ""}:
            problems.append("PAYMENT_WEBHOOK_SECRET must be set to a non-default value")
        if provider_token in {DEV_PROVIDER_TOKEN, DEV_SECRET}:
            problems.append("PAYMENT_PROVIDER_TOKEN must be set to a non-default value")
        for name in ("seed_admin_password", "seed_user_password", "seed_viewer_password"):
            value: SecretStr = getattr(self, name)
            if value.get_secret_value() in DEV_PASSWORDS:
                problems.append(f"{name.upper()} must not use the development default")
        if self.enable_test_hooks:
            problems.append("ENABLE_TEST_HOOKS must be false in production")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true in production")
        db_password = urlsplit(self.database_url).password or ""
        if db_password in DEV_DB_PASSWORDS:
            problems.append("DATABASE_URL must not use an empty or development password")
        if problems:
            raise ValueError("unsafe production configuration: " + "; ".join(problems))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
