"""Unit tests for web/owned_asset.py (spec S1 + decisions M1-M10, S1a, S1b).

Pure math: no web app, no model calls."""
import math
from dataclasses import replace

import pytest

from owned_asset import (
    Assumptions, PropertyInputs, Loan, MissingInputError, analyze, break_even, confidence,
    evaluate, irr_annual, npv, rank, rehab_land_contract, rehab_rent, rehab_sell, resolve,
    risk_adjusted, risk_score, sell_as_is,
)


def _prop(**kw):
    base = dict(address="1 Test St", city="Harper Woods", beds=3, baths=1, sqft=1100, year_built=1965,
                as_is_value=80000, arv=135000, market_rent=1450, rehab_budget=20000, annual_tax=3600,
                annual_insurance=1200, months_of_supply=3, dom_as_is_days=45, dom_renovated_days=30,
                appreciation_pct=3.0)
    base.update(kw)
    return PropertyInputs(**base)


A = Assumptions()


# ── helpers ────────────────────────────────────────────────────────────────

class TestMathHelpers:
    def test_npv_at_zero_rate_is_sum(self):
        assert npv([0, 100, 200], 0) == 300

    def test_npv_discounts_annually_compounded(self):
        assert npv([0] * 12 + [110], 10) == pytest.approx(100, rel=1e-9)

    def test_irr_known_series(self):
        flows = [-1000] + [0] * 11 + [1100]
        irr, reason = irr_annual(flows)
        assert reason is None
        assert irr == pytest.approx(10.0, abs=0.01)

    def test_irr_no_sign_change(self):
        irr, reason = irr_annual([0, 100, 200])
        assert irr is None and "sign" in reason

    def test_loan_amortizes(self):
        loan = Loan(100000, 6.0, 599.55)
        for _ in range(12):
            loan.step()
        assert loan.balance == pytest.approx(98772, abs=5)
        assert not loan.negative_amortization

    def test_negative_amortization_flagged(self):
        assert Loan(100000, 6.0, 300).negative_amortization


# ── resolution and defaults ─────────────────────────────────────────────────

class TestResolve:
    def test_requires_a_value(self):
        with pytest.raises(MissingInputError):
            resolve(_prop(as_is_value=None, arv=None), A)

    def test_contingency_15_for_newer_home_with_quote(self):
        r = resolve(_prop(year_built=1965), A)
        assert r.contingency_pct == 15.0 and r.rehab_total == pytest.approx(23000)

    def test_contingency_20_for_pre_1960_or_unknown_year(self):
        assert resolve(_prop(year_built=1952), A).contingency_pct == 20.0
        assert resolve(_prop(year_built=None), A).contingency_pct == 20.0

    def test_rehab_duration_one_month_per_15k(self):
        assert resolve(_prop(rehab_budget=20000), A).rehab_months == 2
        assert resolve(_prop(rehab_budget=15000), A).rehab_months == 1
        assert resolve(_prop(rehab_budget=0), A).rehab_months == 0

    def test_appreciation_capped_at_4(self):
        assert resolve(_prop(appreciation_pct=7.0), A).appreciation_pct == 4.0

    def test_missing_values_are_tagged_default(self):
        r = resolve(_prop(market_rent=None, annual_insurance=None), A)
        assert r.sources["market_rent"] == "DEFAULT"
        assert r.sources["annual_insurance"] == "DEFAULT"
        assert r.annual_insurance == A.insurance_default_annual


# ── scenario A: sell as-is ──────────────────────────────────────────────────

class TestSellAsIs:
    def test_timeline_and_proceeds(self):
        r = resolve(_prop(investor_payback=10000), A)
        s = sell_as_is(r)
        assert s.timeline_months == 2 + 1                # 45 days -> 2 months + 1 to close
        expected_net = 80000 * (1 - 0.05) - 10000         # 3% + 2% (M10), payback (M8)
        assert s.extras["net_sale"] == pytest.approx(expected_net)
        assert sum(s.flows) < expected_net               # carry deducted

    def test_loan_payoff_uses_amortized_balance(self):
        r = resolve(_prop(loan_payoff=40000, loan_rate_pct=6.0, loan_pi=500), A)
        s = sell_as_is(r)
        assert s.extras["net_sale"] > 80000 * 0.95 - 40000  # balance paid down over the months

    def test_shortfall_when_underwater(self):
        r = resolve(_prop(loan_payoff=70000, loan_pi=500, loan_rate_pct=6, investor_payback=20000), A)
        assert sell_as_is(r).shortfall > 0


# ── scenario B: rehab & sell ────────────────────────────────────────────────

class TestRehabSell:
    def test_selling_costs_8_percent_without_concessions(self):
        r = resolve(_prop(months_of_supply=3), A)
        s = rehab_sell(r, starting_equity=80000)
        assert s.extras["net_sale"] == pytest.approx(135000 * 0.92)

    def test_concessions_when_supply_high_or_unknown(self):
        for mos in (5, None):
            r = resolve(_prop(months_of_supply=mos), A)
            assert rehab_sell(r, 80000).extras["net_sale"] == pytest.approx(135000 * 0.90)

    def test_financing_in_net_profit_not_npv_when_rates_equal(self):
        r = resolve(_prop(), A)
        s = rehab_sell(r, 80000)
        assert s.extras["financing_cost"] > 0
        assert sum(s.profit_flows) == pytest.approx(sum(s.flows) - s.extras["financing_cost"])

    def test_financing_spread_charged_in_npv_when_cost_of_capital_higher(self):
        r_eq = resolve(_prop(), A)
        r_hi = resolve(_prop(), replace(A, cost_of_capital_pct=12.0))
        assert sum(rehab_sell(r_hi, 80000).flows) < sum(rehab_sell(r_eq, 80000).flows)

    def test_disqualified_when_profit_under_10_percent(self):
        r = resolve(_prop(arv=100000, rehab_budget=18000), A)
        assert rehab_sell(r, 80000).disqualifier is not None


# ── scenario C: rehab & rent ────────────────────────────────────────────────

class TestRehabRent:
    def test_no_loan_means_no_dscr_and_terminal_sale(self):
        r = resolve(_prop(), A)
        s = rehab_rent(r, 80000)
        assert s.extras["dscr"] is None
        assert s.liquidity_label == "assumed sale"
        terminal_price = 135000 * 1.03 ** 3
        assert s.extras["terminal_value"] == pytest.approx(terminal_price * 0.92)

    def test_low_dscr_disqualifies(self):
        r = resolve(_prop(loan_payoff=100000, loan_rate_pct=8, loan_pi=900), A)
        s = rehab_rent(r, 80000)
        assert s.extras["dscr"] < 1.15 and "DSCR" in s.disqualifier

    def test_rental_stress_lowers_npv(self):
        r = resolve(_prop(), A)
        base = npv(rehab_rent(r, 80000).flows, A.discount_rate_pct)
        stressed = npv(rehab_rent(r, 80000, {"vacancy_pct": 15.0, "major_repair_month": 18,
                                              "major_repair_cost": 5000.0}).flows, A.discount_rate_pct)
        assert stressed < base


# ── scenario D: land contract ───────────────────────────────────────────────

class TestLandContract:
    def test_terms_and_closing_cash(self):
        r = resolve(_prop(loan_payoff=10000, loan_rate_pct=6, loan_pi=200), A)
        s = rehab_land_contract(r)
        price = 135000 * 1.08
        assert s.extras["lc_price"] == pytest.approx(price)
        assert s.extras["down_payment"] == pytest.approx(price * 0.10)
        # no commission, 2% closing, loan paid off at closing (E7)
        assert s.extras["closing_cash"] < price * 0.10 - price * 0.02

    def test_investor_payback_not_at_closing_but_at_payoff(self):
        base = rehab_land_contract(resolve(_prop(investor_payback=0), A))
        paid = rehab_land_contract(resolve(_prop(investor_payback=15000), A))
        close = base.extras["closing_month"]
        assert paid.flows[close] == pytest.approx(base.flows[close])     # M9: none at closing
        assert sum(paid.flows) == pytest.approx(sum(base.flows) - 15000)  # deducted once overall

    def test_higher_default_probability_lowers_value(self):
        r = resolve(_prop(), A)
        low = npv(rehab_land_contract(r).flows, 8)
        high = npv(rehab_land_contract(r, {"lc_default_prob_pct": 40.0}).flows, 8)
        assert high < low

    def test_performing_and_default_values_reported(self):
        s = rehab_land_contract(resolve(_prop(), A))
        assert s.extras["performing_npv"] > s.extras["default_npv"]


# ── risk, ranking, confidence ───────────────────────────────────────────────

class TestRiskAndRanking:
    def test_risk_score_bounds(self):
        assert risk_score(100000, [100000, 100000], 0) == 1
        assert risk_score(100000, [0], 0) == 10
        assert risk_score(100000, [50000], 0) == round(1 + 9 * 0.5)
        assert risk_score(100000, [0], 3) == 10

    def test_risk_adjusted_penalizes_losses(self):
        assert risk_adjusted(100000, 10) == pytest.approx(50000)
        assert risk_adjusted(-100000, 10) == pytest.approx(-200000)
        assert risk_adjusted(-100000, 10) < risk_adjusted(-100000, 1)

    def test_disqualified_strategies_rank_last(self):
        p = _prop(arv=100000, rehab_budget=18000)
        out = analyze(p, A, with_break_even=False)
        ranking = out["recommendation"]["ranking"]
        assert ranking[-1] == "rehab_sell"
        assert out["recommendation"]["strategy"] != "rehab_sell"

    def test_close_call_prefers_lower_peak_capital(self):
        ev = evaluate(_prop(), A)
        sc = ev["scenarios"]
        # Force a near tie between two strategies with different peak capital.
        sc["sell_as_is"]["risk_adjusted_score"] = 100000
        sc["rehab_land_contract"]["risk_adjusted_score"] = 101000
        sc["rehab_sell"]["risk_adjusted_score"] = 0
        sc["rehab_rent"]["risk_adjusted_score"] = 0
        d = rank(ev, A)
        assert d["close_call"] is True
        assert d["strategy"] == "sell_as_is"

    def test_goal_total_return_keeps_score_order_in_close_call(self):
        ev = evaluate(_prop(), A)
        sc = ev["scenarios"]
        sc["sell_as_is"]["risk_adjusted_score"] = 100000
        sc["rehab_land_contract"]["risk_adjusted_score"] = 101000
        sc["rehab_sell"]["risk_adjusted_score"] = 0
        sc["rehab_rent"]["risk_adjusted_score"] = 0
        d = rank(ev, replace(A, owner_goal="maximize_total_return"))
        assert d["strategy"] == "rehab_land_contract"


class TestConfidence:
    def test_high_when_facts_known(self):
        assert confidence(resolve(_prop(cost_basis=90000), A)) == "High"

    def test_low_when_arv_or_rent_missing(self):
        assert confidence(resolve(_prop(market_rent=None), A)) == "Low"
        assert confidence(resolve(_prop(arv=None), A)) == "Low"

    def test_low_when_property_facts_missing(self):
        assert confidence(resolve(_prop(beds=None), A)) == "Low"

    def test_medium_when_more_than_five_defaults(self):
        p = _prop(dom_as_is_days=None, dom_renovated_days=None, appreciation_pct=None,
                  months_of_supply=None, annual_insurance=None, cost_basis=None)
        assert confidence(resolve(p, A)) == "Medium"

    def test_stale_statement_caps_medium(self):
        assert confidence(resolve(_prop(cost_basis=1, stale_loan_statement=True), A)) == "Medium"


# ── full analysis ───────────────────────────────────────────────────────────

class TestAnalyze:
    def test_output_shape(self):
        out = analyze(_prop(), A, with_break_even=False)
        assert set(out) >= {"property", "assumptions", "flags", "scenarios", "recommendation"}
        assert [s["strategy"] for s in out["scenarios"]] == [
            "sell_as_is", "rehab_sell", "rehab_rent", "rehab_land_contract"]
        for s in out["scenarios"]:
            assert set(s["stress_tests"]) == {"base", "rehab_overrun", "arv_minus_10", "zero_appreciation",
                                              "rental_stress", "lc_default_40", "combined_downside"}
            assert 1 <= s["risk_score"] <= 10

    def test_deterministic(self):
        assert analyze(_prop(), A) == analyze(_prop(), A)

    def test_sell_as_is_liquidity_is_its_sale_month(self):
        out = analyze(_prop(), A, with_break_even=False)
        a = next(s for s in out["scenarios"] if s["strategy"] == "sell_as_is")
        assert a["months_to_liquidity"] == a["timeline_months"] and a["liquidity_label"] is None

    def test_payback_reduces_every_strategy(self):
        base = analyze(_prop(), A, with_break_even=False)
        paid = analyze(_prop(investor_payback=20000), A, with_break_even=False)
        for b, p in zip(base["scenarios"], paid["scenarios"]):
            assert p["npv"] < b["npv"]

    def test_shortfall_flag(self):
        out = analyze(_prop(loan_payoff=90000, loan_rate_pct=6, loan_pi=600, investor_payback=30000),
                      A, with_break_even=False)
        assert any("payback; short" in f["message"] for f in out["flags"])

    def test_servicer_downgrades_compliance_flag(self):
        out = analyze(_prop(), replace(A, lc_servicer="SGMS"), with_break_even=False)
        comp = [f for f in out["flags"] if f["type"] == "compliance"]
        assert comp and comp[0]["severity"] == "info" and "SGMS" in comp[0]["message"]

    def test_cost_basis_does_not_change_ranking(self):
        a = analyze(_prop(cost_basis=None), A, with_break_even=False)["recommendation"]["ranking"]
        b = analyze(_prop(cost_basis=500000), A, with_break_even=False)["recommendation"]["ranking"]
        assert a == b


class TestBreakEven:
    def test_thresholds_flip_the_winner(self):
        p = _prop()
        out = analyze(p, A)
        winner = out["recommendation"]["strategy"]
        for t in out["recommendation"]["break_even_triggers"]:
            assert t["threshold"] % 100 == 0
            assert t["new_winner"] != winner

    def test_holds_when_nothing_flips(self):
        # A property where selling as-is dominates everywhere within ±50% of rehab budget.
        p = _prop(arv=82000, as_is_value=80000, market_rent=500, rehab_budget=10000)
        triggers = break_even(p, A, "sell_as_is", {"rehab_budget": 10000})
        assert all(t["variable"] == "rehab_budget" for t in triggers)


class TestUnknownRehab:
    def test_unknown_rehab_assumes_full_arv_lift_not_zero(self):
        r = resolve(_prop(rehab_budget=None, as_is_value=90000, arv=110000), Assumptions())
        assert r.rehab_budget == 20000 and r.sources["rehab_budget"] == "DEFAULT"

    def test_known_rehab_kept(self):
        r = resolve(_prop(rehab_budget=5000, as_is_value=90000, arv=110000), Assumptions())
        assert r.rehab_budget == 5000


# ── waterfall reconciliation ────────────────────────────────────────────────

class TestWaterfall:
    def _check_all_scenarios(self, out):
        for s in out["scenarios"]:
            wf = s["details"]["waterfall"]
            last = wf[-1]
            assert last.get("total") is True
            assert last["amount"] == pytest.approx(round(s["net_profit"], 2), abs=1.0)
            non_total_sum = sum(row["amount"] for row in wf[:-1])
            assert non_total_sum == pytest.approx(last["amount"], abs=1.0)

    def test_waterfall_totals_reconcile_without_loan_or_payback(self):
        out = analyze(_prop(), A, with_break_even=False)
        self._check_all_scenarios(out)

    def test_waterfall_totals_reconcile_with_loan_and_investor_payback(self):
        out = analyze(_prop(loan_payoff=40000, loan_rate_pct=6.0, loan_pi=500, investor_payback=15000),
                      A, with_break_even=False)
        self._check_all_scenarios(out)


# ── vacancy carry (S1) ──────────────────────────────────────────────────────

class TestVacancyCarry:
    def test_carry_since_vacant_and_cost_basis_adjustment(self):
        out = analyze(_prop(months_vacant=9, cost_basis=100000), A, with_break_even=False)
        v = out["vacancy"]
        assert v["carry_since_vacant"] == pytest.approx(round(9 * v["monthly_carry"], 2), abs=0.01)
        assert v["cost_basis_adjusted"] == pytest.approx(100000 + v["carry_since_vacant"], abs=0.01)
        assert any(f["message"].startswith("Vacant 9 months") for f in out["flags"])
        for s in out["scenarios"]:
            assert s["profit_vs_cost_basis"] == pytest.approx(s["net_profit"] - v["cost_basis_adjusted"], abs=0.01)

    def test_no_vacant_flag_under_six_months(self):
        out = analyze(_prop(months_vacant=4), A, with_break_even=False)
        assert not any(f["message"].startswith("Vacant") for f in out["flags"])

    def test_cost_basis_adjusted_is_none_without_cost_basis(self):
        out = analyze(_prop(months_vacant=9, cost_basis=None), A, with_break_even=False)
        assert out["vacancy"]["cost_basis_adjusted"] is None


class TestVacancyBreakdown:
    def test_breakdown_keys_and_extra_holding_costs(self):
        out = analyze(_prop(security_monthly=50, other_holding_monthly=25,
                            loan_payoff=50000, loan_rate_pct=6.0, loan_pi=500), A, with_break_even=False)
        breakdown = out["vacancy"]["monthly_breakdown"]
        assert set(breakdown) == {"property_tax", "insurance", "utilities", "maintenance",
                                  "security", "grass_snow", "other", "loan_interest"}
        assert breakdown["security"] == 50
        assert breakdown["other"] == 25
        assert breakdown["loan_interest"] == pytest.approx(50000 * 6.0 / 1200.0, abs=0.01)


# ── flag suggestions ────────────────────────────────────────────────────────

class TestFlagSuggestions:
    def test_warning_and_critical_flags_all_have_suggestions(self):
        p = _prop(loan_payoff=90000, loan_rate_pct=0.0, loan_pi=0.0, investor_payback=30000,
                  lc_forfeiture_history=True, rehab_budget=60000, arv=135000)
        out = analyze(p, A, with_break_even=False)
        risky = [f for f in out["flags"] if f["severity"] in ("warning", "critical")]
        # sanity: this property is built to trip several distinct warning/critical flags
        assert len(risky) >= 3
        for f in risky:
            assert f.get("suggestion")
