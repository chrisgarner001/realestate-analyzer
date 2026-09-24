"""Shared fixtures for the FastAPI integration tests.

This is the first integration-test harness in this codebase (see
docs/designs/self-serve-solo-tenant.md, Dependencies): a real FastAPI app
wired to an isolated in-memory SQLite database, with Stripe replaced by a
fake client so no network calls happen in tests.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "web"))

import database  # noqa: E402

TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=TEST_ENGINE)

# Point the database module at the isolated test engine BEFORE server.py is
# imported. database.get_db() (imported by reference into server.py) looks up
# database.SessionLocal dynamically on every call, so patching the module
# globals here is enough — no per-test dependency override needed.
database.engine = TEST_ENGINE
database.SessionLocal = TestSessionLocal


class FakeStripeClient:
    """Stand-in for stripe.StripeClient covering exactly the v1 resources
    server.py calls: customers, setup_intents, checkout.sessions."""

    def __init__(self):
        self._setup_intents = {}
        self._counter = 0
        self.v1 = SimpleNamespace(
            customers=SimpleNamespace(create=self._create_customer),
            setup_intents=SimpleNamespace(create=self._create_setup_intent, retrieve=self._retrieve_setup_intent),
            checkout=SimpleNamespace(sessions=SimpleNamespace(create=self._create_checkout_session)),
        )

    def _next_id(self, prefix):
        self._counter += 1
        return f"{prefix}_test_{self._counter}"

    def _create_customer(self, params):
        return {"id": self._next_id("cus")}

    def _create_setup_intent(self, params):
        si_id = self._next_id("seti")
        si = {"id": si_id, "client_secret": f"{si_id}_secret", "status": "requires_payment_method"}
        self._setup_intents[si_id] = si
        return si

    def _retrieve_setup_intent(self, si_id):
        import stripe
        if si_id not in self._setup_intents:
            raise stripe.InvalidRequestError("No such setup_intent", "id")
        return self._setup_intents[si_id]

    def _create_checkout_session(self, params):
        return {"id": self._next_id("cs"), "url": "https://checkout.stripe.com/test"}

    def mark_setup_intent_succeeded(self, si_id):
        self._setup_intents[si_id]["status"] = "succeeded"


@pytest.fixture()
def db():
    database.Base.metadata.create_all(bind=TEST_ENGINE)
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
        database.Base.metadata.drop_all(bind=TEST_ENGINE)


@pytest.fixture()
def fake_stripe():
    return FakeStripeClient()


@pytest.fixture()
def client(db, fake_stripe, monkeypatch):
    import server  # imported lazily so it binds to the patched database module above

    monkeypatch.setattr(server, "get_stripe_client", lambda: fake_stripe)

    from fastapi.testclient import TestClient
    with TestClient(server.app) as test_client:
        yield test_client
