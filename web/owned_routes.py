"""Owned-asset exit analysis API (batch + single runs, loan statements).

Design: docs/designs/owned-asset-exit-analysis.md. The engine is
owned_asset.py (all numbers); the model only estimates DATA inputs and writes
the rationale / full write-up. Runs are one property per request so each fits
Vercel's 300 s limit; the browser drives the queue (E1).

Row status: queued -> running -> done | failed_refunded | failed_charged
            skipped_pending / excluded rows are never charged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import asdict, fields as dc_fields
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

import auth
import database
import owned_asset
import owned_import
from database import Analysis, OwnedBatch, OwnedBatchRow, OwnedStatement, Tenant, User, get_db

router = APIRouter()

STUCK_AFTER = timedelta(minutes=10)
STATEMENT_RETENTION = timedelta(hours=24)
STALE_STATEMENT_DAYS = 60
MAX_STATEMENT_BYTES = 4 * 1024 * 1024       # Vercel request body limit is 4.5 MB
MAX_STATEMENTS_PER_BATCH = 100
ESTIMATE_TIMEOUT_S = 60
FINAL_STATUSES = {"done", "failed_refunded", "failed_charged", "skipped_pending", "excluded"}
RUNNABLE_STATUSES = ("queued", "failed_refunded", "failed_charged")


def hold_cost() -> int:
    return int(os.getenv("HOLD_REPORT_TOKEN_COST", "5"))


def _server():
    import server  # late import: server imports this module
    return server


def _j(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _dump(obj) -> str:
    return json.dumps(obj, default=str)


# ── access control ──────────────────────────────────────────────────────────

def require_owned_access(current_user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)) -> User:
    if current_user.role == "superadmin":
        return current_user
    tenant = db.query(Tenant).filter_by(id=current_user.tenant_id).first() if current_user.tenant_id else None
    if not tenant or not tenant.asset_analysis_enabled:
        raise HTTPException(403, "Hold / Sell / Refi isn't enabled for your company. Contact your PropYield admin.")
    return current_user


def _get_batch(db: Session, batch_id: int, user: User) -> OwnedBatch:
    batch = db.query(OwnedBatch).filter_by(id=batch_id).first()
    if not batch or (user.role != "superadmin" and batch.tenant_id != user.tenant_id):
        raise HTTPException(404, "Batch not found")
    return batch


def _get_row(db: Session, batch: OwnedBatch, row_id: int) -> OwnedBatchRow:
    row = db.query(OwnedBatchRow).filter_by(id=row_id, batch_id=batch.id).first()
    if not row:
        raise HTTPException(404, "Row not found")
    return row


# ── assumptions ─────────────────────────────────────────────────────────────

ASSUMPTION_FIELDS = {f.name for f in dc_fields(owned_asset.Assumptions)}
ASSUMPTION_BOUNDS = {   # R7-style bounds for editable assumptions
    "discount_rate_pct": (0, 30), "commission_pct": (0, 15), "closing_pct": (0, 10),
    "as_is_commission_pct": (0, 15), "concessions_pct": (0, 10), "utilities_vacant_monthly": (0, 2000),
    "insurance_default_annual": (0, 20000), "vacancy_pct": (0, 50), "maintenance_pct": (0, 30),
    "capex_pct": (0, 30), "management_pct": (0, 30), "rent_growth_pct": (-10, 15),
    "tax_growth_pct": (-10, 15), "insurance_growth_pct": (-10, 30), "appreciation_default_pct": (-10, 15),
    "lc_price_premium_pct": (0, 50), "lc_down_pct": (0, 50), "lc_rate_pct": (0, 20),
    "lc_amort_years": (1, 40), "lc_default_prob_pct": (0, 60), "lc_servicing_monthly": (0, 500),
    "lc_balloon_paid_prob_pct": (0, 100), "lc_note_discount_pct": (0, 60), "horizon_months": (12, 120),
    "cost_of_capital_pct": (0, 30),
}


def _assumptions(batch: OwnedBatch, tenant: Optional[Tenant]) -> owned_asset.Assumptions:
    data = _j(batch.assumptions, {}) or {}
    kwargs = {k: v for k, v in data.items() if k in ASSUMPTION_FIELDS}
    if tenant is not None and tenant.lc_servicer and "lc_servicer" not in kwargs:
        kwargs["lc_servicer"] = tenant.lc_servicer
    return owned_asset.Assumptions(**kwargs)


def _assumptions_hash(a: owned_asset.Assumptions) -> str:
    return hashlib.sha256(_dump(asdict(a)).encode()).hexdigest()[:16]


def _validate_assumptions(data: dict) -> dict:
    clean = {}
    for k, v in data.items():
        if k not in ASSUMPTION_FIELDS:
            raise HTTPException(422, f"Unknown assumption: {k}")
        if v is None:
            continue
        if k in ASSUMPTION_BOUNDS:
            lo, hi = ASSUMPTION_BOUNDS[k]
            try:
                fv = float(v)
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
            if not lo <= fv <= hi:
                raise HTTPException(422, f"{k} must be between {lo} and {hi}")
        clean[k] = v
    return clean


# ── overrides (Edit drawer) ─────────────────────────────────────────────────

OVERRIDE_BOUNDS = {
    "loan_payoff": (0, 2_000_000), "loan_rate_pct": (0, 25), "loan_pi": (0, 20_000),
    "investor_payback": (0, 5_000_000), "cost_basis": (0, 5_000_000), "annual_insurance": (0, 20_000),
    "annual_tax": (0, 100_000), "arv": (1, 10_000_000), "as_is_value": (1, 10_000_000),
    "market_rent": (1, 50_000), "rehab_budget": (0, 500_000), "beds": (0, 20), "baths": (0, 20),
    "sqft": (100, 20_000), "year_built": (1800, 2030),
}


def _validate_overrides(data: dict) -> dict:
    clean = {}
    for k, v in data.items():
        if k not in OVERRIDE_BOUNDS:
            raise HTTPException(422, f"Unknown field: {k}")
        if v in (None, ""):
            clean[k] = None
            continue
        lo, hi = OVERRIDE_BOUNDS[k]
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"{k} must be a number")
        if not lo <= fv <= hi:
            raise HTTPException(422, f"{k} must be between {lo:,} and {hi:,}")
        clean[k] = fv
    if (clean.get("loan_payoff") or 0) > 0 and (not clean.get("loan_rate_pct") or not clean.get("loan_pi")):
        raise HTTPException(422, "A loan payoff above $0 requires the interest rate and monthly P&I")
    return clean


# ── building engine inputs (precedence: statement > drawer > spreadsheet > 0) ─

def _confirmed_statement(db: Session, row: OwnedBatchRow) -> Optional[OwnedStatement]:
    return (db.query(OwnedStatement)
              .filter_by(row_id=row.id, confirmed=True)
              .order_by(OwnedStatement.statement_date.desc().nullslast(), OwnedStatement.id.desc())
              .first())


def _property_inputs(db: Session, row: OwnedBatchRow, estimates: Optional[dict]) -> owned_asset.PropertyInputs:
    fields = _j(row.inputs, {})
    base = owned_import.row_to_inputs_dict(owned_import.ImportedRow(row.row_number, fields))
    sources = base.pop("sources")
    overrides = _j(row.overrides, {}) or {}
    for k, v in overrides.items():
        if v is None:
            continue
        key = {"loan_rate_pct": "loan_rate_pct"}.get(k, k)
        base[key] = v
        sources[key] = "USER"
    stmt = _confirmed_statement(db, row)
    stale = False
    if stmt:
        vals = _j(stmt.confirmed_values, {}) or {}
        for src_k, dst_k in (("payoff", "loan_payoff"), ("rate", "loan_rate_pct"), ("pi", "loan_pi")):
            if vals.get(src_k) is not None:
                base[dst_k] = float(vals[src_k])
        sources["loan_payoff"] = "USER"
        sources["loan_payoff_detail"] = f"loan statement {stmt.statement_date.date() if stmt.statement_date else ''}"
        if stmt.statement_date and (datetime.utcnow() - stmt.statement_date).days > STALE_STATEMENT_DAYS:
            stale = True
    est = estimates or {}
    for k in ("as_is_value", "arv", "market_rent", "dom_as_is_days", "dom_renovated_days", "appreciation_pct",
              "months_of_supply", "beds", "baths", "sqft", "year_built"):
        if base.get(k) is None and est.get(k) is not None:
            base[k] = est[k]
            sources[k] = "DATA"
            if est.get("sources", {}).get(k):
                sources[k + "_detail"] = est["sources"][k]
    kwargs = {k: v for k, v in base.items() if k in {f.name for f in dc_fields(owned_asset.PropertyInputs)}}
    kwargs["stale_loan_statement"] = stale
    kwargs["sources"] = sources
    for k in ("dom_as_is_days", "dom_renovated_days", "year_built"):
        if kwargs.get(k) is not None:
            kwargs[k] = int(kwargs[k])
    return owned_asset.PropertyInputs(**kwargs)


def _needs_estimate(row: OwnedBatchRow) -> bool:
    fields = _j(row.inputs, {}) or {}
    ov = _j(row.overrides, {}) or {}
    have_as_is = ov.get("as_is_value") is not None
    have_rent = ov.get("market_rent") is not None
    have_arv = (ov.get("arv") or fields.get("arv")) is not None
    return not (have_as_is and have_rent and have_arv)


# ── model prompts ───────────────────────────────────────────────────────────

ESTIMATE_SYSTEM = """You are a real estate data researcher for a Michigan single-family rental owner.
Use web search to find current market data for ONE property. Return ONLY a JSON object, no other text:
{"as_is_value": <number, current value in present condition from similar-condition sales in the last 6 months within 0.5 mi, widen if needed>,
 "arv": <number, after-repair value from renovated comps>,
 "market_rent": <number, monthly rent after rehab from rent comps>,
 "dom_as_is_days": <number>, "dom_renovated_days": <number>,
 "appreciation_pct": <number, local 5-year average annual appreciation>,
 "months_of_supply": <number>,
 "beds": <number or null>, "baths": <number or null>, "sqft": <number or null>, "year_built": <number or null>,
 "sources": {"as_is_value": "...", "arv": "...", "market_rent": "...", "appreciation_pct": "...", "facts": "..."},
 "comps": [{"address": "...", "price": <number>, "date": "YYYY-MM-DD", "condition": "..."}]}
Never invent data. If a value cannot be found, use null. Be conservative: lean toward lower prices and longer timelines."""

RATIONALE_SYSTEM = """You are a direct, specific real estate analyst writing for an asset manager.
Write 3 to 5 sentences explaining the recommended exit for this property, using ONLY the numbers provided.
Never compute, change, or introduce new numbers. Name the recommended exit and its advantage over the runner-up,
the main driver, and the key risk. No headings, no process narration, no hype."""

WRITEUP_SYSTEM = """You are the PropYield Exit Strategy Analyzer, a senior real estate investment analyst.
Write the report_markdown for ONE owned property from the analysis_json provided. All numbers come from
analysis_json: never compute or change them. Sections, in order:
1. Recommendation: one bold sentence with the strategy and its advantage over the runner-up.
2. Why this wins: 3-5 bullets, each citing specific numbers from analysis_json.
3. What would change the answer: the break-even triggers in plain English.
4. Stress test summary: which strategy holds up best in the combined downside.
5. Risks & flags: data gaps, compliance and tax items (capital gains, depreciation recapture, installment sale, 1031 as CPA considerations).
6. Assumptions used: note which DEFAULT assumptions the analysis relied on.
End with: "This analysis is an investment model, not legal or tax advice. Confirm the tax impact with your CPA and land contract terms with a Michigan real estate attorney."
Output only the final report. No process narration. Tone: trusted analyst, direct, no hype."""

STATEMENT_SYSTEM = """You read one mortgage/loan statement. Return ONLY a JSON object:
{"is_loan_statement": true|false,
 "readable": true|false,
 "property_addresses": ["<collateral/property address(es), NOT the borrower or lender mailing address>"],
 "payoff_amount": <number or null, payoff or principal balance>,
 "interest_rate_pct": <number or null>,
 "monthly_pi": <number or null, principal + interest only>,
 "statement_date": "YYYY-MM-DD or null",
 "lender": "<name or null>",
 "page": <page number where the payoff appears, or null>,
 "snippets": {"payoff_amount": "<exact text near the payoff>", "interest_rate_pct": "...", "monthly_pi": "..."}}
Never guess a number: use null when it is not printed."""


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _last_text(resp) -> str:
    texts = [getattr(b, "text", "") for b in getattr(resp, "content", []) if getattr(b, "type", None) == "text"]
    return texts[-1] if texts else ""


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if math_isfinite(f) else None
    except (TypeError, ValueError):
        return None


def math_isfinite(f: float) -> bool:
    return f == f and f not in (float("inf"), float("-inf"))


def _estimate(row: OwnedBatchRow) -> dict:
    s = _server()
    fields = _j(row.inputs, {}) or {}
    ov = _j(row.overrides, {}) or {}
    prompt = (f"Property: {fields.get('address_line')}, {fields.get('city')}, {fields.get('state') or 'MI'}\n"
              f"Status: {fields.get('status') or 'Vacant'}. Owner's comps value: {fields.get('arv') or 'unknown'}. "
              f"Rehab quote: {fields.get('rehab_quote') or 'unknown'}.\n"
              f"Notes: {(fields.get('notes') or '')[:800]}\n"
              f"Known overrides: {json.dumps(ov)}")
    client = s.get_client()
    resp = client.messages.create(model=s.MODEL, max_tokens=1500, system=ESTIMATE_SYSTEM,
                                  messages=[{"role": "user", "content": prompt}],
                                  tools=s.WEB_SEARCH_TOOL, timeout=ESTIMATE_TIMEOUT_S)
    data = _extract_json(_last_text(resp))
    if data is None:
        raise ValueError("estimate response was not JSON")
    out = {"sources": data.get("sources") or {}, "comps": data.get("comps") or []}
    for k in ("as_is_value", "arv", "market_rent", "dom_as_is_days", "dom_renovated_days", "appreciation_pct",
              "months_of_supply", "beds", "baths", "sqft", "year_built"):
        out[k] = _num(data.get(k))
    return out


def _validate_estimate(row: OwnedBatchRow, est: dict) -> Optional[str]:
    """R5 per-field validation; returns an error naming the override to enter."""
    fields = _j(row.inputs, {}) or {}
    ov = _j(row.overrides, {}) or {}
    problems = []
    if ov.get("as_is_value") is None and not (est.get("as_is_value") or 0) > 0:
        problems.append("a current (as-is) value")
    if (ov.get("arv") or fields.get("arv")) is None and not (est.get("arv") or 0) > 0:
        problems.append("an ARV")
    if ov.get("market_rent") is None and not (est.get("market_rent") or 0) > 0:
        problems.append("a market rent")
    if problems:
        return "We couldn't find a reliable " + " or ".join(problems) + ". Enter it and re-run."
    return None


def _rationale_prompt(analysis: dict) -> str:
    rec = analysis["recommendation"]
    lines = [f"Recommended exit: {owned_asset.STRATEGY_LABELS[rec['strategy']]}"
             + (" (close call)" if rec.get("close_call") else "")]
    for s in analysis["scenarios"]:
        lines.append(
            f"- {owned_asset.STRATEGY_LABELS[s['strategy']]}: risk-adjusted value ${s['risk_adjusted_score']:,.0f}, "
            f"NPV ${s['npv']:,.0f}, net profit ${s['net_profit']:,.0f}, peak capital ${s['peak_capital']:,.0f}, "
            f"risk {s['risk_score']}/10"
            + (f", ruled out: {s['disqualifier']}" if s["disqualifier"] else ""))
    for t in rec.get("break_even_triggers", []):
        lines.append(f"- If {t['label']} is {t['direction']} ${t['threshold']:,.0f}, "
                     f"{owned_asset.STRATEGY_LABELS[t['new_winner']]} wins")
    lines += [f"Flag: {f['message']}" for f in analysis["flags"] if f["severity"] != "info"]
    return "\n".join(lines)


# ── refunds, stuck rows, finish + email ─────────────────────────────────────

def _refund(db: Session, row: OwnedBatchRow):
    if row.debited:
        user = db.query(User).filter_by(id=row.batch.user_id).first()
        if user:
            user.token_balance = (user.token_balance or 0) + row.debited
        row.debited = 0


def _reset_stuck(db: Session, batch: OwnedBatch):
    now = datetime.utcnow()
    changed = False
    for row in batch.rows:
        if row.status == "running" and row.started_at and now - row.started_at > STUCK_AFTER:
            if not row.rationale:
                _refund(db, row)
                row.status = "queued"
                row.error = "Reset after tab closed"
            else:
                row.status = "failed_charged"
                row.error = "Report incomplete"
            changed = True
    if changed:
        db.commit()


def _cleanup_statement_files(db: Session):
    cutoff = datetime.utcnow() - STATEMENT_RETENTION
    db.query(OwnedStatement).filter(OwnedStatement.created_at < cutoff,
                                    OwnedStatement.file_bytes.isnot(None)).update({"file_bytes": None})
    db.commit()


def _finished(batch: OwnedBatch) -> bool:
    return all(r.status in FINAL_STATUSES for r in batch.rows)


def _maybe_send_summary(db: Session, batch_id: int):
    batch = db.query(OwnedBatch).filter_by(id=batch_id).first()
    if not batch or batch.kind != "batch" or not _finished(batch):
        return
    claimed = db.execute(update(OwnedBatch).where(OwnedBatch.id == batch_id, OwnedBatch.summary_emailed.is_(False))
                         .values(summary_emailed=True, finished_at=datetime.utcnow())).rowcount
    db.commit()
    if claimed != 1:
        return
    body = summary_email_body(batch)
    admins = db.query(User).filter_by(tenant_id=batch.tenant_id, role="admin", is_active=True).all()
    s = _server()
    for admin in admins:
        try:
            s.send_report_email(admin.email, f"Portfolio review complete: {batch.file_name} ({len(batch.rows)} properties)", body)
        except Exception as e:  # pragma: no cover - SMTP failures are logged, never fatal
            print(f"Owned summary email failed for batch {batch.id}: {e}")


def summary_email_body(batch: OwnedBatch) -> str:
    done = [r for r in batch.rows if r.status == "done"]
    failed = [r for r in batch.rows if r.status.startswith("failed")]
    skipped = [r for r in batch.rows if r.status in ("skipped_pending", "excluded")]
    counts: dict = {}
    total_stake, total_carry, stakes = 0.0, 0.0, []
    for r in done:
        a = _j(r.analysis, {})
        rec = a.get("recommendation", {})
        strat = rec.get("strategy")
        counts[strat] = counts.get(strat, 0) + 1
        stake = row_dollars_at_stake(a)
        total_stake += stake
        total_carry += row_monthly_carry(a)
        stakes.append((stake, (_j(r.inputs, {}) or {}).get("address_line", ""), strat))
    stakes.sort(reverse=True)
    lines = [f"{len(done)} analyzed · {len(failed)} failed · {len(skipped)} not analyzed (pending sale or excluded)",
             f"Total carrying cost: ${total_carry:,.0f}/mo · Total at stake: ${total_stake:,.0f}",
             "Recommended exits: " + ", ".join(f"{owned_asset.STRATEGY_LABELS.get(k, k)} {v}" for k, v in counts.items()),
             "", "Top 10 by dollars at stake:"]
    for stake, addr, strat in stakes[:10]:
        lines.append(f"  {addr} — {owned_asset.STRATEGY_LABELS.get(strat, strat)} — ${stake:,.0f}")
    lines += ["", f"Open the batch in PropYield: /portfolio?batch={batch.id}"]
    return "\n".join(lines)


def row_dollars_at_stake(analysis: dict) -> float:
    sc = {s["strategy"]: s for s in analysis.get("scenarios", [])}
    rec = analysis.get("recommendation", {}).get("strategy")
    if not rec or rec not in sc or "sell_as_is" not in sc:
        return 0.0
    return sc[rec]["risk_adjusted_score"] - sc["sell_as_is"]["risk_adjusted_score"]


def row_monthly_carry(analysis: dict) -> float:
    sc = {s["strategy"]: s for s in analysis.get("scenarios", [])}
    s = sc.get("sell_as_is")
    if not s:
        return 0.0
    flows = s.get("monthly_cash_flows") or []
    return -flows[1] if len(flows) > 1 and flows[1] < 0 else 0.0


# ── serialization ───────────────────────────────────────────────────────────

def _row_json(row: OwnedBatchRow, current_hash: Optional[str] = None) -> dict:
    analysis = _j(row.analysis)
    return {
        "id": row.id, "row_number": row.row_number, "include": row.include, "status": row.status,
        "inputs": _j(row.inputs, {}), "overrides": _j(row.overrides, {}), "flags": _j(row.flags, []),
        "estimates": _j(row.estimates), "analysis": analysis,
        "dollars_at_stake": row_dollars_at_stake(analysis) if analysis else None,
        "rationale": row.rationale,
        "rationale_stale": bool(row.rationale and current_hash and row.rationale_assumptions != current_hash),
        "writeup": row.writeup, "writeup_status": row.writeup_status,
        "error": row.error, "debited": row.debited,
    }


def _statement_json(st: OwnedStatement) -> dict:
    ex = _j(st.extracted, {}) or {}
    stale = bool(st.statement_date and (datetime.utcnow() - st.statement_date).days > STALE_STATEMENT_DAYS)
    return {"id": st.id, "row_id": st.row_id, "file_name": st.file_name, "status": st.status,
            "match_tier": st.match_tier, "covers": st.covers, "confirmed": st.confirmed,
            "extracted": ex, "confirmed_values": _j(st.confirmed_values),
            "statement_date": st.statement_date.date().isoformat() if st.statement_date else None,
            "stale": stale, "stale_days": (datetime.utcnow() - st.statement_date).days if st.statement_date else None,
            "has_file": st.file_bytes is not None}


def _batch_json(db: Session, batch: OwnedBatch, user: User) -> dict:
    tenant = db.query(Tenant).filter_by(id=batch.tenant_id).first() if batch.tenant_id else None
    a = _assumptions(batch, tenant)
    h = _assumptions_hash(a)
    rows = [_row_json(r, h) for r in batch.rows]
    statements = db.query(OwnedStatement).filter_by(batch_id=batch.id).order_by(OwnedStatement.id).all()
    runnable = [r for r in batch.rows if r.include and r.status in RUNNABLE_STATUSES]
    dup = None
    if batch.address_hash and batch.kind == "batch":
        other = (db.query(OwnedBatch)
                   .filter(OwnedBatch.id != batch.id, OwnedBatch.tenant_id == batch.tenant_id,
                           OwnedBatch.address_hash == batch.address_hash,
                           OwnedBatch.created_at >= datetime.utcnow() - timedelta(days=30))
                   .order_by(OwnedBatch.created_at.desc()).first())
        if other:
            dup = {"batch_id": other.id, "created_at": other.created_at.isoformat()}
    return {
        "id": batch.id, "kind": batch.kind, "file_name": batch.file_name,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "status": _batch_status(batch), "assumptions": asdict(a), "rows": rows,
        "statements": [_statement_json(s) for s in statements],
        "unconfirmed_statements": sum(1 for s in statements if not s.confirmed and s.status == "read" and s.row_id),
        "token_cost_per_property": hold_cost(),
        "runnable_count": len(runnable), "tokens_needed": len(runnable) * hold_cost(),
        "balance": user.token_balance or 0, "duplicate_of": dup,
    }


def _batch_status(batch: OwnedBatch) -> str:
    statuses = [r.status for r in batch.rows]
    if any(s == "running" for s in statuses):
        return "Running"
    if all(s in FINAL_STATUSES for s in statuses):
        failed = sum(1 for s in statuses if s.startswith("failed"))
        return f"{failed} failed" if failed else "Complete"
    if any(s == "done" for s in statuses):
        return "Paused"
    return "Ready"


# ── endpoints: batches ──────────────────────────────────────────────────────

def _address_hash(rows: list) -> str:
    keys = sorted(f"{owned_import.normalize_address(r.address)['number']}|"
                  f"{owned_import.normalize_address(r.address)['street']}|{(r.fields.get('city') or '').lower()}"
                  for r in rows)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()


def _create_batch(db: Session, user: User, kind: str, file_name: str, imported: list) -> OwnedBatch:
    tenant = db.query(Tenant).filter_by(id=user.tenant_id).first() if user.tenant_id else None
    batch = OwnedBatch(tenant_id=user.tenant_id, user_id=user.id, kind=kind, file_name=file_name,
                       assumptions=(tenant.owned_defaults if tenant and tenant.owned_defaults else "{}"),
                       address_hash=_address_hash(imported))
    db.add(batch)
    db.flush()
    for r in imported:
        status = "skipped_pending" if r.skipped_pending else "queued"
        db.add(OwnedBatchRow(batch_id=batch.id, row_number=r.row_number, include=not r.skipped_pending,
                             status=status, inputs=_dump(r.fields), overrides="{}", flags=_dump(r.flags)))
    db.commit()
    db.refresh(batch)
    return batch


@router.post("/api/owned/batches")
async def upload_batch(file: UploadFile = File(...), loans_file: Optional[UploadFile] = File(None),
                       file_name: Optional[str] = Form(None),
                       user: User = Depends(require_owned_access), db: Session = Depends(get_db)):
    data = await file.read()
    if len(data) > owned_import.MAX_BYTES:
        raise HTTPException(413, "File is larger than 2 MB")
    try:
        rows = owned_import.parse_inventory(data)
        unmatched = []
        if loans_file is not None:
            loans_data = await loans_file.read()
            if len(loans_data) > owned_import.MAX_BYTES:
                raise HTTPException(413, "Loans file is larger than 2 MB")
            unmatched = owned_import.apply_loans(rows, owned_import.parse_loans(loans_data))
    except owned_import.ImportError_ as e:
        raise HTTPException(e.status, {"message": e.message, "missing": e.missing})
    batch = _create_batch(db, user, "batch", file_name or file.filename or "inventory.csv", rows)
    out = _batch_json(db, batch, user)
    out["unmatched_loans"] = unmatched
    return out


class SingleRunRequest(BaseModel):
    address: str = Field(min_length=3, max_length=300)
    city: str = Field(min_length=1, max_length=120)
    state: str = Field("MI", min_length=2, max_length=2)
    portfolio: Optional[str] = None
    beds: Optional[float] = Field(None, ge=0, le=20)
    baths: Optional[float] = Field(None, ge=0, le=20)
    sqft: Optional[float] = Field(None, ge=100, le=20000)
    year_built: Optional[int] = Field(None, ge=1800, le=2030)
    arv: Optional[float] = Field(None, gt=0, le=10_000_000)
    rehab_quote: Optional[float] = Field(None, ge=0, le=500_000)
    annual_tax: Optional[float] = Field(None, ge=0, le=100_000)
    annual_insurance: Optional[float] = Field(None, ge=0, le=20_000)
    loan_payoff: Optional[float] = Field(None, ge=0, le=2_000_000)
    loan_rate: Optional[float] = Field(None, ge=0, le=25)
    loan_pi: Optional[float] = Field(None, ge=0, le=20_000)
    investor_payback: Optional[float] = Field(None, ge=0, le=5_000_000)
    cost_basis: Optional[float] = Field(None, ge=0, le=5_000_000)
    notes: Optional[str] = Field(None, max_length=4000)


@router.post("/api/owned/single")
async def create_single(req: SingleRunRequest, user: User = Depends(require_owned_access),
                        db: Session = Depends(get_db)):
    if (req.loan_payoff or 0) > 0 and (not req.loan_rate or not req.loan_pi):
        raise HTTPException(422, "A loan payoff above $0 requires the interest rate and monthly P&I")
    fields = {"address_line": req.address, "city": req.city, "state": req.state.upper(), "portfolio": req.portfolio,
              "status": "Vacant", "arv": req.arv, "rehab_quote": req.rehab_quote or None,
              "annual_tax": req.annual_tax, "annual_insurance": req.annual_insurance,
              "loan_payoff": req.loan_payoff, "loan_rate": req.loan_rate, "loan_pi": req.loan_pi,
              "investor_payback": req.investor_payback, "cost_basis": req.cost_basis,
              "beds": req.beds, "baths": req.baths, "sqft": req.sqft, "year_built": req.year_built,
              "notes": req.notes, "lc_forfeiture_history": bool(owned_import.FORFEITURE_RE.search(req.notes or ""))}
    imported = owned_import.ImportedRow(1, fields)
    imported.flags = owned_import._row_flags(fields, date.today())
    batch = _create_batch(db, user, "single", req.address, [imported])
    return _batch_json(db, batch, user)


@router.get("/api/owned/batches")
async def list_batches(user: User = Depends(require_owned_access), db: Session = Depends(get_db)):
    q = db.query(OwnedBatch)
    if user.role != "superadmin":
        q = q.filter_by(tenant_id=user.tenant_id)
    out = []
    for b in q.order_by(OwnedBatch.created_at.desc()).limit(200).all():
        runner = db.query(User).filter_by(id=b.user_id).first()
        out.append({"id": b.id, "kind": b.kind, "file_name": b.file_name,
                    "created_at": b.created_at.isoformat() if b.created_at else None,
                    "run_by": (runner.full_name or runner.email) if runner else None,
                    "status": _batch_status(b), "properties": len(b.rows),
                    "done": sum(1 for r in b.rows if r.status == "done"),
                    "tokens_spent": sum(r.debited or 0 for r in b.rows)})
    return out


@router.get("/api/owned/batches/{batch_id}")
async def get_batch(batch_id: int, user: User = Depends(require_owned_access), db: Session = Depends(get_db)):
    batch = _get_batch(db, batch_id, user)
    _reset_stuck(db, batch)
    _cleanup_statement_files(db)
    db.refresh(batch)
    return _batch_json(db, batch, user)


class RowPatch(BaseModel):
    include: Optional[bool] = None
    overrides: Optional[dict] = None


@router.patch("/api/owned/batches/{batch_id}/rows/{row_id}")
async def patch_row(batch_id: int, row_id: int, req: RowPatch, user: User = Depends(require_owned_access),
                    db: Session = Depends(get_db)):
    batch = _get_batch(db, batch_id, user)
    row = _get_row(db, batch, row_id)
    if row.status == "running":
        raise HTTPException(409, "This property is running")
    if req.include is not None:
        row.include = req.include
        if req.include and row.status in ("skipped_pending", "excluded"):
            row.status = "queued"
        elif not req.include and row.status == "queued":
            row.status = "excluded"
    if req.overrides is not None:
        merged = {**(_j(row.overrides, {}) or {}), **req.overrides}
        row.overrides = _dump(_validate_overrides(merged))
        flags = [f for f in (_j(row.flags, []) or [])
                 if not (f == "No loan entered" and merged.get("loan_payoff"))
                 and not (f == "No cost basis" and merged.get("cost_basis") is not None)]
        row.flags = _dump(flags)
    db.commit()
    tenant = db.query(Tenant).filter_by(id=batch.tenant_id).first() if batch.tenant_id else None
    return _row_json(row, _assumptions_hash(_assumptions(batch, tenant)))


@router.put("/api/owned/batches/{batch_id}/assumptions")
async def put_assumptions(batch_id: int, body: dict, user: User = Depends(require_owned_access),
                          db: Session = Depends(get_db)):
    batch = _get_batch(db, batch_id, user)
    clean = _validate_assumptions(body)
    batch.assumptions = _dump(clean)
    tenant = db.query(Tenant).filter_by(id=batch.tenant_id).first() if batch.tenant_id else None
    if tenant is not None:
        tenant.owned_defaults = _dump(clean)      # remembered per tenant (D12)
    db.commit()
    return {"assumptions": asdict(_assumptions(batch, tenant))}


# ── endpoints: run one property (SSE) ───────────────────────────────────────

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


@router.post("/api/owned/batches/{batch_id}/rows/{row_id}/run")
async def run_row(batch_id: int, row_id: int, user: User = Depends(require_owned_access),
                  db: Session = Depends(get_db)):
    batch = _get_batch(db, batch_id, user)
    row = _get_row(db, batch, row_id)
    if not row.include:
        raise HTTPException(409, "This property is not included in the run")
    cost = hold_cost()
    is_super = user.role == "superadmin"
    if not is_super and (user.token_balance or 0) < cost:
        raise HTTPException(402, f"Need {cost} tokens (you have {user.token_balance or 0})")
    if batch.kind == "single" and not is_super:
        s = _server()
        today_start = datetime.combine(date.today(), datetime.min.time())
        usage_start = max(today_start, user.usage_reset_at or today_start)
        count = db.query(Analysis).filter(Analysis.user_id == user.id, Analysis.created_at >= usage_start).count()
        tenant = db.query(Tenant).filter_by(id=user.tenant_id).first() if user.tenant_id else None
        limit = tenant.daily_limit if tenant else 999
        if count >= limit:
            raise HTTPException(429, f"Daily limit of {limit} analyses reached. Resets at midnight.")
    # E1 double-run guard: only one request can move this row to running.
    claimed = db.execute(update(OwnedBatchRow)
                         .where(OwnedBatchRow.id == row.id, OwnedBatchRow.status.in_(RUNNABLE_STATUSES))
                         .values(status="running", started_at=datetime.utcnow(), error=None)).rowcount
    db.commit()
    if claimed != 1:
        raise HTTPException(409, "This property is already running or finished")
    if not is_super:
        user.token_balance = (user.token_balance or 0) - cost
        db.query(OwnedBatchRow).filter_by(id=row.id).update({"debited": cost})
    if batch.kind == "single":
        db.add(Analysis(user_id=user.id, tenant_id=user.tenant_id, address=batch.file_name,
                        analysis_type="owned_single"))
    db.commit()
    return StreamingResponse(_run_stream(batch.id, row.id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _fail(db: Session, row: OwnedBatchRow, message: str, refund: bool) -> str:
    if refund:
        _refund(db, row)
        row.status = "failed_refunded"
    else:
        row.status = "failed_charged"
    row.error = message
    row.finished_at = datetime.utcnow()
    db.commit()
    return _sse({"error": message, "refunded": refund, "done": True, "status": row.status})


def _run_stream(batch_id: int, row_id: int):
    db = database.SessionLocal()
    narrative_started = False
    try:
        row = db.query(OwnedBatchRow).filter_by(id=row_id).first()
        batch = row.batch
        tenant = db.query(Tenant).filter_by(id=batch.tenant_id).first() if batch.tenant_id else None
        a = _assumptions(batch, tenant)
        s = _server()
        # 1. Estimate (DATA) unless the user supplied every market value.
        est = _j(row.estimates) or {}
        if _needs_estimate(row) and not est:
            yield _sse({"status": "estimating"})
            try:
                est = _estimate(row)
            except Exception as e:
                yield _fail(db, row, f"Couldn't look up market data ({type(e).__name__}). Tokens refunded.", True)
                return
            problem = _validate_estimate(row, est)
            if problem:
                row.estimates = _dump(est)
                yield _fail(db, row, problem, True)
                return
            row.estimates = _dump(est)
            db.commit()
        # 2. Compute (pure Python).
        yield _sse({"status": "computing"})
        try:
            analysis = owned_asset.analyze(_property_inputs(db, row, est), a)
        except owned_asset.MissingInputError as e:
            yield _fail(db, row, f"{e}. Tokens refunded.", True)
            return
        row.analysis = _dump(analysis)
        db.commit()
        yield _sse({"analysis": analysis})
        # 3. Short rationale (no web search, E2).
        yield _sse({"status": "writing"})
        parts = []
        try:
            client = s.get_client()
            with client.messages.stream(model=s.MODEL, max_tokens=800, system=RATIONALE_SYSTEM,
                                        messages=[{"role": "user", "content": _rationale_prompt(analysis)}]) as stream:
                for event in stream:
                    if getattr(event, "type", None) == "content_block_delta":
                        text = getattr(getattr(event, "delta", None), "text", None)
                        if text:
                            narrative_started = True
                            parts.append(text)
                            yield _sse({"text": text})
        except Exception:
            if not narrative_started:
                row.analysis = None
                yield _fail(db, row, "The report couldn't be written. Tokens refunded.", True)
            else:
                row.rationale = "".join(parts)
                yield _fail(db, row, "Report incomplete. Tokens were not refunded because the analysis had started.", False)
            return
        row.rationale = "".join(parts)
        row.rationale_assumptions = _assumptions_hash(a)
        row.status = "done"
        row.finished_at = datetime.utcnow()
        db.commit()
        yield _sse({"done": True, "status": "done"})
        _maybe_send_summary(db, batch_id)
    finally:
        db.close()


# ── endpoints: free recalculation, full write-up ────────────────────────────

@router.post("/api/owned/batches/{batch_id}/recalculate")
async def recalculate(batch_id: int, user: User = Depends(require_owned_access), db: Session = Depends(get_db)):
    """D11: rerun the engine on stored DATA inputs; free."""
    batch = _get_batch(db, batch_id, user)
    tenant = db.query(Tenant).filter_by(id=batch.tenant_id).first() if batch.tenant_id else None
    a = _assumptions(batch, tenant)
    n = 0
    for row in batch.rows:
        if row.status == "done":
            row.analysis = _dump(owned_asset.analyze(_property_inputs(db, row, _j(row.estimates) or {}), a))
            n += 1
    db.commit()
    return {"recalculated": n, **_batch_json(db, batch, user)}


@router.post("/api/owned/batches/{batch_id}/rows/{row_id}/writeup")
async def full_writeup(batch_id: int, row_id: int, user: User = Depends(require_owned_access),
                       db: Session = Depends(get_db)):
    batch = _get_batch(db, batch_id, user)
    row = _get_row(db, batch, row_id)
    if row.status != "done" or not row.analysis:
        raise HTTPException(409, "Run the analysis first")
    if row.writeup:
        return {"writeup": row.writeup, "cached": True}
    busy = (db.query(OwnedBatchRow).join(OwnedBatch)
              .filter(OwnedBatch.user_id == user.id, OwnedBatchRow.writeup_status == "running").first())
    if busy:
        raise HTTPException(409, "Another write-up is in progress")
    row.writeup_status = "running"
    db.commit()
    return StreamingResponse(_writeup_stream(row.id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _writeup_stream(row_id: int):
    db = database.SessionLocal()
    try:
        row = db.query(OwnedBatchRow).filter_by(id=row_id).first()
        s = _server()
        analysis = _j(row.analysis, {})
        slim = {k: v for k, v in analysis.items() if k != "assumptions"}
        for sc in slim.get("scenarios", []):
            sc.pop("monthly_cash_flows", None)
        parts = []
        try:
            client = s.get_client()
            with client.messages.stream(model=s.MODEL, max_tokens=s.MAX_TOKENS, system=WRITEUP_SYSTEM,
                                        messages=[{"role": "user", "content": json.dumps(slim, default=str)}],
                                        tools=s.WEB_SEARCH_TOOL) as stream:
                for event in stream:
                    if getattr(event, "type", None) == "content_block_delta":
                        text = getattr(getattr(event, "delta", None), "text", None)
                        if text:
                            parts.append(text)
                            yield _sse({"text": text})
        except Exception:
            row.writeup_status = None
            db.commit()
            yield _sse({"error": "The write-up couldn't be generated. Try again.", "done": True})
            return
        row.writeup = "".join(parts)
        row.writeup_status = "done"
        db.commit()
        yield _sse({"done": True})
    finally:
        db.close()


# ── endpoints: loan statements (N3) ─────────────────────────────────────────

def _statement_media_type(name: str, declared: Optional[str]) -> Optional[str]:
    lower = (name or "").lower()
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    return declared if declared in ("application/pdf", "image/jpeg", "image/png") else None


def _extract_statement(data: bytes, media_type: str) -> dict:
    s = _server()
    b64 = base64.standard_b64encode(data).decode()
    block = ({"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
             if media_type == "application/pdf" else
             {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})
    client = s.get_client()
    resp = client.messages.create(model=s.MODEL, max_tokens=1000, system=STATEMENT_SYSTEM,
                                  messages=[{"role": "user", "content": [block, {"type": "text", "text": "Extract."}]}],
                                  timeout=ESTIMATE_TIMEOUT_S)
    data_json = _extract_json(_last_text(resp))
    if data_json is None:
        raise ValueError("not JSON")
    return data_json


def _match_statement(batch: OwnedBatch, addresses: list) -> tuple[Optional[OwnedBatchRow], str]:
    if len(addresses) > 1:
        return None, "multi"
    if not addresses:
        return None, "none"
    target = addresses[0]
    best, best_tier = None, "none"
    for row in batch.rows:
        f = _j(row.inputs, {}) or {}
        # Statements print "123 Main St, City, MI 48000": split the city off when present.
        parts = [p.strip() for p in target.split(",")]
        line, city = parts[0], (parts[1] if len(parts) > 1 else "")
        tier = owned_import.match_tier(line, city, f.get("address_line", ""), f.get("city", ""))
        if tier == "exact":
            return row, "exact"
        if tier == "likely" and best is None:
            best, best_tier = row, "likely"
    return best, best_tier


@router.post("/api/owned/batches/{batch_id}/statements")
async def upload_statement(batch_id: int, file: UploadFile = File(...),
                           user: User = Depends(require_owned_access), db: Session = Depends(get_db)):
    batch = _get_batch(db, batch_id, user)
    count = db.query(OwnedStatement).filter_by(batch_id=batch.id).count()
    name = file.filename or "statement"
    if count >= MAX_STATEMENTS_PER_BATCH:
        raise HTTPException(422, f"Over the {MAX_STATEMENTS_PER_BATCH}-statement limit: {name} was not added")
    data = await file.read()
    media = _statement_media_type(name, file.content_type)
    st = OwnedStatement(batch_id=batch.id, file_name=name, media_type=media)
    db.add(st)
    if media is None:
        st.status = "not_statement"
    elif len(data) > MAX_STATEMENT_BYTES:
        st.status = "too_large"
    elif media == "application/pdf" and b"/Encrypt" in data[:200000]:
        st.status = "password"
    else:
        st.file_bytes = data
        try:
            ex = _extract_statement(data, media)
        except Exception:
            ex = None
        if ex is None or ex.get("readable") is False:
            st.status = "unreadable"
        elif ex.get("is_loan_statement") is False:
            st.status = "not_statement"
        else:
            st.status = "read"
            st.extracted = _dump(ex)
            try:
                st.statement_date = datetime.strptime(ex.get("statement_date") or "", "%Y-%m-%d")
            except ValueError:
                st.statement_date = None
            addresses = [x for x in (ex.get("property_addresses") or []) if x]
            st.covers = max(1, len(addresses))
            row, tier = _match_statement(batch, addresses)
            st.match_tier = tier
            if row is not None:
                _assign(db, st, row)
    db.commit()
    return _statement_json(st)


def _assign(db: Session, st: OwnedStatement, row: OwnedBatchRow):
    """Newest statement per property wins; older unconfirmed ones are superseded."""
    others = db.query(OwnedStatement).filter(OwnedStatement.row_id == row.id, OwnedStatement.id != st.id,
                                             OwnedStatement.status == "read").all()
    for o in others:
        if o.statement_date and st.statement_date and o.statement_date > st.statement_date:
            st.status = "superseded"
            return
    for o in others:
        if not o.confirmed:
            o.status = "superseded"
    st.row_id = row.id


class AssignRequest(BaseModel):
    row_id: int


@router.post("/api/owned/statements/{statement_id}/assign")
async def assign_statement(statement_id: int, req: AssignRequest, user: User = Depends(require_owned_access),
                           db: Session = Depends(get_db)):
    st = db.query(OwnedStatement).filter_by(id=statement_id).first()
    if not st:
        raise HTTPException(404, "Statement not found")
    batch = _get_batch(db, st.batch_id, user)
    row = _get_row(db, batch, req.row_id)
    st.match_tier = "manual"
    _assign(db, st, row)
    db.commit()
    return _statement_json(st)


class ConfirmRequest(BaseModel):
    payoff: float = Field(ge=0, le=2_000_000)
    rate: Optional[float] = Field(None, ge=0, le=25)
    pi: Optional[float] = Field(None, ge=0, le=20_000)
    confirm_stale: bool = False


@router.post("/api/owned/statements/{statement_id}/confirm")
async def confirm_statement(statement_id: int, req: ConfirmRequest, user: User = Depends(require_owned_access),
                            db: Session = Depends(get_db)):
    st = db.query(OwnedStatement).filter_by(id=statement_id).first()
    if not st:
        raise HTTPException(404, "Statement not found")
    _get_batch(db, st.batch_id, user)
    if st.row_id is None:
        raise HTTPException(409, "Assign this statement to a property first")
    if req.payoff > 0 and (req.rate is None or req.pi is None):
        raise HTTPException(422, "A payoff above $0 requires the interest rate and monthly P&I")
    if st.statement_date and (datetime.utcnow() - st.statement_date).days > STALE_STATEMENT_DAYS and not req.confirm_stale:
        raise HTTPException(409, "This statement is more than 60 days old. Use 'Confirm anyway' to accept it.")
    st.confirmed = True
    st.status = "confirmed"
    st.confirmed_values = _dump({"payoff": req.payoff, "rate": req.rate, "pi": req.pi})
    st.confirmed_at = datetime.utcnow()
    st.file_bytes = None                # deleted once confirmed (N-design D5)
    row = db.query(OwnedBatchRow).filter_by(id=st.row_id).first()
    if row is not None:
        row.flags = _dump([f for f in (_j(row.flags, []) or []) if f != "No loan entered"])
    db.commit()
    return _statement_json(st)


@router.delete("/api/owned/statements/{statement_id}")
async def remove_statement(statement_id: int, user: User = Depends(require_owned_access),
                           db: Session = Depends(get_db)):
    st = db.query(OwnedStatement).filter_by(id=statement_id).first()
    if not st:
        raise HTTPException(404, "Statement not found")
    _get_batch(db, st.batch_id, user)
    db.delete(st)
    db.commit()
    return {"ok": True}


@router.get("/api/owned/statements/{statement_id}/file")
async def statement_file(statement_id: int, user: User = Depends(require_owned_access),
                         db: Session = Depends(get_db)):
    st = db.query(OwnedStatement).filter_by(id=statement_id).first()
    if not st:
        raise HTTPException(404, "Statement not found")
    _get_batch(db, st.batch_id, user)
    if st.file_bytes is None:
        raise HTTPException(410, "This file was deleted after confirmation or 24 hours")
    return Response(content=st.file_bytes, media_type=st.media_type or "application/octet-stream",
                    headers={"Cache-Control": "no-store"})
