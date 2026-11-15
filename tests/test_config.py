"""The URL rewriting is load-bearing: get it wrong and the app cannot connect
at all, or connects without TLS. Pin the behaviour."""

from __future__ import annotations

from app.core.config import normalize_asyncpg_url, settings

NEON_POOLED = (
    "postgresql://user:pw@ep-example-000000-pooler.c-5.us-east-2.aws.neon.tech"
    "/neondb?sslmode=require&channel_binding=require"
)


def test_driver_is_rewritten_to_asyncpg() -> None:
    assert normalize_asyncpg_url(NEON_POOLED).drivername == "postgresql+asyncpg"


def test_libpq_only_params_are_stripped() -> None:
    url = normalize_asyncpg_url(NEON_POOLED)
    assert "sslmode" not in url.query
    assert "channel_binding" not in url.query


def test_credentials_and_database_survive_rewriting() -> None:
    url = normalize_asyncpg_url(NEON_POOLED)
    assert url.username == "user"
    assert url.password == "pw"
    assert url.database == "neondb"
    assert url.host is not None and url.host.endswith("neon.tech")


def test_configured_urls_point_at_the_right_endpoints() -> None:
    """Pooled for the app, unpooled for migrations (CLAUDE.md §5)."""
    app_host = settings.async_database_url.host or ""
    migration_host = settings.async_direct_url.host or ""

    if "neon.tech" in app_host:
        assert "-pooler." in app_host, "DATABASE_URL must be Neon's pooled endpoint"
        assert "-pooler." not in migration_host, "DIRECT_URL must be the unpooled endpoint"
        assert settings.is_pooled_connection is True
        assert settings.requires_tls is True


def test_local_postgres_does_not_require_tls() -> None:
    from app.core.config import Settings

    local = Settings(
        DATABASE_URL="postgresql://u:p@postgres:5432/hospital",
        DIRECT_URL="postgresql://u:p@postgres:5432/hospital",
    )
    assert local.requires_tls is False
    assert local.is_pooled_connection is False


def _production(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "ENVIRONMENT": "production",
        "DEBUG": False,
        "SECRET_KEY": "x" * 48,
        "MAIL_BACKEND": "smtp",
        "SMTP_USERNAME": "mailer@example.com",
        "SMTP_PASSWORD": "app-password",
        "APP_PUBLIC_URL": "https://app.example.com",
        "UPI_VPA": "nirogya@okaxis",
        "OTP_HMAC_SECRET": "y" * 48,
    }
    values.update(overrides)
    return values


def test_production_requires_upi_vpa_and_otp_secret() -> None:
    """A payment page with no UPI ID, or OTPs hashed with a guessable key,
    must stop the deploy rather than reach a paying hospital."""
    import pytest
    from pydantic import ValidationError

    from app.core.config import Settings

    Settings(**_production())  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="UPI_VPA"):
        Settings(**_production(UPI_VPA=""))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="OTP_HMAC_SECRET"):
        Settings(**_production(OTP_HMAC_SECRET=""))  # type: ignore[arg-type]


def test_upi_vpa_must_look_like_a_upi_id() -> None:
    import pytest
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(UPI_VPA="not a vpa")
    assert Settings(UPI_VPA="Nirogya@OKAXIS").UPI_VPA == "nirogya@okaxis"
