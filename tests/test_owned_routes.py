"""Integration tests for the owned-asset exit API (web/owned_routes.py).

Uses the synthetic inventory fixture (never real client data) and the fake
Anthropic client: create() answers the estimate / statement calls, stream()
writes the rationale.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import auth
from database import OwnedBatchRow, OwnedStatement, Tenant, User

FIXTURE = Path(__file__).parent / "fixtures" / "inventory_sample.csv"
ESTIMATE = json.dumps({"as_is_value": 80000, "arv": 140000, "market_rent": 1400, "dom_as_is_days": 45,
                       "dom_renovated_days": 30, "appreciation_pct": 3, "months_of_supply": 3,
                       "beds": 3, "baths": 1, "sqft": 1100, "year_built": 1952,
                       "sources": {"as_is_value": "3 sales within 0.5 mi"}, "comps": []})


def _tenant(db, enabled=True, daily_limit=5):
    t = Tenant(slug="ires", company_name="Sample Group", daily_limit=daily_limit, tier="partner",
               contact_email="sponsor@sample.test", asset_analysis_enabled=enabled)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _user(db, tenant, email="am@sample.test", role="admin", tokens=100):
    u = User(tenant_id=tenant.id if tenant else None, email=email, password_hash=auth.hash_password("pw12345678"),
             full_name="Asset Manager", role=role, token_balance=tokens, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _h(user, tenant):
    return {"Authorization": "Bearer " + auth.create_token(user.id, user.email, user.role,
                                                           tenant.id if tenant else None,
                                                           tenant.slug if tenant else None)}


def _upload(client, headers, data=None):
    return client.post("/api/owned/batches", headers=headers,
                       files={"file": ("inventory.csv", data or FIXTURE.read_bytes(), "text/csv")})


def _events(resp):
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def _balance(db, user):
    db.expire_all()
    return db.get(User, user.id).token_balance


def _row(db, row_id):
    db.expire_all()
    return db.get(OwnedBatchRow, row_id)


@pytest.fixture()
def setup(client, db, fake_anthropic, sent_emails):
    tenant = _tenant(db)
    user = _user(db, tenant)
    headers = _h(user, tenant)
    batch = _upload(client, headers).json()
    return {"tenant": tenant, "user": user, "h": headers, "batch": batch}


def _run(client, setup, row, fake, estimate=ESTIMATE):
    fake.create_texts.append(estimate)
    return client.post(f"/api/owned/batches/{setup['batch']['id']}/rows/{row['id']}/run", headers=setup["h"])


def _runnable(batch):
    return [r for r in batch["rows"] if r["status"] == "queued"]


class TestAccess:
    def test_not_enabled_is_403(self, client, db):
        tenant = _tenant(db, enabled=False)
        user = _user(db, tenant)
        assert _upload(client, _h(user, tenant)).status_code == 403

    def test_me_reports_entitlement_and_cost(self, client, db):
        tenant = _tenant(db)
        user = _user(db, tenant, tokens=40)
        me = client.get("/api/me", headers=_h(user, tenant)).json()
        assert me["asset_analysis_enabled"] is True and me["hold_token_cost"] == 5 and me["token_balance"] == 40

    def test_super_can_toggle_entitlement(self, client, db):
        tenant = _tenant(db, enabled=False)
        sup = _user(db, None, email="root@propyield.test", role="superadmin")
        h = _h(sup, None)
        assert client.put(f"/api/super/tenants/{tenant.id}", headers=h,
                          json={"asset_analysis_enabled": True, "lc_servicer": "SGMS"}).status_code == 200
        listed = [t for t in client.get("/api/super/tenants", headers=h).json() if t["id"] == tenant.id][0]
        assert listed["asset_analysis_enabled"] is True and listed["lc_servicer"] == "SGMS"

    def test_other_tenant_cannot_see_batch(self, client, db, setup):
        other = Tenant(slug="other", company_name="Other", tier="partner", asset_analysis_enabled=True)
        db.add(other)
        db.commit()
        u2 = _user(db, other, email="x@other.test")
        assert client.get(f"/api/owned/batches/{setup['batch']['id']}", headers=_h(u2, other)).status_code == 404


class TestUpload:
    def test_preview_counts(self, setup):
        b = setup["batch"]
        assert len(b["rows"]) == 4
        assert [r["status"] for r in b["rows"]].count("skipped_pending") == 1
        assert b["runnable_count"] == 3 and b["tokens_needed"] == 15

    def test_missing_columns_422_names_them(self, client, setup):
        r = _upload(client, setup["h"], b"Building Name,Building City\nElm 1,Town\n")
        assert r.status_code == 422 and "Marketing: Comps" in r.json()["detail"]["missing"]

    def test_duplicate_warning(self, client, setup):
        again = _upload(client, setup["h"]).json()
        assert again["duplicate_of"]["batch_id"] == setup["batch"]["id"]

    def test_include_pending_row(self, client, setup):
        pending = [r for r in setup["batch"]["rows"] if r["status"] == "skipped_pending"][0]
        r = client.patch(f"/api/owned/batches/{setup['batch']['id']}/rows/{pending['id']}",
                         headers=setup["h"], json={"include": True})
        assert r.json()["status"] == "queued"

    def test_loan_override_needs_rate_and_pi(self, client, setup):
        row = setup["batch"]["rows"][0]
        url = f"/api/owned/batches/{setup['batch']['id']}/rows/{row['id']}"
        assert client.patch(url, headers=setup["h"], json={"overrides": {"loan_payoff": 40000}}).status_code == 422
        ok = client.patch(url, headers=setup["h"],
                          json={"overrides": {"loan_payoff": 40000, "loan_rate_pct": 7.5, "loan_pi": 410}})
        assert ok.status_code == 200 and "No loan entered" not in ok.json()["flags"]


class TestRun:
    def test_success_debits_five_and_stores_analysis(self, client, db, fake_anthropic, setup):
        row = _runnable(setup["batch"])[0]
        resp = _run(client, setup, row, fake_anthropic)
        events = _events(resp)
        assert [e["status"] for e in events if "status" in e][:3] == ["estimating", "computing", "writing"]
        assert any("analysis" in e for e in events) and events[-1]["done"] is True
        assert _balance(db, setup["user"]) == 95
        stored = _row(db, row["id"])
        assert stored.status == "done" and stored.debited == 5 and stored.rationale
        assert json.loads(stored.analysis)["recommendation"]["strategy"]
        assert fake_anthropic.create_calls[0]["tools"]            # estimate uses web search
        assert "tools" not in fake_anthropic.stream_calls[0]       # rationale does not (E2)

    def test_double_run_409(self, client, fake_anthropic, setup):
        row = _runnable(setup["batch"])[0]
        _run(client, setup, row, fake_anthropic)
        assert _run(client, setup, row, fake_anthropic).status_code == 409

    def test_insufficient_tokens_402(self, client, db, fake_anthropic, setup):
        u = db.get(User, setup["user"].id)
        u.token_balance = 4
        db.commit()
        assert _run(client, setup, _runnable(setup["batch"])[0], fake_anthropic).status_code == 402

    def test_estimate_failure_refunds(self, client, db, fake_anthropic, setup):
        fake_anthropic.create_raise = True
        row = _runnable(setup["batch"])[0]
        events = _events(_run(client, setup, row, fake_anthropic))
        assert events[-1]["refunded"] is True
        assert _balance(db, setup["user"]) == 100 and _row(db, row["id"]).status == "failed_refunded"

    def test_estimate_missing_values_refunds_with_guidance(self, client, db, fake_anthropic, setup):
        row = _runnable(setup["batch"])[0]
        events = _events(_run(client, setup, row, fake_anthropic, estimate=json.dumps({"arv": 140000})))
        assert "market rent" in events[-1]["error"]
        assert _balance(db, setup["user"]) == 100

    def test_rationale_fails_before_text_refunds(self, client, db, fake_anthropic, setup):
        fake_anthropic.stream_raise_after = 0
        row = _runnable(setup["batch"])[0]
        _run(client, setup, row, fake_anthropic)
        assert _balance(db, setup["user"]) == 100 and _row(db, row["id"]).status == "failed_refunded"

    def test_rationale_fails_after_text_charged(self, client, db, fake_anthropic, setup):
        fake_anthropic.stream_chunks = ["Sell as-is wins ", "because"]
        fake_anthropic.stream_raise_after = 1
        row = _runnable(setup["batch"])[0]
        _run(client, setup, row, fake_anthropic)
        assert _balance(db, setup["user"]) == 95 and _row(db, row["id"]).status == "failed_charged"

    def test_rerun_after_refund_works(self, client, db, fake_anthropic, setup):
        fake_anthropic.create_raise = True
        row = _runnable(setup["batch"])[0]
        _run(client, setup, row, fake_anthropic)
        fake_anthropic.create_raise = False
        _run(client, setup, row, fake_anthropic)
        assert _row(db, row["id"]).status == "done" and _balance(db, setup["user"]) == 95

    def test_superadmin_not_debited(self, client, db, fake_anthropic, setup):
        sup = _user(db, None, email="root@propyield.test", role="superadmin", tokens=0)
        setup = {**setup, "h": _h(sup, None)}
        row = _runnable(setup["batch"])[0]
        _run(client, setup, row, fake_anthropic)
        assert _row(db, row["id"]).status == "done" and _row(db, row["id"]).debited == 0

    def test_batch_rows_ignore_daily_limit(self, client, db, fake_anthropic, setup):
        t = db.get(Tenant, setup["tenant"].id)
        t.daily_limit = 1
        db.commit()
        for row in _runnable(setup["batch"]):
            _run(client, setup, row, fake_anthropic)
        assert all(_row(db, r["id"]).status == "done" for r in _runnable(setup["batch"]))


class TestFinish:
    def test_summary_email_once_to_admins_only(self, client, db, fake_anthropic, setup, sent_emails):
        _user(db, setup["tenant"], email="agent@sample.test", role="realtor")
        for row in _runnable(setup["batch"]):
            _run(client, setup, row, fake_anthropic)
        assert [e["to"] for e in sent_emails] == ["am@sample.test"]
        assert "3 analyzed" in sent_emails[0]["body"]
        # a later free recalculation never re-sends
        client.post(f"/api/owned/batches/{setup['batch']['id']}/recalculate", headers=setup["h"])
        assert len(sent_emails) == 1

    def test_recalculate_is_free_and_marks_rationale_stale(self, client, db, fake_anthropic, setup):
        row = _runnable(setup["batch"])[0]
        _run(client, setup, row, fake_anthropic)
        client.put(f"/api/owned/batches/{setup['batch']['id']}/assumptions", headers=setup["h"],
                   json={"discount_rate_pct": 10})
        out = client.post(f"/api/owned/batches/{setup['batch']['id']}/recalculate", headers=setup["h"]).json()
        assert out["recalculated"] == 1 and _balance(db, setup["user"]) == 95
        assert [r for r in out["rows"] if r["id"] == row["id"]][0]["rationale_stale"] is True

    def test_assumptions_validated_and_remembered(self, client, db, setup):
        url = f"/api/owned/batches/{setup['batch']['id']}/assumptions"
        assert client.put(url, headers=setup["h"], json={"discount_rate_pct": 99}).status_code == 422
        client.put(url, headers=setup["h"], json={"commission_pct": 5})
        nxt = _upload(client, setup["h"]).json()
        assert nxt["assumptions"]["commission_pct"] == 5

    def test_stuck_row_reset_refunds(self, client, db, fake_anthropic, setup):
        row = _runnable(setup["batch"])[0]
        r = db.get(OwnedBatchRow, row["id"])
        r.status, r.debited, r.started_at = "running", 5, datetime.utcnow() - timedelta(minutes=11)
        u = db.get(User, setup["user"].id)
        u.token_balance = 95
        db.commit()
        client.get(f"/api/owned/batches/{setup['batch']['id']}", headers=setup["h"])
        assert _row(db, row["id"]).status == "queued" and _balance(db, setup["user"]) == 100

    def test_writeup_generated_once(self, client, db, fake_anthropic, setup):
        row = _runnable(setup["batch"])[0]
        _run(client, setup, row, fake_anthropic)
        url = f"/api/owned/batches/{setup['batch']['id']}/rows/{row['id']}/writeup"
        client.post(url, headers=setup["h"])
        calls = len(fake_anthropic.stream_calls)
        assert client.post(url, headers=setup["h"]).json()["cached"] is True
        assert len(fake_anthropic.stream_calls) == calls and _balance(db, setup["user"]) == 95


class TestSingle:
    def test_single_run_counts_daily_limit(self, client, db, fake_anthropic):
        tenant = _tenant(db, daily_limit=1)
        user = _user(db, tenant)
        h = _h(user, tenant)
        body = {"address": "100 Sample St", "city": "Sample Town", "arv": 140000}
        s1 = client.post("/api/owned/single", headers=h, json=body).json()
        fake_anthropic.create_texts.append(ESTIMATE)
        client.post(f"/api/owned/batches/{s1['id']}/rows/{s1['rows'][0]['id']}/run", headers=h)
        s2 = client.post("/api/owned/single", headers=h, json=body).json()
        r = client.post(f"/api/owned/batches/{s2['id']}/rows/{s2['rows'][0]['id']}/run", headers=h)
        assert s1["kind"] == "single" and r.status_code == 429

    def test_single_skips_estimate_when_values_given(self, client, db, fake_anthropic, setup):
        s = client.post("/api/owned/single", headers=setup["h"],
                        json={"address": "100 Sample St", "city": "Sample Town", "arv": 140000}).json()
        row = s["rows"][0]
        client.patch(f"/api/owned/batches/{s['id']}/rows/{row['id']}", headers=setup["h"],
                     json={"overrides": {"as_is_value": 80000, "market_rent": 1400}})
        client.post(f"/api/owned/batches/{s['id']}/rows/{row['id']}/run", headers=setup["h"])
        assert fake_anthropic.create_calls == [] and _row(db, row["id"]).status == "done"


def _statement(payoff=41000.0, date="2026-09-01", address="12345 Elm St, Sample Town, MI 48000"):
    return json.dumps({"is_loan_statement": True, "readable": True, "property_addresses": [address],
                       "payoff_amount": payoff, "interest_rate_pct": 7.25, "monthly_pi": 410,
                       "statement_date": date, "lender": "Sample Bank", "page": 1,
                       "snippets": {"payoff_amount": "Payoff amount $41,000.00"}})


class TestStatements:
    def _up(self, client, setup, fake, text):
        fake.create_texts.append(text)
        return client.post(f"/api/owned/batches/{setup['batch']['id']}/statements", headers=setup["h"],
                           files={"file": ("stmt.pdf", b"%PDF-1.4 fake", "application/pdf")}).json()

    def test_extract_match_confirm_deletes_file(self, client, db, fake_anthropic, setup):
        st = self._up(client, setup, fake_anthropic, _statement(date=datetime.utcnow().strftime("%Y-%m-%d")))
        assert st["status"] == "read" and st["match_tier"] == "exact" and st["has_file"]
        assert client.get(f"/api/owned/statements/{st['id']}/file", headers=setup["h"]).status_code == 200
        ok = client.post(f"/api/owned/statements/{st['id']}/confirm", headers=setup["h"],
                         json={"payoff": 41000, "rate": 7.25, "pi": 410}).json()
        assert ok["confirmed"] and not ok["has_file"]
        assert client.get(f"/api/owned/statements/{st['id']}/file", headers=setup["h"]).status_code == 410

    def test_confirmed_statement_beats_spreadsheet(self, client, db, fake_anthropic, setup):
        st = self._up(client, setup, fake_anthropic, _statement(date=datetime.utcnow().strftime("%Y-%m-%d")))
        client.post(f"/api/owned/statements/{st['id']}/confirm", headers=setup["h"],
                    json={"payoff": 41000, "rate": 7.25, "pi": 410})
        _run(client, setup, {"id": st["row_id"]}, fake_anthropic)
        a = json.loads(_row(db, st["row_id"]).analysis)
        payoff = [x for x in a["assumptions"] if x["name"] == "loan_payoff"][0]
        assert payoff["value"] == 41000 and payoff["source"] == "USER"
        assert "loan statement" in payoff["source_detail"]

    def test_stale_statement_needs_confirm_anyway(self, client, fake_anthropic, setup):
        st = self._up(client, setup, fake_anthropic, _statement(date="2026-01-01"))
        assert st["stale"] is True
        url = f"/api/owned/statements/{st['id']}/confirm"
        assert client.post(url, headers=setup["h"], json={"payoff": 41000, "rate": 7, "pi": 400}).status_code == 409
        assert client.post(url, headers=setup["h"],
                           json={"payoff": 41000, "rate": 7, "pi": 400, "confirm_stale": True}).status_code == 200

    def test_multi_property_statement_is_manual(self, client, fake_anthropic, setup):
        text = json.loads(_statement())
        text["property_addresses"] = ["12345 Elm St, Sample Town", "23456 Oak St, Sample Town"]
        st = self._up(client, setup, fake_anthropic, json.dumps(text))
        assert st["match_tier"] == "multi" and st["row_id"] is None

    def test_not_a_statement(self, client, fake_anthropic, setup):
        st = self._up(client, setup, fake_anthropic, json.dumps({"is_loan_statement": False, "readable": True}))
        assert st["status"] == "not_statement"

    def test_unsupported_type_rejected_without_model_call(self, client, fake_anthropic, setup):
        st = client.post(f"/api/owned/batches/{setup['batch']['id']}/statements", headers=setup["h"],
                         files={"file": ("notes.docx", b"PK..", "application/octet-stream")}).json()
        assert st["status"] == "not_statement" and fake_anthropic.create_calls == []


class TestFreeDuringTesting:
    def test_zero_cost_runs_without_tokens(self, client, db, fake_anthropic, setup, monkeypatch):
        monkeypatch.setenv("HOLD_REPORT_TOKEN_COST", "0")
        u = db.get(User, setup["user"].id)
        u.token_balance = 0
        db.commit()
        row = _runnable(setup["batch"])[0]
        _run(client, setup, row, fake_anthropic)
        assert _row(db, row["id"]).status == "done" and _row(db, row["id"]).debited == 0
        assert _balance(db, setup["user"]) == 0
        assert client.get("/api/me", headers=setup["h"]).json()["hold_token_cost"] == 0
