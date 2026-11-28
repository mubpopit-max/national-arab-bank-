import unittest

import app as app_module
from empire_store import db, post, balance, assert_epc_capacity, fx_reserve_balance, cash_sdg_balance


class EmpireLedgerTests(unittest.TestCase):
    def setUp(self):
        self.context = app_module.app.app_context()
        self.context.push()
        d = db()
        d.execute("DELETE FROM es_ledger")
        d.execute("DELETE FROM es_wallets")
        d.execute("DELETE FROM es_payments")
        d.execute("DELETE FROM es_withdrawals")
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

    def test_exchange_reserve_is_separate_from_protection_fund(self):
        d = db()
        d.execute("BEGIN IMMEDIATE")
        post("TEST-PROTECTION-FEE", [
            ("protection:fund", "SDG", 1000),
            ("external:fees", "SDG", -1000),
        ], "test protection fee")
        post("TEST-FX-RESERVE", [
            ("fx:reserve", "SDG", 500),
            ("external:cash_reserve", "SDG", -500),
        ], "test fx reserve")
        d.commit()

        self.assertEqual(fx_reserve_balance("SDG"), 500)
        protection = d.execute(
            "SELECT COALESCE(SUM(delta),0) n FROM es_ledger "
            "WHERE account='protection:fund' AND currency='SDG'"
        ).fetchone()["n"]
        self.assertEqual(protection, 1000)

    def test_both_currency_exchange_directions_execute_atomically(self):
        client = app_module.app.test_client()
        login = client.post("/login", data={
            "account_number": "5147234",
            "password": "CI-only-password-123!",
        }, follow_redirects=True)
        self.assertEqual(login.status_code, 200)
        with client.session_transaction() as session:
            username = session["username"]
            csrf_token = session["csrf"]

        d = db()
        d.execute("BEGIN IMMEDIATE")
        post("TEST-FX-USER-URC", [
            ("user:" + username, "URC", 100),
            ("external:test_fx_user_urc", "URC", -100),
        ], "test user URC funding")
        post("TEST-FX-USER-SDG", [
            ("user:" + username, "SDG", 100000),
            ("external:test_fx_user_sdg", "SDG", -100000),
        ], "test user SDG funding")
        d.commit()

        first_quote = client.get("/store/exchange/quote?direction=URC_TO_SDG&amount=10")
        self.assertEqual(first_quote.status_code, 200)
        first_quote_data = first_quote.get_json()
        self.assertEqual(cash_sdg_balance(), 0)
        self.assertEqual(fx_reserve_balance("SDG"), 0)
        self.assertTrue(first_quote_data["valid"])
        self.assertEqual(first_quote_data["wallet_balance"], 100)
        self.assertEqual(first_quote_data["max_convertible"], 97)

        # Conversion preview and confirmation depend on digital wallet balance,
        # never on the cash payout reserve.
        oversized = client.get("/store/exchange/quote?direction=URC_TO_SDG&amount=200").get_json()
        self.assertFalse(oversized["valid"])
        self.assertEqual(oversized["converted"], 1120000)
        self.assertEqual(oversized["wallet_balance"], 100)
        self.assertEqual(oversized["max_convertible"], 97)
        self.assertNotIn("سيولة", oversized.get("message", ""))

        first = client.post("/store/exchange", data={
            "csrf": csrf_token, "direction": "URC_TO_SDG", "amount": "10",
            "quote_id": first_quote_data["quote_id"],
        }, follow_redirects=True)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(fx_reserve_balance("SDG"), 0)
        self.assertEqual(balance(username, "SDG"), 156000)
        self.assertEqual(d.execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='burn:urc' AND currency='URC'").fetchone()["n"], 10)
        balance_after_first = balance(username, "URC")
        replay = client.post("/store/exchange", data={
            "csrf": csrf_token, "direction": "URC_TO_SDG", "amount": "10",
            "quote_id": first_quote_data["quote_id"],
        }, follow_redirects=True)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(balance(username, "URC"), balance_after_first)

        second_quote = client.get("/store/exchange/quote?direction=SDG_TO_URC&amount=56000")
        self.assertEqual(second_quote.status_code, 200)
        second_quote_data = second_quote.get_json()
        self.assertTrue(second_quote_data["valid"])
        second = client.post("/store/exchange", data={
            "csrf": csrf_token, "direction": "SDG_TO_URC", "amount": "56000",
            "quote_id": second_quote_data["quote_id"],
        }, follow_redirects=True)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(fx_reserve_balance("URC"), 0)
        self.assertEqual(balance(username, "URC"), 99)

        protection_fees = d.execute(
            "SELECT COALESCE(SUM(delta),0) n FROM es_ledger "
            "WHERE account='protection:fund' AND currency IN ('SDG','URC')"
        ).fetchone()["n"]
        self.assertGreater(protection_fees, 0)
        unbalanced = d.execute(
            "SELECT reference,currency,SUM(delta) total FROM es_ledger "
            "GROUP BY reference,currency HAVING total != 0"
        ).fetchall()
        self.assertEqual(unbalanced, [])

    def test_cash_liquidity_is_required_only_for_external_sdg_withdrawals(self):
        client = app_module.app.test_client()
        login = client.post("/login", data={
            "account_number": "5147234",
            "password": "CI-only-password-123!",
        }, follow_redirects=True)
        self.assertEqual(login.status_code, 200)
        with client.session_transaction() as session:
            username = session["username"]
            csrf_token = session["csrf"]

        d = db()
        d.execute("BEGIN IMMEDIATE")
        post("TEST-WITHDRAW-USER-SDG", [
            ("user:" + username, "SDG", 10000),
            ("external:test_withdraw_user_sdg", "SDG", -10000),
        ], "test withdrawal wallet funding")
        post("TEST-DIGITAL-FX-POOL-NOT-CASH", [
            ("fx:reserve", "SDG", 1000000),
            ("external:test_digital_fx_pool", "SDG", -1000000),
        ], "internal FX reserve is not physical cash")
        d.commit()

        blocked = client.post("/store/withdrawal", data={
            "csrf": csrf_token, "amount": "500",
            "payout_method": "BANK_TRANSFER", "payout_details": "Test bank account",
        }, follow_redirects=True)
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(d.execute("SELECT COUNT(*) n FROM es_withdrawals").fetchone()["n"], 0)
        self.assertEqual(balance(username, "SDG"), 10000)

        d.execute("BEGIN IMMEDIATE")
        post("TEST-CASH-WITHDRAWAL-RESERVE", [
            ("cash:sdg_reserve", "SDG", 600),
            ("external:test_physical_cash", "SDG", -600),
        ], "confirmed physical cash for withdrawal")
        d.commit()

        accepted = client.post("/store/withdrawal", data={
            "csrf": csrf_token, "amount": "500",
            "payout_method": "BANK_TRANSFER", "payout_details": "Test bank account",
        }, follow_redirects=True)
        self.assertEqual(accepted.status_code, 200)
        withdrawal = d.execute("SELECT * FROM es_withdrawals WHERE status='PENDING'").fetchone()
        self.assertIsNotNone(withdrawal)
        self.assertEqual(cash_sdg_balance(), 600)
        self.assertEqual(balance(username, "SDG"), 9485)

        over_reserved = client.post("/store/withdrawal", data={
            "csrf": csrf_token, "amount": "200",
            "payout_method": "BANK_TRANSFER", "payout_details": "Test bank account",
        }, follow_redirects=True)
        self.assertEqual(over_reserved.status_code, 200)
        self.assertEqual(d.execute("SELECT COUNT(*) n FROM es_withdrawals").fetchone()["n"], 1)

        paid = client.post("/store/admin/withdrawals", data={
            "csrf": csrf_token, "id": str(withdrawal["id"]), "action": "PAID",
        }, follow_redirects=True)
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(cash_sdg_balance(), 100)
        self.assertEqual(d.execute("SELECT status FROM es_withdrawals WHERE id=?", (withdrawal["id"],)).fetchone()["status"], "PAID")
        unbalanced = d.execute(
            "SELECT reference,currency,SUM(delta) total FROM es_ledger "
            "GROUP BY reference,currency HAVING total != 0"
        ).fetchall()
        self.assertEqual(unbalanced, [])

    def test_wallet_balance_uses_ledger_not_stale_cache(self):
        d = db()
        d.execute("BEGIN IMMEDIATE")
        post("TEST-LEDGER-WALLET", [
            ("user:founder", "URC", 500),
            ("external:test_ledger_wallet", "URC", -500),
        ], "test wallet funding")
        d.commit()

        # Simulate a stale cache row; owner balance must still follow ledger history.
        d.execute("UPDATE es_wallets SET balance=0 WHERE username='founder' AND currency='URC'")
        self.assertEqual(balance("founder", "URC"), 500)

        d.execute("BEGIN IMMEDIATE")
        post("TEST-LEDGER-WALLET-DEBIT", [
            ("user:founder", "URC", -100),
            ("external:test_ledger_wallet_debit", "URC", 100),
        ], "test wallet debit")
        d.commit()
        self.assertEqual(balance("founder", "URC"), 400)
        cached = d.execute("SELECT balance FROM es_wallets WHERE username='founder' AND currency='URC'").fetchone()
        self.assertEqual(cached["balance"], 400)

    def test_owner_configured_fx_rate_controls_quote_and_execution(self):
        client = app_module.app.test_client()
        login = client.post("/login", data={
            "account_number": "5147234",
            "password": "CI-only-password-123!",
        }, follow_redirects=True)
        self.assertEqual(login.status_code, 200)
        with client.session_transaction() as session:
            csrf_token = session["csrf"]

        d = db()
        d.execute("UPDATE es_settings SET value='manual' WHERE key='fx_rate_mode'")
        d.execute("UPDATE es_settings SET value='6000' WHERE key='fx_manual_rate'")
        d.execute("BEGIN IMMEDIATE")
        post("TEST-FX-RATE-USER", [
            ("user:founder", "URC", 100),
            ("external:test_fx_rate_user", "URC", -100),
        ], "test FX user funding")
        post("TEST-FX-RATE-RESERVE", [
            ("fx:reserve", "SDG", 1000000),
            ("external:test_fx_rate_reserve", "SDG", -1000000),
        ], "test FX reserve funding")
        d.commit()

        old_quote = client.get("/store/exchange/quote?direction=URC_TO_SDG&amount=10").get_json()
        self.assertTrue(old_quote["valid"])
        self.assertEqual(old_quote["rate"], 6000)
        self.assertEqual(old_quote["converted"], 60000)

        # A price change after quoting must invalidate that quote, never execute stale terms.
        d.execute("UPDATE es_settings SET value='6200' WHERE key='fx_manual_rate'")
        stale = client.post("/store/exchange", data={
            "csrf": csrf_token, "direction": "URC_TO_SDG", "amount": "10",
            "quote_id": old_quote["quote_id"],
        }, follow_redirects=True)
        self.assertEqual(stale.status_code, 200)
        self.assertEqual(balance("founder", "SDG"), 0)

        fresh_quote = client.get("/store/exchange/quote?direction=URC_TO_SDG&amount=10").get_json()
        self.assertTrue(fresh_quote["valid"])
        self.assertEqual(fresh_quote["rate"], 6200)
        self.assertEqual(fresh_quote["converted"], 62000)
        executed = client.post("/store/exchange", data={
            "csrf": csrf_token, "direction": "URC_TO_SDG", "amount": "10",
            "quote_id": fresh_quote["quote_id"],
        }, follow_redirects=True)
        self.assertEqual(executed.status_code, 200)
        self.assertEqual(balance("founder", "SDG"), 62000)
        self.assertEqual(fx_reserve_balance("SDG"), 1000000)
        self.assertEqual(balance("founder", "URC"), 89)
        self.assertEqual(d.execute("SELECT COALESCE(SUM(delta),0) n FROM es_ledger WHERE account='burn:urc' AND currency='URC'").fetchone()["n"], 10)

    def test_urc_supply_cap_counts_issued_units(self):
        d = db()
        d.execute("BEGIN IMMEDIATE")
        post("TEST-URC-ISSUE", [
            ("user:founder", "URC", 6),
            ("issuer:urc", "URC", -6),
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
        manifest = client.get("/store/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertIn(b"/store/icon.svg?v=3", manifest.data)
        icon = client.get("/store/icon.svg")
        self.assertEqual(icon.status_code, 200)
        self.assertIn(b"<svg", icon.data)
        self.assertIn(b"linearGradient", icon.data)
        exchange_page = client.get("/store/exchange")
        self.assertEqual(exchange_page.status_code, 200)
        self.assertIn(b'id="fxFrom"', exchange_page.data)
        self.assertIn(b'id="fxTo"', exchange_page.data)
        self.assertIn(b'id="fxSwap"', exchange_page.data)
        self.assertIn(b'id="fxResult"', exchange_page.data)
        self.assertIn("تحويل العملة".encode(), exchange_page.data)
        self.assertNotIn("صرف العملات".encode(), exchange_page.data)
        self.assertEqual(client.get("/store/admin").status_code, 200)
        self.assertEqual(client.get("/account_details").status_code, 302)


if __name__ == "__main__":
    unittest.main()
