import sys
import unittest

import importlib.util
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import database

module_path = Path(__file__).resolve().parents[1] / "scripts" / "generate_realestate_pdf.py"
spec = importlib.util.spec_from_file_location("generate_realestate_pdf", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NormalizeReportDataTests(unittest.TestCase):
    def test_normalizes_current_report_schema(self):
        data = {
            "property_address": "123 Main Street, Austin, TX 78701",
            "report_date": "April 29, 2026",
            "listing_price": 425000,
            "property_score": 76,
            "categories": {
                "Value & Comps": {"score": 78, "weight": "25%"},
                "Income Potential": {"score": 72, "weight": "20%"},
            },
            "comparable_sales": [{
                "address": "125 Oak Ave",
                "price": 430000,
                "sqft": 1900,
                "beds": 3,
                "baths": 2,
                "distance": "0.3 mi",
                "sale_date": "2026-03-15",
            }],
            "estimated_rent": 2650,
            "net_cash_flow": 320,
            "cap_rate": 7.2,
            "cash_on_cash": 9.8,
            "gross_rent_multiplier": 13.4,
            "vacancy_rate": 5.0,
        }

        normalized = module.normalize_report_data(data)

        self.assertEqual(normalized["address"], "123 Main Street, Austin, TX 78701")
        self.assertEqual(normalized["price"], "$425,000")
        self.assertEqual(normalized["overall_score"], 76)
        self.assertEqual(normalized["comps"][0]["address"], "125 Oak Ave")
        self.assertEqual(normalized["cashflow"]["items"][0]["item"], "Gross Rental Income")
        self.assertEqual(normalized["investment_metrics"]["cap_rate"], "7.2%")

    def test_active_users_only_are_returned_for_admin_list(self):
        engine = create_engine("sqlite://")
        database.Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)

        with Session() as session:
            session.add_all([
                database.User(tenant_id=1, email="active@example.com", password_hash="x", full_name="Active", role="realtor", is_active=True),
                database.User(tenant_id=1, email="inactive@example.com", password_hash="x", full_name="Inactive", role="realtor", is_active=False),
            ])
            session.commit()

            users = session.query(database.User).filter(database.User.tenant_id == 1, database.User.is_active.is_(True)).all()
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0].email, "active@example.com")


if __name__ == "__main__":
    unittest.main()
