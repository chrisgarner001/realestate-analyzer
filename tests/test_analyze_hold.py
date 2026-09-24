"""Integration tests for POST /analyze.

TestAnalyzeRegression pins how the 12 existing report types behave today
(docs/designs/owned-asset-hold-sell-refi.md, eng review R14). They were
written before any owned-asset change touched /analyze and must keep passing
unchanged.
"""
import pytest

import auth
from database import Tenant, User, Analysis, BuyerLead

EXISTING_TYPES = ["full", "quick", "comps", "rental", "invest", "neighborhood",
                  "market", "flip", "mortgage", "compare", "listing", "screen"]


def _make_tenant(db, slug="acme", daily_limit=5, contact_email="sponsor@acme.test"):
    tenant = Tenant(slug=slug, company_name="Acme Lending", daily_limit=daily_limit,
                    contact_email=contact_email, tier="partner")
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _make_user(db, tenant, email="agent@acme.test", role="realtor", tokens=10):
    user = User(tenant_id=tenant.id if tenant else None, email=email,
                password_hash=auth.hash_password("pw12345678"), full_name="Pat Agent",
                role=role, token_balance=tokens, is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _headers(user, tenant):
    token = auth.create_token(user.id, user.email, user.role,
                              tenant.id if tenant else None, tenant.slug if tenant else None)
    return {"Authorization": f"Bearer {token}"}


def _analyze(client, headers, analysis_type="full", **extra):
    body = {"address": "123 Main St, Detroit, MI 48201", "analysis_type": analysis_type,
            "asking_price": "150000"}
    if analysis_type == "compare":
        body["address2"] = "456 Oak St, Detroit, MI 48202"
    body.update(extra)
    return client.post("/analyze", json=body, headers=headers)


def _balance(db, user):
    db.expire_all()
    return db.get(User, user.id).token_balance


class TestAnalyzeRegression:
    @pytest.mark.parametrize("analysis_type", EXISTING_TYPES)
    def test_each_existing_type_debits_exactly_one_token(self, client, db, sent_emails, analysis_type):
        tenant = _make_tenant(db)
        user = _make_user(db, tenant, tokens=10)
        r = _analyze(client, _headers(user, tenant), analysis_type)
        assert r.status_code == 200
        assert _balance(db, user) == 9

    def test_zero_balance_returns_402(self, client, db, sent_emails):
        tenant = _make_tenant(db)
        user = _make_user(db, tenant, tokens=0)
        r = _analyze(client, _headers(user, tenant))
        assert r.status_code == 402
        assert db.query(Analysis).count() == 0

    def test_superadmin_is_not_debited(self, client, db, sent_emails):
        admin = _make_user(db, None, email="root@propyield.test", role="superadmin", tokens=0)
        r = _analyze(client, _headers(admin, None))
        assert r.status_code == 200
        assert _balance(db, admin) == 0

    def test_daily_limit_returns_429(self, client, db, sent_emails):
        tenant = _make_tenant(db, daily_limit=2)
        user = _make_user(db, tenant, tokens=10)
        headers = _headers(user, tenant)
        assert _analyze(client, headers).status_code == 200
        assert _analyze(client, headers).status_code == 200
        r = _analyze(client, headers)
        assert r.status_code == 429
        assert _balance(db, user) == 8

    def test_analysis_and_lead_rows_written(self, client, db, sent_emails):
        tenant = _make_tenant(db)
        user = _make_user(db, tenant)
        _analyze(client, _headers(user, tenant), "full", buyer_name="Bo Buyer")
        analysis = db.query(Analysis).one()
        lead = db.query(BuyerLead).one()
        assert analysis.analysis_type == "full" and analysis.tenant_id == tenant.id
        assert lead.analysis_id == analysis.id and lead.buyer_name == "Bo Buyer"
        assert "Fake analysis text" in (lead.report_text or "")

    def test_report_emails_go_to_sponsor_notification_and_admins(self, client, db, sent_emails, monkeypatch):
        monkeypatch.setenv("REPORT_NOTIFICATION_EMAIL", "founder@propyield.test")
        tenant = _make_tenant(db, contact_email="sponsor@acme.test")
        user = _make_user(db, tenant)
        _make_user(db, tenant, email="boss@acme.test", role="admin")
        _analyze(client, _headers(user, tenant))
        recipients = {e["to"] for e in sent_emails}
        assert {"sponsor@acme.test", "founder@propyield.test", "boss@acme.test"} <= recipients

    def test_failure_does_not_refund_existing_types(self, client, db, sent_emails, fake_anthropic):
        fake_anthropic.stream_raise_after = 0
        tenant = _make_tenant(db)
        user = _make_user(db, tenant, tokens=5)
        r = _analyze(client, _headers(user, tenant))
        assert r.status_code == 200
        assert '"error"' in r.text
        assert _balance(db, user) == 4
