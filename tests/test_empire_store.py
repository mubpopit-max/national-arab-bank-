import unittest

import app as app_module
from empire_store import db, post, balance, assert_epc_capacity


class EmpireLedgerTests(unittest.TestCase):
    def setUp(self):
        self.context = app_module.app.app_context()
        self.context.push()
        d = db()
        d.execute("DELETE FROM es_ledger")
        d.execute("DELETE FROM es_wallets")
        d.execute("DELETE FROM es_payments")
        d.execute("INSERT INTO es_settings(key,value) VALUES('epc_max_supply','10') "
                  "ON CONFLICT(key) DO UPDATE SET value='10'")

    def tearDown(self):
        self.context.pop()

    def test_balanced_credit_and_user_transfer(self):
        d = db()
        d.execute("BEGIN IMMEDIATE")
        post("TEST-CREDIT", [
            ("user:founder", "SDG", 5000),
            ("external:test_funding", "SDG", -5000),
        ], "test credit")
        d.commit()

        d.execute("BEGIN IMMEDIATE")
        post("TEST-TRANSFER", [
            ("user:founder", "SDG", -1200),
            ("user:buyer", "SDG", 1200),
        ], "test transfer")
        d.commit()

        self.assertEqual(balance("founder", "SDG"), 3800)
        self.assertEqual(balance("buyer", "SDG"), 1200)
        unbalanced = d.execute(
            "SELECT reference,currency,SUM(delta) total FROM es_ledger "
            "GROUP BY reference,currency HAVING total != 0"
        ).fetchall()
        self.assertEqual(unbalanced, [])

    def test_insufficient_balance_does_not_debit_wallet(self):
        d = db()
        d.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ValueError):
            post("TEST-OVERDRAW", [
                ("user:founder", "SDG", -1),
                ("external:test", "SDG", 1),
            ], "must fail")
        d.rollback()
        self.assertEqual(balance("founder", "SDG"), 0)

    def test_epc_supply_cap_counts_issued_units(self):
        d = db()
        d.execute("BEGIN IMMEDIATE")
        post("TEST-EPC-ISSUE", [
            ("user:founder", "EPC", 6),
            ("issuer:epc", "EPC", -6),
        ], "test issue")
        d.commit()
        assert_epc_capacity(4)
        with self.assertRaises(ValueError):
            assert_epc_capacity(5)

    def test_store_routes_and_founder_access(self):
        client = app_module.app.test_client()
        response = client.post("/login", data={
            "account_number": "5147234",
            "password": "CI-only-password-123!",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("الإمبراطورية إستوري".encode(), response.data)
        self.assertEqual(client.get("/store/healthz").status_code, 200)
        self.assertEqual(client.get("/store/admin").status_code, 200)
        self.assertEqual(client.get("/account_details").status_code, 302)


if __name__ == "__main__":
    unittest.main()
