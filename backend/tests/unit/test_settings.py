from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from ticketing.core.settings import Settings

STRONG = SecretStr("x" * 48)


def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "production",
        "secret_key": STRONG,
        "payment_webhook_secret": STRONG,
        "seed_admin_password": SecretStr("A-strong-admin-password-1"),
        "seed_user_password": SecretStr("A-strong-user-password-1"),
        "seed_viewer_password": SecretStr("A-strong-viewer-password-1"),
        "cookie_secure": True,
        "enable_test_hooks": False,
        "database_url": "postgresql://ticketing_app:a-strong-db-password@db/ticketing",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_development_defaults_are_accepted() -> None:
    assert Settings(app_env="development").hold_ttl_seconds == 600


def test_production_with_safe_values_is_accepted() -> None:
    assert _prod().app_env == "production"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("secret_key", SecretStr("dev-insecure-change-me"), "SECRET_KEY"),
        ("payment_webhook_secret", SecretStr(""), "PAYMENT_WEBHOOK_SECRET"),
        ("seed_admin_password", SecretStr("Admin#Dev2026!"), "SEED_ADMIN_PASSWORD"),
        ("enable_test_hooks", True, "ENABLE_TEST_HOOKS"),
        ("cookie_secure", False, "COOKIE_SECURE"),
        ("database_url", "postgresql://ticketing_app:ticketing_app@db/x", "DATABASE_URL"),
    ],
)
def test_production_rejects_unsafe_values(field: str, value: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _prod(**{field: value})


def test_ticket_limit_cannot_exceed_four() -> None:
    with pytest.raises(ValidationError):
        Settings(max_tickets_per_user_event=5)
