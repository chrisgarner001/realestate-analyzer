"""
Property tax engine.

Generalizes the model behind Michigan's Property Tax Estimator
(treas-secure.state.mi.us/ptestimator) to all 50 states:

    annual_tax = ((market_value * assessment_ratio) - exemptions) * mills / 1000

Michigan is the special case that motivated this module: SEV is 50% of true
cash value, Taxable Value is capped until the property transfers, and a
non-homestead (investor) buyer pays up to 18 additional school operating mills
that a Principal Residence Exemption owner does not. The result is that an
investor's bill routinely lands 40-70% above what the seller currently pays.

Two levels of accuracy, in preference order:
  1. MILLAGE  - an imported jurisdiction record with real homestead and
                non-homestead millage rates. Authoritative.
  2. FALLBACK - the statewide median effective rate from state_rules.json,
                adjusted for owner-occupancy. Clearly labelled as an estimate.

The millage lookup is injected so this module stays import-light and unit
testable without a database.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

RULES_PATH = Path(__file__).parent / "taxdata" / "state_rules.json"

STATE_ABBREVS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
VALID_STATES = set(STATE_ABBREVS.values())

# Fallback used only when a state is unknown; roughly the national median.
NATIONAL_MEDIAN_EFFECTIVE_RATE = 0.0110


@lru_cache(maxsize=1)
def load_state_rules() -> dict:
    with RULES_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def get_state_rules(state: Optional[str]) -> Optional[dict]:
    if not state:
        return None
    return load_state_rules().get(state.strip().upper())


def parse_state(address: Optional[str]) -> Optional[str]:
    """Pull a 2-letter state code out of a free-form US address."""
    if not address:
        return None
    text = address.strip()

    # "..., MI 48009" or "..., MI"
    match = re.search(r",\s*([A-Za-z]{2})\s*(?:\d{5}(?:-\d{4})?)?\s*$", text)
    if match and match.group(1).upper() in VALID_STATES:
        return match.group(1).upper()

    lowered = text.lower()
    for name, code in STATE_ABBREVS.items():
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return code

    # Last resort: any standalone 2-letter token that is a real state.
    for token in reversed(re.findall(r"\b([A-Za-z]{2})\b", text)):
        if token.upper() in VALID_STATES:
            return token.upper()
    return None


def _to_float(value) -> Optional[float]:
    """Accept 450000, '450,000', '$450k', '1.2%' and similar."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(",", "").replace("$", "").replace("%", "")
    multiplier = 1.0
    if text.endswith("k"):
        multiplier, text = 1_000.0, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000.0, text[:-1]
    match = re.search(r"-?\d*\.?\d+", text)
    if not match:
        return None
    try:
        return float(match.group()) * multiplier
    except ValueError:
        return None


@dataclass
class MillageRecord:
    """A jurisdiction's levied millage, mirroring Michigan's PTE output."""
    state: str
    county: str
    jurisdiction: str                      # city / township / village
    school_district: str = ""
    homestead_mills: float = 0.0           # PRE / owner-occupied total mills
    non_homestead_mills: float = 0.0       # non-PRE / investor total mills
    year: Optional[int] = None
    source: str = ""

    def mills_for(self, owner_occupied: bool) -> float:
        if owner_occupied:
            return self.homestead_mills or self.non_homestead_mills
        return self.non_homestead_mills or self.homestead_mills


@dataclass
class TaxScenario:
    label: str
    annual: float
    monthly: float
    effective_rate: float                  # as a fraction of market value
    taxable_base: float
    mills: Optional[float] = None


@dataclass
class TaxEstimate:
    state: Optional[str]
    market_value: float
    method: str                            # "millage" | "state_fallback"
    confidence: str                        # "high" | "medium" | "low"
    owner_occupied: bool
    assessment_ratio: float
    homestead: Optional[TaxScenario] = None
    non_homestead: Optional[TaxScenario] = None
    selected: Optional[TaxScenario] = None
    seller_current: Optional[TaxScenario] = None
    uncapping_delta_annual: Optional[float] = None
    jurisdiction: Optional[dict] = None
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _scenario(label, taxable_base, mills=None, rate=None, market_value=0.0) -> TaxScenario:
    if mills is not None:
        annual = taxable_base * mills / 1000.0
    else:
        annual = taxable_base * (rate or 0.0)
    effective = (annual / market_value) if market_value else 0.0
    return TaxScenario(
        label=label,
        annual=round(annual, 2),
        monthly=round(annual / 12.0, 2),
        effective_rate=round(effective, 6),
        taxable_base=round(taxable_base, 2),
        mills=round(mills, 4) if mills is not None else None,
    )


def _fallback_non_owner_rate(rules: dict, owner_rate: float, market_value: float) -> float:
    """
    Adjust a statewide owner-occupied effective rate up to a non-homestead rate.

    Published median effective rates are derived from owner-occupied homes, so
    they already bake in whatever homestead relief the state grants. We reverse
    that relief three different ways depending on how the state implements it.
    """
    ratio_owner = rules.get("assessment_ratio") or 1.0
    ratio_non_owner = rules.get("non_owner_assessment_ratio")

    # 1. States that reclassify the property at a higher assessment ratio.
    if ratio_non_owner and ratio_owner and ratio_non_owner != ratio_owner:
        return owner_rate * (ratio_non_owner / ratio_owner)

    # 2. States that levy extra mills on non-homestead property (Michigan).
    extra_mills = rules.get("non_homestead_extra_mills")
    if extra_mills:
        return owner_rate + (extra_mills * ratio_owner / 1000.0)

    # 3. States granting a flat exemption the investor loses.
    exemption = (rules.get("homestead_exemption") or {}).get("amount")
    if exemption and market_value > exemption:
        return owner_rate * market_value / (market_value - exemption)

    # 4. States granting percentage relief the investor loses.
    percent = (rules.get("homestead_exemption") or {}).get("percent")
    if percent and 0 < percent < 1:
        return owner_rate / (1.0 - percent)

    return owner_rate


def estimate_property_tax(
    market_value,
    state: Optional[str] = None,
    address: Optional[str] = None,
    county: Optional[str] = None,
    jurisdiction: Optional[str] = None,
    school_district: Optional[str] = None,
    owner_occupied: bool = False,
    sev: Optional[float] = None,
    current_taxable_value: Optional[float] = None,
    millage_lookup: Optional[Callable[..., Optional[MillageRecord]]] = None,
) -> TaxEstimate:
    """
    Estimate annual property tax for a purchase.

    market_value          purchase price / true cash value
    owner_occupied        False for an investor (non-homestead) buyer
    sev                   override the assessed value instead of deriving it
    current_taxable_value what the SELLER is taxed on today, used to show the
                          uncapping jump the buyer will absorb
    millage_lookup        callable(state, county, jurisdiction, school_district)
                          returning a MillageRecord or None
    """
    value = _to_float(market_value) or 0.0
    state = (state or parse_state(address) or "").upper() or None
    rules = get_state_rules(state) or {}

    ratio = rules.get("assessment_ratio", 1.0) or 1.0
    non_owner_ratio = rules.get("non_owner_assessment_ratio") or ratio

    estimate = TaxEstimate(
        state=state,
        market_value=round(value, 2),
        method="state_fallback",
        confidence="low",
        owner_occupied=owner_occupied,
        assessment_ratio=ratio,
    )

    if not state:
        estimate.warnings.append(
            "Could not determine the state from the address. Using the national median rate."
        )
    if value <= 0:
        estimate.warnings.append("No purchase price supplied; tax cannot be estimated.")
        return estimate

    homestead_base = sev if sev is not None else value * ratio
    non_homestead_base = sev if sev is not None else value * non_owner_ratio

    record = None
    if millage_lookup and state:
        try:
            record = millage_lookup(
                state=state, county=county,
                jurisdiction=jurisdiction, school_district=school_district,
            )
        except Exception:
            record = None

    if record and (record.homestead_mills or record.non_homestead_mills):
        estimate.method = "millage"
        estimate.confidence = "high"
        estimate.source = record.source or f"{state} millage table"
        estimate.jurisdiction = {
            "county": record.county,
            "jurisdiction": record.jurisdiction,
            "school_district": record.school_district,
            "year": record.year,
            "homestead_mills": record.homestead_mills,
            "non_homestead_mills": record.non_homestead_mills,
        }
        estimate.homestead = _scenario(
            "Homestead / owner-occupied", homestead_base,
            mills=record.mills_for(True), market_value=value,
        )
        estimate.non_homestead = _scenario(
            "Non-homestead / investor", non_homestead_base,
            mills=record.mills_for(False), market_value=value,
        )
        if not record.non_homestead_mills:
            estimate.warnings.append(
                "Non-homestead millage not on file for this jurisdiction — showing the "
                "homestead rate instead. An investor buyer's actual bill is typically higher."
            )
        elif not record.homestead_mills:
            estimate.warnings.append(
                "Homestead millage not on file for this jurisdiction — showing the "
                "non-homestead rate instead."
            )
    else:
        owner_rate = rules.get("median_effective_rate") or NATIONAL_MEDIAN_EFFECTIVE_RATE
        investor_rate = _fallback_non_owner_rate(rules, owner_rate, value)
        estimate.confidence = rules.get("confidence", "low")
        estimate.source = f"{rules.get('name', 'National')} statewide median effective rate"
        estimate.homestead = _scenario(
            "Homestead / owner-occupied", value, rate=owner_rate, market_value=value,
        )
        estimate.non_homestead = _scenario(
            "Non-homestead / investor", value, rate=investor_rate, market_value=value,
        )
        if state and rules.get("millage_available"):
            estimate.warnings.append(
                f"No local millage record on file for this {rules.get('name', state)} "
                "jurisdiction. This is a statewide median estimate — verify with the "
                "county treasurer before relying on it."
            )

    estimate.selected = estimate.homestead if owner_occupied else estimate.non_homestead

    # What the seller pays today vs what the buyer will pay.
    current_tv = _to_float(current_taxable_value)
    if current_tv and estimate.selected:
        if estimate.method == "millage" and record:
            seller = _scenario(
                "Seller's current bill", current_tv,
                mills=record.mills_for(True), market_value=value,
            )
        else:
            seller_rate = rules.get("median_effective_rate") or NATIONAL_MEDIAN_EFFECTIVE_RATE
            base_ratio = ratio or 1.0
            seller = _scenario(
                "Seller's current bill", current_tv / base_ratio,
                rate=seller_rate, market_value=value,
            )
        estimate.seller_current = seller
        estimate.uncapping_delta_annual = round(estimate.selected.annual - seller.annual, 2)

    _add_state_notes(estimate, rules, owner_occupied)
    return estimate


def _add_state_notes(estimate: TaxEstimate, rules: dict, owner_occupied: bool) -> None:
    if not rules:
        return
    if rules.get("notes"):
        estimate.notes.append(rules["notes"])

    reassessment = rules.get("reassessment_on_sale")
    if reassessment == "uncap_to_sev":
        estimate.warnings.append(
            "Michigan uncapping: this property's Taxable Value resets to SEV in the "
            "year after the sale. The seller's current tax bill is NOT what you will pay."
        )
    elif reassessment == "reassess_to_purchase_price":
        estimate.warnings.append(
            "Proposition 13: the assessed value resets to your purchase price on transfer."
        )
    elif reassessment in ("reset_to_just_value", "reset_to_market", "reassess_at_sale"):
        estimate.warnings.append(
            "Assessment caps reset on transfer — expect the bill to rise above the seller's."
        )
    elif reassessment == "no_reassessment":
        estimate.notes.append(
            "Assessed value does not reset on sale in this state; you inherit the seller's basis."
        )

    if not owner_occupied and rules.get("owner_occupancy_matters"):
        exemption = rules.get("homestead_exemption") or {}
        detail = exemption.get("description")
        if detail:
            estimate.warnings.append(f"As a non-owner-occupant you lose: {detail}")

    if estimate.homestead and estimate.non_homestead:
        gap = estimate.non_homestead.annual - estimate.homestead.annual
        if gap > 1 and estimate.homestead.annual > 0:
            pct = gap / estimate.homestead.annual * 100
            estimate.notes.append(
                f"Investor (non-homestead) tax runs ${gap:,.0f}/yr — {pct:.0f}% — above "
                "the owner-occupied bill in this jurisdiction."
            )
