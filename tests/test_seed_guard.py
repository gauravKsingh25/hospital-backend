"""The demo seed must never run against production.

It creates a cross-tenant platform owner whose password is printed in the
README. On a production database that is a public key to every hospital.
"""

from __future__ import annotations

import pytest

from app.core.config import settings


async def test_seed_demo_refuses_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import seed_demo

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    with pytest.raises(SystemExit, match="production"):
        await seed_demo.seed(code="DEMO", name="Demo Hospital", password="x" * 20)
