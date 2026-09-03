import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.property_tax import (  # noqa: E402
    MillageRecord, estimate_property_tax, parse_state, get_state_rules, _to_float,
)


def mi_millage(**_kwargs):
    return MillageRecord(
        state="MI", county="Oakland", jurisdiction="Royal Oak City",
        school_district="Royal Oak Schools",
        homestead_mills=32.0, non_homestead_mills=50.0,
        year=2025, source="test",
    )


class ParseStateTests(unittest.TestCase):
    def test_parses_trailing_state_and_zip(self):
        self.assertEqual(parse_state("123 Main St, Royal Oak, MI 48067"), "MI")

    def test_parses_state_without_zip(self):
        self.assertEqual(parse_state("500 Woodward Ave, Detroit, MI"), "MI")

    def test_parses_full_state_name(self):
        self.assertEqual(parse_state("12 Elm Street, Austin, Texas 78701"), "TX")

    def test_returns_none_for_unparseable(self):
        self.assertIsNone(parse_state("no state here"))

    def test_does_not_mistake_street_abbreviation_for_state(self):
        self.assertEqual(parse_state("400 Ocean Dr, Miami, FL 33139"), "FL")


class ValueParsingTests(unittest.TestCase):
    def test_handles_formatted_currency(self):
        self.assertEqual(_to_float("$450,000"), 450000.0)

    def test_handles_shorthand(self):
        self.assertEqual(_to_float("450k"), 450000.0)

    def test_returns_none_for_garbage(self):
        self.assertIsNone(_to_float("n/a"))


class MichiganMillageTests(unittest.TestCase):
    """Mirrors the Michigan Property Tax Estimator's own arithmetic."""

    def test_homestead_and_non_homestead_use_sev_as_base(self):
        est = estimate_property_tax(
            300_000, state="MI", county="Oakland", jurisdiction="Royal Oak City",
            owner_occupied=False, millage_lookup=mi_millage,
        )
        # SEV = 50% of $300,000 = $150,000
        self.assertEqual(est.homestead.taxable_base, 150_000.0)
        self.assertEqual(est.homestead.annual, 4_800.0)     # 150,000 * 32 / 1000
        self.assertEqual(est.non_homestead.annual, 7_500.0)  # 150,000 * 50 / 1000
        self.assertEqual(est.method, "millage")
        self.assertEqual(est.confidence, "high")

    def test_investor_selects_non_homestead_scenario(self):
        est = estimate_property_tax(300_000, state="MI", county="Oakland",
                                    owner_occupied=False, millage_lookup=mi_millage)
        self.assertEqual(est.selected.annual, 7_500.0)
        self.assertEqual(est.selected.monthly, 625.0)

    def test_owner_occupant_selects_homestead_scenario(self):
        est = estimate_property_tax(300_000, state="MI", county="Oakland",
                                    owner_occupied=True, millage_lookup=mi_millage)
        self.assertEqual(est.selected.annual, 4_800.0)

    def test_uncapping_delta_against_sellers_capped_taxable_value(self):
        est = estimate_property_tax(
            300_000, state="MI", county="Oakland", owner_occupied=False,
            current_taxable_value=90_000, millage_lookup=mi_millage,
        )
        self.assertEqual(est.seller_current.annual, 2_880.0)   # 90,000 * 32 / 1000
        self.assertEqual(est.uncapping_delta_annual, 4_620.0)  # 7,500 - 2,880

    def test_uncapping_warning_is_raised_for_michigan(self):
        est = estimate_property_tax(300_000, state="MI", owner_occupied=False,
                                    millage_lookup=mi_millage)
        self.assertTrue(any("uncapping" in w.lower() for w in est.warnings))

    def test_explicit_sev_overrides_derived_base(self):
        est = estimate_property_tax(300_000, state="MI", sev=120_000,
                                    owner_occupied=False, millage_lookup=mi_millage)
        self.assertEqual(est.non_homestead.annual, 6_000.0)


class PartialMillageRecordTests(unittest.TestCase):
    """import_millage.py permits a CSV with only one of the two mills columns —
    mills_for() then falls back to whichever is populated. That fallback must
    surface a warning, not silently apply the wrong (lower) rate to an investor."""

    def homestead_only(self, **_kwargs):
        return MillageRecord(
            state="MI", county="Oakland", jurisdiction="Royal Oak City",
            school_district="Royal Oak Schools",
            homestead_mills=32.0, non_homestead_mills=0.0,
            year=2025, source="test",
        )

    def non_homestead_only(self, **_kwargs):
        return MillageRecord(
            state="MI", county="Oakland", jurisdiction="Royal Oak City",
            school_district="Royal Oak Schools",
            homestead_mills=0.0, non_homestead_mills=50.0,
            year=2025, source="test",
        )

    def test_missing_non_homestead_mills_falls_back_and_warns(self):
        est = estimate_property_tax(300_000, state="MI", owner_occupied=False,
                                    millage_lookup=self.homestead_only)
        self.assertEqual(est.non_homestead.mills, 32.0)  # fell back to homestead rate
        self.assertTrue(any("non-homestead millage not on file" in w.lower() for w in est.warnings))

    def test_missing_homestead_mills_falls_back_and_warns(self):
        est = estimate_property_tax(300_000, state="MI", owner_occupied=True,
                                    millage_lookup=self.non_homestead_only)
        self.assertEqual(est.homestead.mills, 50.0)  # fell back to non-homestead rate
        self.assertTrue(any("homestead millage not on file" in w.lower() for w in est.warnings))

    def test_complete_record_raises_no_partial_data_warning(self):
        est = estimate_property_tax(300_000, state="MI", owner_occupied=False,
                                    millage_lookup=mi_millage)
        self.assertFalse(any("not on file" in w.lower() for w in est.warnings))


class FallbackTests(unittest.TestCase):
    def test_falls_back_to_state_median_without_millage_data(self):
        est = estimate_property_tax(300_000, state="MI", owner_occupied=False)
        self.assertEqual(est.method, "state_fallback")
        # MI median 1.38% owner-occupied, plus 18 non-PRE mills on a 50% SEV.
        self.assertAlmostEqual(est.non_homestead.effective_rate, 0.0228, places=4)
        self.assertTrue(any("millage" in w.lower() for w in est.warnings))

    def test_assessment_ratio_states_scale_the_investor_rate(self):
        # South Carolina: 4% owner-occupied vs 6% non-owner-occupied.
        est = estimate_property_tax(300_000, state="SC", owner_occupied=False)
        self.assertAlmostEqual(
            est.non_homestead.annual / est.homestead.annual, 1.5, places=3
        )

    def test_flat_exemption_states_gross_the_rate_back_up(self):
        # Florida investors lose the $50k homestead exemption.
        est = estimate_property_tax(300_000, state="FL", owner_occupied=False)
        self.assertGreater(est.non_homestead.annual, est.homestead.annual)

    def test_states_without_owner_occupancy_rules_are_equal(self):
        est = estimate_property_tax(300_000, state="VA", owner_occupied=False)
        self.assertEqual(est.non_homestead.annual, est.homestead.annual)

    def test_unknown_state_uses_national_median_and_warns(self):
        est = estimate_property_tax(300_000, address="somewhere unknown")
        self.assertIsNone(est.state)
        self.assertEqual(est.selected.annual, 3_300.0)  # 1.10% national median
        self.assertTrue(any("state" in w.lower() for w in est.warnings))

    def test_zero_price_returns_no_scenarios(self):
        est = estimate_property_tax(0, state="MI")
        self.assertIsNone(est.selected)


class StateRulesTests(unittest.TestCase):
    def test_all_states_and_dc_are_present(self):
        rules = {k for k in get_state_rules.__globals__["load_state_rules"]()
                 if not k.startswith("_")}
        self.assertEqual(len(rules), 51)

    def test_every_state_has_required_fields(self):
        data = get_state_rules.__globals__["load_state_rules"]()
        for code, rules in data.items():
            if code.startswith("_"):
                continue
            with self.subTest(state=code):
                self.assertIn("name", rules)
                self.assertGreater(rules["assessment_ratio"], 0)
                self.assertLessEqual(rules["assessment_ratio"], 1.0)
                self.assertGreater(rules["median_effective_rate"], 0)
                self.assertLess(rules["median_effective_rate"], 0.05)

    def test_michigan_carries_the_18_mill_non_homestead_levy(self):
        self.assertEqual(get_state_rules("MI")["non_homestead_extra_mills"], 18.0)


if __name__ == "__main__":
    unittest.main()
