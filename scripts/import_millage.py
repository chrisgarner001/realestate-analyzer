"""
Bulk-import jurisdiction millage rates into the millage_rates table.

Michigan
--------
The Treasury's Property Tax Estimator is backed by an annual millage rate
report published as a spreadsheet. Export it to CSV and run:

    python scripts/import_millage.py michigan_2025_millage.csv --state MI \
        --tax-year 2025 --format mi-treasury \
        --source "Michigan Dept. of Treasury 2025 millage rates"

Any other state
---------------
Normalize the county's published rate table to these columns and run with
--format generic (the default):

    county,jurisdiction,school_district,homestead_mills,non_homestead_mills

Rates must be expressed in MILLS (dollars per $1,000 of taxable value). Pass
--rates-are-percent to convert a percentage column automatically.

Idempotent: re-importing the same state/county/jurisdiction/school/year updates
the existing row rather than duplicating it.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.database import SessionLocal, MillageRate, init_db  # noqa: E402
from web.property_tax import VALID_STATES  # noqa: E402

# Michigan Treasury's export uses these headers; map them onto our schema.
COLUMN_ALIASES = {
    "generic": {
        "county": ["county"],
        "jurisdiction": ["jurisdiction", "local_unit", "city_township_village", "unit"],
        "school_district": ["school_district", "school", "district"],
        "homestead_mills": ["homestead_mills", "homestead", "pre_mills", "pre"],
        "non_homestead_mills": ["non_homestead_mills", "non_homestead", "nonhomestead",
                                "non_pre_mills", "non_pre", "nonpre"],
    },
    "mi-treasury": {
        "county": ["county", "county name"],
        "jurisdiction": ["local unit", "local unit name", "city/township/village",
                         "unit name", "jurisdiction"],
        "school_district": ["school district", "school district name", "school"],
        "homestead_mills": ["principal residence", "pre/mbt", "homestead",
                            "pre mills", "principal residence exemption"],
        "non_homestead_mills": ["non-principal residence", "non-pre", "non homestead",
                                "non-homestead", "non-pre mills"],
    },
}


def _normalize(header: str) -> str:
    return (header or "").strip().lower().replace("_", " ")


def _build_index(fieldnames, fmt):
    aliases = COLUMN_ALIASES[fmt]
    normalized = {_normalize(name): name for name in fieldnames or []}
    resolved = {}
    for target, candidates in aliases.items():
        for candidate in candidates:
            key = _normalize(candidate)
            if key in normalized:
                resolved[target] = normalized[key]
                break
    missing = [f for f in ("county", "jurisdiction") if f not in resolved]
    if missing:
        raise SystemExit(
            f"CSV is missing required column(s): {', '.join(missing)}.\n"
            f"Found headers: {', '.join(fieldnames or [])}"
        )
    if "homestead_mills" not in resolved and "non_homestead_mills" not in resolved:
        raise SystemExit("CSV must contain a homestead and/or non-homestead millage column.")
    return resolved


def _mills(row, index, key, as_percent):
    column = index.get(key)
    if not column:
        return 0.0
    raw = (row.get(column) or "").strip().replace(",", "").replace("$", "").replace("%", "")
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value * 10.0 if as_percent else value


def import_csv(path, state, tax_year, fmt, source, as_percent, dry_run):
    state = state.upper()
    if state not in VALID_STATES:
        raise SystemExit(f"'{state}' is not a valid US state code.")

    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        index = _build_index(reader.fieldnames, fmt)
        rows = list(reader)

    if dry_run:
        print(f"Column mapping: {index}")
        for row in rows[:5]:
            print({
                "county": row.get(index["county"], "").strip(),
                "jurisdiction": row.get(index["jurisdiction"], "").strip(),
                "school_district": row.get(index.get("school_district", ""), "").strip(),
                "homestead_mills": _mills(row, index, "homestead_mills", as_percent),
                "non_homestead_mills": _mills(row, index, "non_homestead_mills", as_percent),
            })
        print(f"\nDry run: {len(rows)} rows would be imported for {state} {tax_year}.")
        return

    init_db()
    db = SessionLocal()
    inserted = updated = skipped = 0
    try:
        for row in rows:
            county = (row.get(index["county"]) or "").strip()
            jurisdiction = (row.get(index["jurisdiction"]) or "").strip()
            if not county or not jurisdiction:
                skipped += 1
                continue
            school = (row.get(index.get("school_district", "")) or "").strip()
            homestead = _mills(row, index, "homestead_mills", as_percent)
            non_homestead = _mills(row, index, "non_homestead_mills", as_percent)
            if not homestead and not non_homestead:
                skipped += 1
                continue

            existing = db.query(MillageRate).filter_by(
                state=state, county=county, jurisdiction=jurisdiction,
                school_district=school, tax_year=tax_year,
            ).first()
            if existing:
                existing.homestead_mills = homestead
                existing.non_homestead_mills = non_homestead
                existing.source = source
                updated += 1
            else:
                db.add(MillageRate(
                    state=state, county=county, jurisdiction=jurisdiction,
                    school_district=school, homestead_mills=homestead,
                    non_homestead_mills=non_homestead, tax_year=tax_year, source=source,
                ))
                inserted += 1
            if (inserted + updated) % 500 == 0:
                db.commit()
        db.commit()
    finally:
        db.close()
    print(f"{state} {tax_year}: {inserted} inserted, {updated} updated, {skipped} skipped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="Path to the millage rate CSV")
    parser.add_argument("--state", required=True, help="Two-letter state code, e.g. MI")
    parser.add_argument("--tax-year", type=int, required=True)
    parser.add_argument("--format", dest="fmt", choices=sorted(COLUMN_ALIASES), default="generic")
    parser.add_argument("--source", default="", help="Attribution shown to users")
    parser.add_argument("--rates-are-percent", action="store_true",
                        help="Input column is a percentage of taxable value, not mills")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the column mapping and a sample without writing")
    args = parser.parse_args()

    import_csv(args.csv_path, args.state, args.tax_year, args.fmt,
               args.source or f"{args.state} {args.tax_year} millage table",
               args.rates_are_percent, args.dry_run)


if __name__ == "__main__":
    main()
