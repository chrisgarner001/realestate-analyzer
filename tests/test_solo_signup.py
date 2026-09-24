"""Integration tests for the self-serve solo tenant signup flow.

See docs/designs/self-serve-solo-tenant.md. Covers the invariants called out
in that design as the actual risk surface: no DB rows before the card is
verified, SetupIntent replay protection, slug-collision handling, and the
solo-tier single-user guard on the two endpoints that can otherwise add a
second User to a tenant.
"""
import database
from database import Tenant, User, TokenPurchase


def _start(client, email="jerry@example.com", company_name="Jerry Johnson Realty"):
    return client.post("/api/signup/solo/start", json={"email": email, "company_name": company_name})


def _complete(client, setup_intent_id, email="jerry@example.com", password="supersecret1",
             company_name="Jerry Johnson Realty", primary_color="#c0392b"):
    return client.post("/api/signup/solo/complete", json={
        "setup_intent_id": setup_intent_id, "email": email, "password": password,
        "company_name": company_name, "primary_color": primary_color,
    })


class TestSoloSignupHappyPath:
    def test_start_creates_no_database_rows(self, client, db):
        r = _start(client)
        assert r.status_code == 200
        assert "client_secret" in r.json()
        assert db.query(Tenant).count() == 0
        assert db.query(User).filter_by(email="jerry@example.com").count() == 0

    def test_complete_requires_succeeded_setup_intent(self, client, fake_stripe):
        start = _start(client).json()
        si_id = start["client_secret"].rsplit("_secret", 1)[0]
        # Card not yet confirmed client-side — status is still requires_payment_method.
        r = _complete(client, si_id)
        assert r.status_code == 400

    def test_full_flow_creates_solo_tenant_with_free_token(self, client, fake_stripe, db):
        start = _start(client).json()
        si_id = start["client_secret"].rsplit("_secret", 1)[0]
        fake_stripe.mark_setup_intent_succeeded(si_id)

        r = _complete(client, si_id)
        assert r.status_code == 200
        body = r.json()
        assert body["access_token"]
        assert body["user"]["role"] == "admin"
        slug = body["user"]["slug"]

        tenant = db.query(Tenant).filter_by(slug=slug).first()
        admin = db.query(User).filter_by(email="jerry@example.com").first()
        assert tenant is not None and tenant.tier == "solo"
        assert tenant.setup_intent_id == si_id
        assert admin is not None and admin.token_balance == 1
        # Free token lands on the User, not the Tenant pool — a solo tenant is
        # a one-person tenant, so there's no separate admin-allocation step.
        assert (tenant.token_balance or 0) == 0


class TestSetupIntentReplayProtection:
    def test_same_setup_intent_cannot_mint_a_second_tenant(self, client, fake_stripe, db):
        start = _start(client, email="a@example.com").json()
        si_id = start["client_secret"].rsplit("_secret", 1)[0]
        fake_stripe.mark_setup_intent_succeeded(si_id)

        first = _complete(client, si_id, email="a@example.com")
        assert first.status_code == 200

        second = _complete(client, si_id, email="b@example.com", company_name="A Different Company")
        assert second.status_code == 400
        assert db.query(Tenant).count() == 1


class TestSlugCollision:
    def test_second_signup_with_same_company_name_gets_a_suffixed_slug(self, client, fake_stripe, db):
        start1 = _start(client, email="one@example.com").json()
        si_1 = start1["client_secret"].rsplit("_secret", 1)[0]
        fake_stripe.mark_setup_intent_succeeded(si_1)
        r1 = _complete(client, si_1, email="one@example.com", company_name="Acme Investments")
        slug1 = r1.json()["user"]["slug"]

        start2 = _start(client, email="two@example.com").json()
        si_2 = start2["client_secret"].rsplit("_secret", 1)[0]
        fake_stripe.mark_setup_intent_succeeded(si_2)
        r2 = _complete(client, si_2, email="two@example.com", company_name="Acme Investments")
        slug2 = r2.json()["user"]["slug"]

        assert slug1 != slug2
        assert slug2.startswith(slug1)
        assert db.query(Tenant).filter_by(slug=slug1).count() == 1
        assert db.query(Tenant).filter_by(slug=slug2).count() == 1


class TestSoloTenantIsSingleUser:
    def _make_solo_admin_token(self, client, fake_stripe):
        start = _start(client, email="solo-admin@example.com").json()
        si_id = start["client_secret"].rsplit("_secret", 1)[0]
        fake_stripe.mark_setup_intent_succeeded(si_id)
        return _complete(client, si_id, email="solo-admin@example.com").json()["access_token"]

    def test_admin_create_user_rejected_for_solo_tier(self, client, fake_stripe):
        token = self._make_solo_admin_token(client, fake_stripe)
        r = client.post("/api/admin/users",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"email": "teammate@example.com", "password": "password123", "full_name": "Teammate"})
        assert r.status_code == 403

    def test_admin_invite_rejected_for_solo_tier(self, client, fake_stripe):
        token = self._make_solo_admin_token(client, fake_stripe)
        r = client.post("/api/admin/invite",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"email": "teammate@example.com", "password": "password123", "full_name": "Teammate",
                              "subject": "Join", "html_body": "<p>hi</p>", "text_body": "hi"})
        assert r.status_code == 403


class TestWebhookCreditsSoloUserNotTenantPool:
    """A purchase Checkout Session's webhook only carries tenant_id in its
    metadata. For a partner tenant that credits Tenant.token_balance; for a
    solo tenant (single-admin invariant) it must credit the admin User
    directly instead, per docs/designs/self-serve-solo-tenant.md."""

    def test_solo_purchase_credits_the_admin_user(self, client, db):
        import server
        tenant = Tenant(slug="solo-webhook-test", company_name="Solo Co", tier="solo", token_balance=0)
        db.add(tenant); db.flush()
        admin = User(tenant_id=tenant.id, email="solo-webhook@example.com",
                     password_hash="x", role="admin", token_balance=1)
        db.add(admin); db.commit()

        server._credit_tenant_tokens(db, {
            "id": "cs_test_solo_1", "metadata": {"tenant_id": str(tenant.id), "token_count": "10"},
            "amount_total": 5000, "currency": "usd",
        })

        db.refresh(tenant); db.refresh(admin)
        assert admin.token_balance == 11
        assert (tenant.token_balance or 0) == 0
        purchase = db.query(TokenPurchase).filter_by(stripe_session_id="cs_test_solo_1").first()
        assert purchase.status == "completed"

    def test_solo_purchase_with_no_active_admin_is_flagged_not_lost(self, client, db):
        import server
        tenant = Tenant(slug="solo-orphan-test", company_name="Orphan Co", tier="solo", token_balance=0)
        db.add(tenant); db.commit()

        server._credit_tenant_tokens(db, {
            "id": "cs_test_solo_orphan", "metadata": {"tenant_id": str(tenant.id), "token_count": "10"},
            "amount_total": 5000, "currency": "usd",
        })

        purchase = db.query(TokenPurchase).filter_by(stripe_session_id="cs_test_solo_orphan").first()
        assert purchase is not None
        assert purchase.status == "uncredited"
