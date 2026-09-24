"""Owned-asset exit engine.

Implements docs/specs/exit-strategy-analyzer-v1.md in code, with the
decisions recorded in docs/designs/owned-asset-exit-analysis.md (S1, S1a,
S1b, M1-M10). The model never computes these numbers: it only supplies DATA
inputs (comps, rent, DOM, appreciation) and writes the narrative from the
analysis this module returns.

Flow:
    PropertyInputs + Assumptions
        -> preflight()                 flags, confidence (M3, S1b)
        -> scenario models A-D         monthly cash flows, months 0..horizon
        -> metrics()                   net profit, NPV, IRR, peak capital,
                                       liquidity (M6), equity multiple (M1)
        -> stress tests (7)            NPV per condition
        -> risk score (S1a) + risk-adjusted score (M2)
        -> rank()                      disqualifiers, 5% tie-break
        -> break_even() (M7)           ARV / rent / rehab thresholds
        -> analyze() returns analysis_json (exact values; rounding is render-only)

All money is in dollars, rates in percent, months are integers from month 0 = today.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace, asdict
from typing import Optional

STRATEGIES = ["sell_as_is", "rehab_sell", "rehab_rent", "rehab_land_contract"]
STRATEGY_LABELS = {
    "sell_as_is": "Sell as-is",
    "rehab_sell": "Rehab & sell",
    "rehab_rent": "Rehab & rent",
    "rehab_land_contract": "Land contract",
}
EFFORT = {"sell_as_is": "Low", "rehab_sell": "Medium", "rehab_rent": "High", "rehab_land_contract": "Medium"}

# Inputs counted by the confidence rule (M3): property-specific only.
CONFIDENCE_INPUTS = [
    "as_is_value", "arv", "market_rent", "dom_as_is_days", "dom_renovated_days",
    "appreciation_pct", "months_of_supply", "rehab_budget", "beds", "baths", "sqft",
    "year_built", "annual_tax", "annual_insurance", "loan_payoff", "investor_payback", "cost_basis",
]


@dataclass
class Assumptions:
    """Spec Defaults table, with M10 selling costs (IRES standard)."""
    horizon_months: int = 36
    discount_rate_pct: float = 8.0
    cost_of_capital_pct: Optional[float] = None   # None = discount rate (M4)
    # Selling costs (M10)
    commission_pct: float = 6.0
    closing_pct: float = 2.0                        # includes MI transfer tax
    as_is_commission_pct: float = 3.0
    concessions_pct: float = 2.0                    # applied when months of supply > 4 (or unknown)
    concessions_supply_threshold: float = 4.0
    # Rehab
    contingency_pct: float = 15.0
    contingency_old_or_unverified_pct: float = 20.0
    rehab_dollars_per_month: float = 15000.0
    # Carry
    utilities_vacant_monthly: float = 250.0
    min_maintenance_monthly: float = 100.0
    insurance_default_annual: float = 1200.0
    cash_for_keys: float = 2000.0
    # Rental
    vacancy_pct: float = 8.0
    maintenance_pct: float = 8.0
    maintenance_post_rehab_pct: float = 5.0
    capex_pct: float = 5.0
    management_pct: float = 9.0
    lease_up_months: int = 1
    turnover_cost: float = 1500.0
    turnover_every_months: int = 24
    rent_growth_pct: float = 3.0
    tax_growth_pct: float = 3.0
    insurance_growth_pct: float = 6.0
    appreciation_default_pct: float = 3.0
    appreciation_cap_pct: float = 4.0
    # Land contract (spec model D; payback timing M9; default branch M5)
    lc_price_premium_pct: float = 8.0
    lc_down_pct: float = 10.0
    lc_rate_pct: float = 10.0
    lc_amort_years: int = 30
    lc_marketing_months: int = 2                    # 45 days, rounded up
    lc_default_prob_pct: float = 20.0
    lc_default_month: int = 14
    lc_resale_month: int = 20
    lc_legal_cost: float = 3500.0
    lc_repair_cost: float = 5000.0
    lc_servicing_monthly: float = 25.0
    lc_balloon_paid_prob_pct: float = 60.0
    lc_note_discount_pct: float = 15.0
    lc_servicer: Optional[str] = None               # e.g. "SGMS" downgrades the compliance flag (S1)
    # DOM defaults
    dom_as_is_default_days: int = 60
    dom_renovated_default_days: int = 45
    # Decision
    owner_goal: Optional[str] = None                # maximize_cash_now | maximize_total_return | monthly_income | minimize_effort
    close_call_pct: float = 5.0


@dataclass
class PropertyInputs:
    address: str
    city: str = ""
    state: str = "MI"
    occupancy: str = "vacant"                       # vacant | occupied
    lease_months_remaining: Optional[int] = None
    current_rent: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[float] = None
    year_built: Optional[int] = None
    as_is_value: Optional[float] = None
    arv: Optional[float] = None
    market_rent: Optional[float] = None
    dom_as_is_days: Optional[int] = None
    dom_renovated_days: Optional[int] = None
    appreciation_pct: Optional[float] = None
    months_of_supply: Optional[float] = None
    rehab_budget: Optional[float] = None
    annual_tax: Optional[float] = None
    annual_insurance: Optional[float] = None
    loan_payoff: float = 0.0
    loan_rate_pct: float = 0.0
    loan_pi: float = 0.0
    investor_payback: float = 0.0
    cost_basis: Optional[float] = None
    lc_forfeiture_history: bool = False
    stale_loan_statement: bool = False
    # Source tag per input: USER | DATA | DEFAULT (with optional detail)
    sources: dict = field(default_factory=dict)


class MissingInputError(ValueError):
    """Raised when a required input cannot be defaulted (e.g., no value at all)."""


# ── small helpers ────────────────────────────────────────────────────────────

def _months_from_days(days: float) -> int:
    return max(1, math.ceil(days / 30.0))


def discount_factor(month: int, annual_rate_pct: float) -> float:
    return (1 + annual_rate_pct / 100.0) ** (-month / 12.0)


def npv(flows: list[float], annual_rate_pct: float) -> float:
    return sum(cf * discount_factor(m, annual_rate_pct) for m, cf in enumerate(flows))


def irr_annual(flows: list[float]) -> tuple[Optional[float], Optional[str]]:
    """Annualized IRR (percent) of monthly flows via bisection (R11).

    Returns (None, reason) when IRR is not meaningful."""
    if not any(cf < 0 for cf in flows) or not any(cf > 0 for cf in flows):
        return None, "cash flows have no sign change"

    def f(monthly_rate: float) -> float:
        return sum(cf / (1 + monthly_rate) ** m for m, cf in enumerate(flows))

    lo, hi = -0.99, 1.0
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        return None, "IRR not found in the search range"
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = f(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    monthly = (lo + hi) / 2
    return ((1 + monthly) ** 12 - 1) * 100.0, None


class Loan:
    """Existing loan amortized from payoff balance, rate and monthly P&I."""

    def __init__(self, balance: float, rate_pct: float, pi: float):
        self.balance = max(0.0, balance)
        self.rate = rate_pct / 100.0 / 12.0
        self.pi = pi if self.balance > 0 else 0.0
        self.negative_amortization = self.balance > 0 and self.pi < self.balance * self.rate

    def step(self) -> float:
        """Advance one month; returns the payment made."""
        if self.balance <= 0:
            return 0.0
        interest = self.balance * self.rate
        payment = min(self.pi, self.balance + interest) if self.pi > 0 else interest
        self.balance = self.balance + interest - payment
        return payment


def _amortized_payment(principal: float, rate_pct: float, years: int) -> float:
    r = rate_pct / 100.0 / 12.0
    n = years * 12
    if principal <= 0:
        return 0.0
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** -n)


def _remaining_principal(principal: float, rate_pct: float, years: int, months_paid: int) -> float:
    r = rate_pct / 100.0 / 12.0
    pmt = _amortized_payment(principal, rate_pct, years)
    bal = principal
    for _ in range(months_paid):
        bal = bal * (1 + r) - pmt
    return max(0.0, bal)


# ── resolved inputs ─────────────────────────────────────────────────────────

@dataclass
class Resolved:
    """Inputs after defaults are applied; every value knows its source."""
    p: PropertyInputs
    a: Assumptions
    as_is_value: float
    arv: float
    market_rent: float
    dom_as_is_months: int
    dom_renovated_months: int
    appreciation_pct: float
    months_of_supply: Optional[float]
    rehab_budget: float
    rehab_total: float
    rehab_months: int
    contingency_pct: float
    annual_tax: float
    annual_insurance: float
    vacate_months: int
    cash_for_keys: float
    sources: dict


def resolve(p: PropertyInputs, a: Assumptions) -> Resolved:
    src = dict(p.sources)

    def tag(name, value, default_value):
        if value is None:
            src[name] = "DEFAULT"
            return default_value
        src.setdefault(name, "USER")
        return value

    arv = p.arv
    as_is = p.as_is_value
    if arv is None and as_is is None:
        raise MissingInputError("as_is_value or arv is required (DATA estimate or user input)")
    if p.rehab_budget is None and arv is not None and as_is is not None:
        # Unknown rehab is not free: assume it consumes the whole ARV lift (conservative).
        rehab_budget = max(0.0, arv - as_is)
        src["rehab_budget"] = "DEFAULT"
    else:
        rehab_budget = tag("rehab_budget", p.rehab_budget, 0.0)
    if arv is None:
        arv = as_is + rehab_budget
        src["arv"] = "DEFAULT"
    else:
        src.setdefault("arv", "USER")
    if as_is is None:
        as_is = max(0.0, arv - rehab_budget)
        src["as_is_value"] = "DEFAULT"
    else:
        src.setdefault("as_is_value", "USER")

    market_rent = p.market_rent
    if market_rent is None:
        market_rent = round(arv * 0.009, 2)       # conservative 0.9% rule fallback, DEFAULT
        src["market_rent"] = "DEFAULT"
    else:
        src.setdefault("market_rent", "USER")

    dom_as_is = tag("dom_as_is_days", p.dom_as_is_days, a.dom_as_is_default_days)
    dom_ren = tag("dom_renovated_days", p.dom_renovated_days, a.dom_renovated_default_days)
    appreciation = tag("appreciation_pct", p.appreciation_pct, a.appreciation_default_pct)
    appreciation = min(appreciation, a.appreciation_cap_pct)
    if p.months_of_supply is None:
        src.setdefault("months_of_supply", "DEFAULT")
    else:
        src.setdefault("months_of_supply", "USER")
    annual_tax = tag("annual_tax", p.annual_tax, 0.0)
    annual_ins = tag("annual_insurance", p.annual_insurance, a.insurance_default_annual)
    for name in ("beds", "baths", "sqft", "year_built", "cost_basis"):
        if getattr(p, name) is None:
            src.setdefault(name, "DEFAULT")
        else:
            src.setdefault(name, "USER")
    for name in ("loan_payoff", "investor_payback"):
        src.setdefault(name, "USER")

    old_or_unverified = p.year_built is None or p.year_built < 1960 or src.get("rehab_budget") == "DEFAULT"
    contingency = a.contingency_old_or_unverified_pct if old_or_unverified else a.contingency_pct
    rehab_total = rehab_budget * (1 + contingency / 100.0)
    rehab_months = max(1, math.ceil(rehab_budget / a.rehab_dollars_per_month)) if rehab_budget > 0 else 0

    vacate_months, cfk = 0, 0.0
    if p.occupancy == "occupied":
        lease_left = p.lease_months_remaining
        if lease_left is not None and lease_left <= 2:
            vacate_months = lease_left
        else:
            vacate_months, cfk = 1, a.cash_for_keys

    return Resolved(p=p, a=a, as_is_value=as_is, arv=arv, market_rent=market_rent,
                    dom_as_is_months=_months_from_days(dom_as_is),
                    dom_renovated_months=_months_from_days(dom_ren),
                    appreciation_pct=appreciation, months_of_supply=p.months_of_supply,
                    rehab_budget=rehab_budget, rehab_total=rehab_total, rehab_months=rehab_months,
                    contingency_pct=contingency, annual_tax=annual_tax, annual_insurance=annual_ins,
                    vacate_months=vacate_months, cash_for_keys=cfk, sources=src)


# ── shared cash-flow pieces ─────────────────────────────────────────────────

def _grown(annual: float, growth_pct: float, month: int) -> float:
    """Monthly amount for `month`, grown once per full year."""
    year = (month - 1) // 12 if month > 0 else 0
    return annual / 12.0 * (1 + growth_pct / 100.0) ** year


def _vacant_carry(r: Resolved, month: int, loan_payment: float) -> float:
    a = r.a
    return (_grown(r.annual_tax, a.tax_growth_pct, month)
            + _grown(r.annual_insurance, a.insurance_growth_pct, month)
            + loan_payment + a.utilities_vacant_monthly + a.min_maintenance_monthly)


def _occupied_offset(r: Resolved, month: int) -> float:
    """Rent collected while a tenant is still in place before vacating."""
    if r.p.occupancy == "occupied" and month <= r.vacate_months and r.p.current_rent:
        return r.p.current_rent
    return 0.0


def _concessions_apply(r: Resolved) -> bool:
    mos = r.months_of_supply
    return mos is None or mos > r.a.concessions_supply_threshold


def _retail_selling_costs(r: Resolved, price: float) -> float:
    a = r.a
    pct = a.commission_pct + a.closing_pct + (a.concessions_pct if _concessions_apply(r) else 0.0)
    return price * pct / 100.0


def _as_is_selling_costs(r: Resolved, price: float) -> float:
    return price * (r.a.as_is_commission_pct + r.a.closing_pct) / 100.0


def _empty(r: Resolved) -> list[float]:
    return [0.0] * (r.a.horizon_months + 1)


def _rehab_outlays(r: Resolved, start_month: int) -> dict[int, float]:
    """Rehab (with contingency) spread evenly over the rehab months."""
    if r.rehab_months == 0 or r.rehab_total <= 0:
        return {}
    per = r.rehab_total / r.rehab_months
    return {start_month + i: per for i in range(1, r.rehab_months + 1)}


def _financing(r: Resolved, outlays: dict[int, float], end_month: int) -> tuple[float, dict[int, float]]:
    """M4: financing cost in Net Profit; only the spread over the discount rate enters NPV.

    Returns (total_financing_cost, npv_flow_adjustments)."""
    coc = r.a.cost_of_capital_pct if r.a.cost_of_capital_pct is not None else r.a.discount_rate_pct
    total = sum(amt * coc / 100.0 * max(0, end_month - m) / 12.0 for m, amt in outlays.items())
    spread = max(0.0, coc - r.a.discount_rate_pct)
    adj = {}
    if spread > 0:
        adj[end_month] = -sum(amt * spread / 100.0 * max(0, end_month - m) / 12.0 for m, amt in outlays.items())
    return total, adj


# ── scenario models ─────────────────────────────────────────────────────────

@dataclass
class Scenario:
    strategy: str
    flows: list[float]                       # cash flows used for NPV (M1: no starting equity)
    profit_flows: list[float]                # flows used for Net Profit (M4 financing included)
    timeline_months: int
    liquidity_label: Optional[str] = None
    disqualifier: Optional[str] = None
    shortfall: float = 0.0                   # proceeds < loan + payback at liquidation (M8)
    extras: dict = field(default_factory=dict)


def sell_as_is(r: Resolved) -> Scenario:
    loan = Loan(r.p.loan_payoff, r.p.loan_rate_pct, r.p.loan_pi)
    flows = _empty(r)
    t = min(r.vacate_months + r.dom_as_is_months + 1, r.a.horizon_months)
    if r.cash_for_keys:
        flows[1] -= r.cash_for_keys
    for m in range(1, t + 1):
        pay = loan.step()
        flows[m] -= _vacant_carry(r, m, pay) - _occupied_offset(r, m)
    net_sale = r.as_is_value - _as_is_selling_costs(r, r.as_is_value) - loan.balance - r.p.investor_payback
    flows[t] += net_sale
    return Scenario("sell_as_is", flows, list(flows), t,
                    shortfall=max(0.0, -net_sale), extras={"sale_month": t, "net_sale": net_sale})


def rehab_sell(r: Resolved, starting_equity: float) -> Scenario:
    loan = Loan(r.p.loan_payoff, r.p.loan_rate_pct, r.p.loan_pi)
    flows = _empty(r)
    start = r.vacate_months
    t = min(start + r.rehab_months + r.dom_renovated_months + 1, r.a.horizon_months)
    outlays = _rehab_outlays(r, start)
    if r.cash_for_keys:
        flows[1] -= r.cash_for_keys
    carry_total = 0.0
    for m in range(1, t + 1):
        pay = loan.step()
        carry = _vacant_carry(r, m, pay) - _occupied_offset(r, m)
        carry_total += carry
        flows[m] -= carry + outlays.get(m, 0.0)
    net_sale = r.arv - _retail_selling_costs(r, r.arv) - loan.balance - r.p.investor_payback
    flows[t] += net_sale
    fin_total, npv_adj = _financing(r, outlays, t)
    profit_flows = list(flows)
    profit_flows[t] -= fin_total
    for m, v in npv_adj.items():
        flows[m] += v
    s = Scenario("rehab_sell", flows, profit_flows, t, shortfall=max(0.0, -net_sale),
                 extras={"sale_month": t, "net_sale": net_sale, "financing_cost": fin_total,
                         "carry_total": carry_total})
    # Spec override: profit after all costs < 10% of rehab + carry invested.
    gain = sum(profit_flows) - starting_equity
    invested = r.rehab_total + carry_total
    if invested > 0 and gain < 0.10 * invested:
        s.disqualifier = "Profit after all costs is under 10% of rehab + carry invested"
    return s


def rehab_rent(r: Resolved, starting_equity: float, stress: Optional[dict] = None) -> Scenario:
    stress = stress or {}
    a = r.a
    loan = Loan(r.p.loan_payoff, r.p.loan_rate_pct, r.p.loan_pi)
    flows = _empty(r)
    h = a.horizon_months
    start = r.vacate_months
    outlays = _rehab_outlays(r, start)
    rehab_end = start + r.rehab_months
    lease_start = min(rehab_end + a.lease_up_months + 1, h)
    vacancy = stress.get("vacancy_pct", a.vacancy_pct)
    ins_growth = stress.get("insurance_growth_pct", a.insurance_growth_pct)
    full_rehab = r.rehab_budget >= a.rehab_dollars_per_month
    if r.cash_for_keys:
        flows[1] -= r.cash_for_keys
    year1_noi = year1_debt = year1_gross = year1_opex = 0.0
    op_months = 0
    min_cf = None
    for m in range(1, h + 1):
        pay = loan.step()
        if m < lease_start:
            flows[m] -= _vacant_carry(r, m, pay) - _occupied_offset(r, m) + outlays.get(m, 0.0)
            continue
        ops_month = m - lease_start          # 0-based month of operation
        gross = r.market_rent * (1 + a.rent_growth_pct / 100.0) ** (ops_month // 12)
        collected = gross * (1 - vacancy / 100.0)
        tax = _grown(r.annual_tax, a.tax_growth_pct, m)
        ins = r.annual_insurance / 12.0 * (1 + ins_growth / 100.0) ** ((m - 1) // 12)
        maint_pct = a.maintenance_post_rehab_pct if (full_rehab and m - rehab_end <= 24) else a.maintenance_pct
        opex = (tax + ins + gross * maint_pct / 100.0 + gross * a.capex_pct / 100.0
                + collected * a.management_pct / 100.0 + a.turnover_cost / a.turnover_every_months)
        if ops_month == 0:
            opex += gross * 0.5                                  # ½ month management lease-up fee
        if stress.get("major_repair_month") == m:
            opex += stress.get("major_repair_cost", 0.0)
        noi = collected - opex
        cf = noi - pay
        flows[m] += cf
        if op_months < 12:
            year1_noi += noi
            year1_debt += pay
            year1_gross += gross
            year1_opex += opex
            op_months += 1
            min_cf = cf if min_cf is None else min(min_cf, cf)
    # Terminal hypothetical sale at month h (spec model C)
    years = h / 12.0
    terminal_price = r.arv * (1 + r.appreciation_pct / 100.0) ** years
    terminal = terminal_price - _retail_selling_costs(r, terminal_price) - loan.balance - r.p.investor_payback
    flows[h] += terminal
    fin_total, npv_adj = _financing(r, outlays, lease_start)
    profit_flows = list(flows)
    profit_flows[h] -= fin_total
    for m, v in npv_adj.items():
        flows[m] += v
    dscr = (year1_noi / year1_debt) if year1_debt > 0 else None
    cash_invested = starting_equity + r.rehab_total
    year1_cf = year1_noi - year1_debt
    s = Scenario("rehab_rent", flows, profit_flows, h, liquidity_label="assumed sale",
                 shortfall=max(0.0, -terminal),
                 extras={
                     "terminal_value": terminal, "financing_cost": fin_total,
                     "year1_cap_rate_pct": (year1_noi / r.arv * 100.0) if r.arv else None,
                     "cash_on_cash_pct": (year1_cf / cash_invested * 100.0) if cash_invested > 0 else None,
                     "dscr": dscr,
                     "break_even_occupancy_pct": ((year1_opex + year1_debt) / year1_gross * 100.0) if year1_gross else None,
                     "year1_monthly_cash_flow": year1_cf / op_months if op_months else None,
                     "operating_months_in_year1": op_months,
                 })
    if dscr is not None and dscr < 1.15:
        s.disqualifier = f"Year-1 DSCR {dscr:.2f} is below 1.15"
    elif op_months and year1_cf / op_months < 0:
        s.disqualifier = "Monthly cash flow is negative after reserves"
    return s


def rehab_land_contract(r: Resolved, stress: Optional[dict] = None) -> Scenario:
    stress = stress or {}
    a = r.a
    h = a.horizon_months
    loan = Loan(r.p.loan_payoff, r.p.loan_rate_pct, r.p.loan_pi)
    start = r.vacate_months
    outlays = _rehab_outlays(r, start)
    close = min(start + r.rehab_months + a.lc_marketing_months, h - 1)
    price = r.arv * (1 + a.lc_price_premium_pct / 100.0)
    down = price * a.lc_down_pct / 100.0
    principal = price - down
    pmt = _amortized_payment(principal, a.lc_rate_pct, a.lc_amort_years)
    default_prob = stress.get("lc_default_prob_pct", a.lc_default_prob_pct) / 100.0
    payback = r.p.investor_payback

    # Shared pre-closing flows (rehab + carry), then closing (E7 loan payoff; no investor payback, M9).
    base = _empty(r)
    if r.cash_for_keys:
        base[1] -= r.cash_for_keys
    for m in range(1, close + 1):
        pay = loan.step()
        base[m] -= _vacant_carry(r, m, pay) - _occupied_offset(r, m) + outlays.get(m, 0.0)
    closing_cash = down - price * a.closing_pct / 100.0 - loan.balance
    base[close] += closing_cash

    def performing(balloon: bool) -> list[float]:
        f = list(base)
        for m in range(close + 1, h + 1):
            f[m] += pmt - a.lc_servicing_monthly
        remaining = _remaining_principal(principal, a.lc_rate_pct, a.lc_amort_years, h - close)
        if balloon:
            f[h] += remaining - payback
        else:
            f[h] += remaining * (1 - a.lc_note_discount_pct / 100.0) - payback
        return f

    def defaulted() -> list[float]:
        f = list(base)
        dm = max(a.lc_default_month, close + 1)
        rm = min(dm + (a.lc_resale_month - a.lc_default_month), h)
        for m in range(close + 1, dm + 1):
            f[m] += pmt - a.lc_servicing_monthly
        for m in range(dm + 1, rm + 1):            # M5: buyer stops paying; IRES carries
            f[m] -= _vacant_carry(r, m, 0.0)
        f[min(dm + 2, h)] -= a.lc_legal_cost
        f[min(rm - 1, h)] -= a.lc_repair_cost
        resale_value = r.as_is_value * (1 + r.appreciation_pct / 100.0) ** (rm / 12.0)
        f[rm] += resale_value - _as_is_selling_costs(r, resale_value) - payback
        return f

    balloon_p = a.lc_balloon_paid_prob_pct / 100.0
    perf_b, perf_x, dflt = performing(True), performing(False), defaulted()
    ev = [(1 - default_prob) * (balloon_p * pb + (1 - balloon_p) * px) + default_prob * d
          for pb, px, d in zip(perf_b, perf_x, dflt)]
    fin_total, npv_adj = _financing(r, outlays, close)
    profit_flows = list(ev)
    profit_flows[close] -= fin_total
    for m, v in npv_adj.items():
        ev[m] += v
    months_paid = h - close
    interest_earned = pmt * months_paid - (
        principal - _remaining_principal(principal, a.lc_rate_pct, a.lc_amort_years, months_paid))
    return Scenario("rehab_land_contract", ev, profit_flows, h, liquidity_label="balloon or note sale",
                    shortfall=max(0.0, -closing_cash),
                    extras={
                        "lc_price": price, "down_payment": down, "monthly_payment": pmt,
                        "closing_month": close, "closing_cash": closing_cash,
                        "performing_npv": balloon_p * npv(perf_b, a.discount_rate_pct)
                                          + (1 - balloon_p) * npv(perf_x, a.discount_rate_pct),
                        "default_npv": npv(dflt, a.discount_rate_pct),
                        "total_interest_earned": interest_earned,
                        "financing_cost": fin_total,
                    })


# ── metrics ─────────────────────────────────────────────────────────────────

def metrics(s: Scenario, r: Resolved, starting_equity: float, liquidate_now_total: float) -> dict:
    a = r.a
    flows = s.flows
    cum, peak_out = 0.0, 0.0
    for cf in flows:
        cum += cf
        peak_out = max(peak_out, -cum)
    returned = sum(cf for cf in s.profit_flows if cf > 0)
    invested = -sum(cf for cf in s.profit_flows if cf < 0)
    denom = starting_equity + invested
    # IRR treats today's equity as the amount kept invested (opportunity cost);
    # NPV does not include it (M1).
    if starting_equity > 0:
        irr, irr_reason = irr_annual([flows[0] - starting_equity] + flows[1:])
    else:
        irr, irr_reason = None, "starting equity is zero or negative"
    return {
        "net_profit": sum(s.profit_flows),
        "npv": npv(flows, a.discount_rate_pct),
        "irr": irr,
        "irr_reason": irr_reason,
        "equity_multiple": (returned / denom) if denom > 0 else None,
        "peak_capital": peak_out,
        **liquidity(s, liquidate_now_total),
    }


def liquidity(s: Scenario, liquidate_now_total: float) -> dict:
    """M6, measured against what liquidating today yields.

    A path has IRES's capital back in the first month its cumulative cash
    reaches the total cash of selling as-is now (cumulative cash already nets
    out any extra capital the path put in). Sell as-is reaches it at its own
    closing. Paths that only get there through the hypothetical terminal sale
    or the note value at the horizon are labeled; never reached -> '> horizon'.
    """
    target = max(0.0, liquidate_now_total)
    cum = 0.0
    h = len(s.flows) - 1
    for m, cf in enumerate(s.flows):
        cum += cf
        if cf > 0 and cum + 1e-6 >= target:
            if m == h and s.liquidity_label:
                return {"months_to_liquidity": h, "liquidity_label": s.liquidity_label}
            return {"months_to_liquidity": m, "liquidity_label": None}
    return {"months_to_liquidity": None, "liquidity_label": f"> {h}"}


def _liquidity_sort_key(mt: dict, h: int) -> tuple:
    m = mt.get("months_to_liquidity")
    if m is None:
        return (10_000, 1)
    return (m, 1 if mt.get("liquidity_label") else 0)


# ── stress tests, risk, ranking ─────────────────────────────────────────────

STRESS_TESTS = ["base", "rehab_overrun", "arv_minus_10", "zero_appreciation",
                "rental_stress", "lc_default_40", "combined_downside"]


def _stressed(p: PropertyInputs, a: Assumptions, name: str) -> tuple[PropertyInputs, Assumptions, dict]:
    extra: dict = {}
    if name in ("rehab_overrun", "combined_downside"):
        if p.rehab_budget:
            p = replace(p, rehab_budget=p.rehab_budget * 1.25)
        extra["extra_rehab_month"] = True
    if name in ("arv_minus_10", "combined_downside"):
        if p.arv is not None:
            p = replace(p, arv=p.arv * 0.9)
        else:
            # ARV is derived from as-is value + rehab; stress the derived value.
            r0 = resolve(p, a)
            p = replace(p, arv=r0.arv * 0.9)
    if name in ("zero_appreciation", "combined_downside"):
        p = replace(p, appreciation_pct=0.0)
    if name == "rental_stress":
        extra.update({"vacancy_pct": 15.0, "major_repair_month": 18, "major_repair_cost": 5000.0,
                      "insurance_growth_pct": a.insurance_growth_pct + 15.0})
    if name == "lc_default_40":
        extra["lc_default_prob_pct"] = 40.0
    return p, a, extra


def _run_scenarios(p: PropertyInputs, a: Assumptions, stress: Optional[dict] = None) -> tuple[Resolved, dict, float, float]:
    stress = stress or {}
    r = resolve(p, a)
    if stress.get("extra_rehab_month") and r.rehab_budget > 0:
        r.rehab_months += 1
    starting_equity = r.as_is_value - r.p.loan_payoff - r.p.investor_payback
    a_s = sell_as_is(r)
    scen = {
        "sell_as_is": a_s,
        "rehab_sell": rehab_sell(r, starting_equity),
        "rehab_rent": rehab_rent(r, starting_equity, stress),
        "rehab_land_contract": rehab_land_contract(r, stress),
    }
    liquidate_now_total = sum(a_s.flows)
    return r, scen, starting_equity, liquidate_now_total


def risk_score(base_npv: float, stress_npvs: list[float], critical_flags: int) -> int:
    """S1a."""
    worst = min(stress_npvs) if stress_npvs else base_npv
    if base_npv == 0:
        spread = 1.0
    else:
        spread = max(0.0, min(1.0, (base_npv - worst) / abs(base_npv)))
    return int(min(10, round(1 + 9 * spread) + critical_flags))


def risk_adjusted(npv_value: float, score: int) -> float:
    """M2: risk always makes the score worse, including for losses."""
    factor = 1 + 0.1 * score
    return npv_value / factor if npv_value >= 0 else npv_value * factor


def _critical_flags(strategy: str, r: Resolved) -> int:
    n = 0
    if strategy in ("rehab_sell", "rehab_rent", "rehab_land_contract") and r.arv and r.rehab_total > 0.30 * r.arv:
        n += 1
    if strategy == "rehab_land_contract" and r.p.lc_forfeiture_history:
        n += 1
    return n


def evaluate(p: PropertyInputs, a: Assumptions, with_stress: bool = True) -> dict:
    """Scenario results with metrics, stress tests and risk scores (no ranking text)."""
    r, scen, starting_equity, liquidate_now_total = _run_scenarios(p, a)
    out = {}
    stress_results = {name: {} for name in STRESS_TESTS}
    if with_stress:
        for name in STRESS_TESTS[1:]:
            sp, sa, extra = _stressed(p, a, name)
            _, sscen, _, _ = _run_scenarios(sp, sa, extra)
            for k, s in sscen.items():
                stress_results[name][k] = npv(s.flows, a.discount_rate_pct)
    for k, s in scen.items():
        m = metrics(s, r, starting_equity, liquidate_now_total)
        stresses = {"base": m["npv"]}
        for name in STRESS_TESTS[1:]:
            stresses[name] = stress_results[name].get(k, m["npv"]) if with_stress else m["npv"]
        score = risk_score(m["npv"], list(stresses.values()), _critical_flags(k, r))
        out[k] = {
            "scenario": s, "metrics": m, "stress_tests": stresses,
            "risk_score": score, "risk_adjusted_score": risk_adjusted(m["npv"], score),
        }
    return {"resolved": r, "scenarios": out, "starting_equity": starting_equity,
            "liquidate_now_total": liquidate_now_total}


def rank(ev: dict, a: Assumptions) -> dict:
    sc = ev["scenarios"]
    h = a.horizon_months
    eligible = [k for k in STRATEGIES if sc[k]["scenario"].disqualifier is None]
    if not eligible:
        eligible = ["sell_as_is"]
    order = sorted(eligible, key=lambda k: sc[k]["risk_adjusted_score"], reverse=True)
    close_call = False
    if len(order) >= 2:
        top, second = order[0], order[1]
        t, s2 = sc[top]["risk_adjusted_score"], sc[second]["risk_adjusted_score"]
        if t != 0 and abs(t - s2) / abs(t) <= a.close_call_pct / 100.0:
            close_call = True
            if a.owner_goal not in ("maximize_total_return", "monthly_income"):
                def key(k):
                    return (sc[k]["metrics"]["peak_capital"], _liquidity_sort_key(sc[k]["metrics"], h))
                pair = sorted([top, second], key=key)
                order = pair + order[2:]
    disq = [k for k in STRATEGIES if k not in eligible]
    winner, runner = order[0], (order[1] if len(order) > 1 else None)
    margin = None
    if runner is not None:
        w, rr = sc[winner]["risk_adjusted_score"], sc[runner]["risk_adjusted_score"]
        margin = ((w - rr) / abs(rr) * 100.0) if rr else None
    return {"strategy": winner, "ranking": order + disq, "runner_up": runner,
            "close_call": close_call, "margin_vs_runner_up_pct": margin}


# ── break-even (M7) ─────────────────────────────────────────────────────────

BREAK_EVEN_VARS = {"arv": "ARV", "market_rent": "market rent", "rehab_budget": "rehab budget"}


def _winner_for(p: PropertyInputs, a: Assumptions) -> str:
    return rank(evaluate(p, a), a)["strategy"]


def break_even(p: PropertyInputs, a: Assumptions, base_winner: str, base_values: dict) -> list[dict]:
    triggers = []
    for var, label in BREAK_EVEN_VARS.items():
        base = base_values.get(var)
        if not base:
            continue
        for direction in (-1, 1):
            prev_f = 0.0
            found = None
            for step in range(1, 11):
                f = direction * 0.05 * step
                w = _winner_for(replace(p, **{var: base * (1 + f)}), a)
                if w != base_winner:
                    lo, hi = prev_f, f
                    for _ in range(30):
                        mid = (lo + hi) / 2
                        if _winner_for(replace(p, **{var: base * (1 + mid)}), a) == base_winner:
                            lo = mid
                        else:
                            hi = mid
                        if abs((hi - lo) * base) < 100:
                            break
                    value = round(base * (1 + hi) / 100.0) * 100.0
                    found = {"variable": var, "label": label, "threshold": value,
                             "direction": "above" if direction > 0 else "below",
                             "new_winner": _winner_for(replace(p, **{var: base * (1 + hi)}), a)}
                    break
                prev_f = f
            if found:
                triggers.append(found)
    return triggers


# ── preflight, confidence, flags ────────────────────────────────────────────

def confidence(r: Resolved) -> str:
    """M3 + S1b + N-design D10 (stale statement caps at Medium)."""
    src = r.sources
    missing_facts = any(src.get(k) == "DEFAULT" for k in ("beds", "baths", "sqft", "year_built"))
    if src.get("arv") == "DEFAULT" or src.get("market_rent") == "DEFAULT" or missing_facts:
        return "Low"
    defaults = sum(1 for k in CONFIDENCE_INPUTS if src.get(k) == "DEFAULT")
    if defaults > 5 or r.p.stale_loan_statement:
        return "Medium"
    return "High"


def preflight_flags(r: Resolved, scen: dict) -> list[dict]:
    flags = []
    if r.p.occupancy == "occupied":
        flags.append({"type": "risk", "severity": "info",
                      "message": "Tenant-occupied: sale and rehab paths start after the lease ends or cash-for-keys"})
    if r.arv and r.rehab_total > 0.30 * r.arv:
        flags.append({"type": "risk", "severity": "warning",
                      "message": "Rehab plus contingency exceeds 30% of ARV; double-check scope and ARV"})
    if r.p.loan_payoff > 0:
        flags.append({"type": "risk", "severity": "warning",
                      "message": "Existing mortgage: a land contract sale may trigger due-on-sale; payoff at LC closing is assumed"})
    if r.a.lc_servicer:
        flags.append({"type": "compliance", "severity": "info",
                      "message": f"LC servicing and compliance: {r.a.lc_servicer}"})
    else:
        flags.append({"type": "compliance", "severity": "warning",
                      "message": "Land contract: Compliance Review Required (Dodd-Frank/SAFE Act seller-financing rules)"})
    if r.p.lc_forfeiture_history:
        flags.append({"type": "risk", "severity": "warning",
                      "message": "Prior land contract forfeiture on this property"})
    if Loan(r.p.loan_payoff, r.p.loan_rate_pct, r.p.loan_pi).negative_amortization:
        flags.append({"type": "data", "severity": "warning",
                      "message": "Loan P&I is below the monthly interest (negative amortization)"})
    if r.p.stale_loan_statement:
        flags.append({"type": "data", "severity": "warning",
                      "message": "Loan payoff comes from a statement more than 60 days old"})
    for k, s in scen.items():
        if s.shortfall > 0:
            flags.append({"type": "risk", "severity": "critical",
                          "message": f"{STRATEGY_LABELS[k]}: proceeds don't cover loan + investor payback; short ${s.shortfall:,.0f}"})
    for name in ("arv", "market_rent"):
        if r.sources.get(name) == "DEFAULT":
            flags.append({"type": "data", "severity": "warning",
                          "message": f"No comps for {'ARV' if name == 'arv' else 'market rent'}; a labeled default was used"})
    return flags


# ── public entry point ──────────────────────────────────────────────────────

def analyze(p: PropertyInputs, a: Optional[Assumptions] = None, with_break_even: bool = True) -> dict:
    """Full analysis_json (spec Output Format) with exact values."""
    a = a or Assumptions()
    ev = evaluate(p, a)
    r: Resolved = ev["resolved"]
    decision = rank(ev, a)
    scen_objs = {k: v["scenario"] for k, v in ev["scenarios"].items()}
    flags = preflight_flags(r, scen_objs)
    scenarios_json = []
    for k in STRATEGIES:
        item = ev["scenarios"][k]
        s: Scenario = item["scenario"]
        m = item["metrics"]
        annual = [sum(s.flows[1 + 12 * y: 1 + 12 * (y + 1)]) for y in range(math.ceil(a.horizon_months / 12))]
        scenarios_json.append({
            "strategy": k,
            "eligible": s.disqualifier is None,
            "disqualifier": s.disqualifier,
            "timeline_months": s.timeline_months,
            "net_profit": m["net_profit"],
            "npv": m["npv"],
            "irr": m["irr"],
            "irr_reason": m["irr_reason"],
            "equity_multiple": m["equity_multiple"],
            "peak_capital": m["peak_capital"],
            "months_to_liquidity": m["months_to_liquidity"],
            "liquidity_label": m["liquidity_label"],
            "risk_score": item["risk_score"],
            "risk_adjusted_score": item["risk_adjusted_score"],
            "effort": EFFORT[k],
            "annual_cash_flow": annual,
            "stress_tests": item["stress_tests"],
            "profit_vs_cost_basis": (m["net_profit"] - p.cost_basis) if p.cost_basis is not None else None,
            "shortfall": s.shortfall,
            "details": s.extras,
            "monthly_cash_flows": s.flows,
        })
    if p.cost_basis is not None:
        win = ev["scenarios"][decision["strategy"]]["metrics"]["net_profit"]
        if win < p.cost_basis:
            flags.append({"type": "risk", "severity": "warning",
                          "message": f"Recommended exit returns ${p.cost_basis - win:,.0f} less than cost basis"})
    triggers = []
    if with_break_even:
        base_values = {"arv": r.arv, "market_rent": r.market_rent, "rehab_budget": r.rehab_budget}
        triggers = break_even(p, a, decision["strategy"], base_values)
    assumptions_list = [{"name": k, "value": v, "source": "DEFAULT", "source_detail": "PropYield defaults"}
                        for k, v in asdict(a).items() if v is not None]
    for k in CONFIDENCE_INPUTS:
        val = getattr(r, k, None) if hasattr(r, k) else getattr(p, k, None)
        assumptions_list.append({"name": k, "value": val, "source": r.sources.get(k, "USER"),
                                 "source_detail": p.sources.get(k + "_detail", "")})
    return {
        "property": {"address": p.address, "city": p.city, "state": p.state,
                     "occupancy": p.occupancy, "confidence": confidence(r)},
        "starting_equity": ev["starting_equity"],
        "assumptions": assumptions_list,
        "flags": flags,
        "scenarios": scenarios_json,
        "recommendation": {
            "strategy": decision["strategy"],
            "ranking": decision["ranking"],
            "runner_up": decision["runner_up"],
            "close_call": decision["close_call"],
            "margin_vs_runner_up_pct": decision["margin_vs_runner_up_pct"],
            "break_even_triggers": triggers,
            "one_line_summary": "",
        },
    }
