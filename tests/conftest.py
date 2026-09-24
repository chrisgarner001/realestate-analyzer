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


class _FakeStream:
    """Mimics the context manager returned by anthropic's messages.stream():
    iterating yields events with .type / .delta.text like the real SDK."""

    def __init__(self, chunks, raise_after=None):
        self._chunks = chunks
        self._raise_after = raise_after

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for i, chunk in enumerate(self._chunks):
            if self._raise_after is not None and i == self._raise_after:
                raise RuntimeError("fake model failure")
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text=chunk))
        if self._raise_after is not None and self._raise_after >= len(self._chunks):
            raise RuntimeError("fake model failure")


class FakeAnthropicClient:
    """Stand-in for anthropic.Anthropic covering messages.stream() and
    messages.create(). Tests queue responses; every call is recorded.

    - stream_chunks: text chunks each stream() call yields (default one chunk)
    - stream_raise_after: index at which stream() raises (None = never);
      0 means it raises before any text
    - create_texts: queue of texts returned by successive create() calls
    - create_raise: if True, create() raises
    """

    def __init__(self):
        self.stream_chunks = ["## Report\n\nFake analysis text."]
        self.stream_raise_after = None
        self.create_texts = []
        self.create_raise = False
        self.stream_calls = []
        self.create_calls = []
        self.messages = SimpleNamespace(stream=self._stream, create=self._create)

    def _stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return _FakeStream(list(self.stream_chunks), self.stream_raise_after)

    def _create(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.create_raise:
            raise RuntimeError("fake create failure")
        text = self.create_texts.pop(0) if self.create_texts else "{}"
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


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
def fake_anthropic():
    return FakeAnthropicClient()


@pytest.fixture()
def sent_emails(monkeypatch):
    """Records every report email instead of sending it."""
    import server
    sent = []

    def _record(recipient, subject, body):
        sent.append({"to": recipient, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(server, "send_report_email", _record)
    return sent


@pytest.fixture()
def client(db, fake_stripe, fake_anthropic, monkeypatch):
    import server  # imported lazily so it binds to the patched database module above

    monkeypatch.setattr(server, "get_stripe_client", lambda: fake_stripe)
    monkeypatch.setattr(server, "get_client", lambda: fake_anthropic)

    from fastapi.testclient import TestClient
    with TestClient(server.app) as test_client:
        yield test_client
