"""Tests for web/owned_import.py against a synthetic fixture shaped like the
IRES inventory export (never the real file; Reports/ is gitignored)."""
from datetime import date
from pathlib import Path

import pytest

import owned_asset
from owned_import import (
    ImportError_, MAX_BYTES, apply_loans, match_tier, normalize_address, parse_inventory,
    parse_loans, parse_money, row_to_inputs_dict,
)

FIXTURE = Path(__file__).parent / "fixtures" / "inventory_sample.csv"
RUN = date(2026, 9, 23)


@pytest.fixture()
def rows():
    return parse_inventory(FIXTURE.read_bytes(), run_date=RUN)


class TestParseInventory:
    def test_finds_header_and_skips_preamble_and_footer(self, rows):
        assert [r.address for r in rows] == ["Elm 12345", "Oak 23456", "Pine 34567", "Birch 45678"]

    def test_money_and_taxes(self, rows):
        elm = rows[0].fields
        assert elm["arv"] == 140000
        assert elm["rehab_quote"] == 21500
        assert elm["annual_tax"] == 4000

    def test_multiline_notes_kept(self, rows):
        assert "PLUMBING" in rows[0].fields["notes"] and "REHAB" in rows[0].fields["notes"]

    def test_zero_comps_and_no_rehab_are_unknown_not_zero(self, rows):
        birch = rows[3].fields
        assert birch["arv"] is None and birch["rehab_quote"] is None
        assert "Estimated ARV" in rows[3].flags and "Unknown rehab" in rows[3].flags

    def test_forfeiture_history_detected(self, rows):
        assert rows[1].fields["lc_forfeiture_history"] is True
        assert "LC forfeiture history" in rows[1].flags
        assert rows[0].fields["lc_forfeiture_history"] is False

    def test_pending_rows_skipped_by_default(self, rows):
        assert rows[2].skipped_pending is True and "Pending sale" in rows[2].flags
        assert not rows[0].skipped_pending

    def test_stale_value_flag(self, rows):
        assert "Stale value" in rows[1].flags          # Zillow date 01/10/2025
        assert "Stale value" not in rows[0].flags

    def test_months_vacant(self, rows):
        assert rows[0].fields["months_vacant"] >= 19


class TestLimitsAndErrors:
    def test_oversize_rejected(self):
        with pytest.raises(ImportError_) as e:
            parse_inventory(b"x" * (MAX_BYTES + 1))
        assert e.value.status == 413

    def test_missing_columns_named(self):
        data = b"Building Name,Building City\nElm 1,Town\n"
        with pytest.raises(ImportError_) as e:
            parse_inventory(data)
        assert e.value.status == 422
        assert "Marketing: Comps" in e.value.missing

    def test_no_header_row(self):
        with pytest.raises(ImportError_):
            parse_inventory(b"a,b,c\n1,2,3\n")

    def test_utf8_bom_and_cp1252(self):
        text = FIXTURE.read_text(encoding="utf-8")
        assert len(parse_inventory(("﻿" + text).encode("utf-8"), RUN)) == 4
        assert len(parse_inventory(text.replace("Sample", "S\xe4mple").encode("cp1252"), RUN)) == 4

    def test_too_many_rows(self):
        header = FIXTURE.read_text(encoding="utf-8").splitlines()[6]
        body = "\n".join(f'P,Elm {i},Town,Vacant,,,,,"$100,000.00 ",,,Off Market,,"$1.00 ",,,"$1.00 ","$1.00 ",,P'
                         for i in range(501))
        with pytest.raises(ImportError_):
            parse_inventory((header + "\n" + body).encode())


class TestAddresses:
    def test_normalize_handles_ires_order_and_suffixes(self):
        a = normalize_address("Kingsville 20083")
        b = normalize_address("20083 Kingsville St.")
        assert a["number"] == b["number"] == "20083" and a["street"] == b["street"] == "kingsville"

    def test_match_tiers(self):
        assert match_tier("Elm 12345", "Sample Town", "12345 Elm Street", "sample town") == "exact"
        assert match_tier("Elm 12345", "", "12345 Elm St", "Sample Town") == "likely"
        assert match_tier("Elm 12345", "Sample Town", "Elm 12345 Unit 2", "Sample Town") == "likely"
        assert match_tier("Elm 12345", "Sample Town", "12346 Elm St", "Sample Town") == "none"

    def test_parse_money(self):
        assert parse_money("$1,234.56 ") == 1234.56
        assert parse_money("($500.00)") == -500
        assert parse_money("") is None


class TestLoans:
    def test_loans_file_applied_by_address(self, rows):
        loans = parse_loans(b"Building Name,Building City,Loan Payoff,Loan Rate,Loan P&I\n"
                            b"12345 Elm St,Sample Town,\"$40,000\",7.5%,$410\n"
                            b"99999 Nowhere Rd,Sample Town,$1,5,$1\n")
        unmatched = apply_loans(rows, loans)
        assert rows[0].fields["loan_payoff"] == 40000 and rows[0].fields["loan_rate"] == 7.5
        assert "No loan entered" not in rows[0].flags
        assert [u["address_line"] for u in unmatched] == ["99999 Nowhere Rd"]


class TestToEngine:
    def test_row_runs_through_engine(self, rows):
        kwargs = row_to_inputs_dict(rows[0])
        p = owned_asset.PropertyInputs(**kwargs, as_is_value=90000)
        out = owned_asset.analyze(p, with_break_even=False)
        assert out["recommendation"]["strategy"] in owned_asset.STRATEGIES

    def test_missing_values_tagged_default(self, rows):
        kwargs = row_to_inputs_dict(rows[3])
        assert kwargs["sources"]["arv"] == "DEFAULT" and kwargs["sources"]["rehab_budget"] == "DEFAULT"
