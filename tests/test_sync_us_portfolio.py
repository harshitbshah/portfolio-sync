from unittest.mock import MagicMock, patch, call, ANY

import sync_us_portfolio as usp


def _mock_sheets_svc(rows):
    svc = MagicMock()
    (svc.spreadsheets.return_value
        .values.return_value
        .get.return_value
        .execute.return_value) = {"values": rows}
    return svc


# ── get_sheet_tickers() ───────────────────────────────────────────────────────

class TestGetSheetTickers:
    def test_returns_row_and_ticker_tuples(self):
        rows = [["Ticker"], ["AAPL"], ["MSFT"]]
        with patch("sync_us_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = usp.get_sheet_tickers()
        assert result == [(2, "AAPL"), (3, "MSFT")]

    def test_skips_header_row(self):
        rows = [["Ticker"], ["AAPL"]]
        with patch("sync_us_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = usp.get_sheet_tickers()
        assert (1, "Ticker") not in result

    def test_skips_non_equity_rows(self):
        # "Total" has lowercase letters → won't match ^[A-Z]{1,5}$
        # "123" starts with digit → no match
        # "" → no match
        rows = [["Ticker"], ["AAPL"], ["Total"], ["123"], [""]]
        with patch("sync_us_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = usp.get_sheet_tickers()
        assert [t for _, t in result] == ["AAPL"]

    def test_trims_whitespace_from_ticker(self):
        rows = [["Ticker"], ["  AAPL  "]]
        with patch("sync_us_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = usp.get_sheet_tickers()
        assert result[0][1] == "AAPL"


# ── get_all_holdings() ────────────────────────────────────────────────────────

class TestGetAllHoldings:
    def _accounts_resp(self, accounts):
        return {"data": {"accounts": accounts}}

    def _holdings_resp(self, ticker_qtys):
        return {
            "data": {
                "portfolio": {
                    "aggregateHoldings": {
                        "edges": [
                            {"node": {"quantity": qty, "holdings": [{"ticker": t}]}}
                            for t, qty in ticker_qtys
                        ]
                    }
                }
            }
        }

    def test_aggregates_quantities_across_brokerage_accounts(self):
        accounts = self._accounts_resp([
            {"id": "acc1", "deactivatedAt": None, "type": {"name": "brokerage"}},
            {"id": "acc2", "deactivatedAt": None, "type": {"name": "brokerage"}},
        ])
        with patch("sync_us_portfolio._monarch_request", side_effect=[
            accounts,
            self._holdings_resp([("AAPL", 10.0), ("MSFT", 5.0)]),
            self._holdings_resp([("AAPL", 3.0)]),
        ]):
            result = usp.get_all_holdings("token")
        assert result["AAPL"] == 13.0
        assert result["MSFT"] == 5.0

    def test_skips_non_brokerage_accounts(self):
        accounts = self._accounts_resp([
            {"id": "acc1", "deactivatedAt": None, "type": {"name": "bank"}},
        ])
        with patch("sync_us_portfolio._monarch_request", return_value=accounts):
            result = usp.get_all_holdings("token")
        assert result == {}

    def test_skips_deactivated_accounts(self):
        accounts = self._accounts_resp([
            {"id": "acc1", "deactivatedAt": "2024-01-01", "type": {"name": "brokerage"}},
        ])
        with patch("sync_us_portfolio._monarch_request", return_value=accounts):
            result = usp.get_all_holdings("token")
        assert result == {}

    def test_filters_out_cash_instruments(self):
        accounts = self._accounts_resp([
            {"id": "acc1", "deactivatedAt": None, "type": {"name": "brokerage"}},
        ])
        with patch("sync_us_portfolio._monarch_request", side_effect=[
            accounts,
            self._holdings_resp([("SPAXX", 100.0), ("FCASH", 50.0), ("AAPL", 10.0)]),
        ]):
            result = usp.get_all_holdings("token")
        assert "SPAXX" not in result
        assert "FCASH" not in result
        assert "AAPL" in result

    def test_filters_out_non_ticker_symbols(self):
        accounts = self._accounts_resp([
            {"id": "acc1", "deactivatedAt": None, "type": {"name": "brokerage"}},
        ])
        with patch("sync_us_portfolio._monarch_request", side_effect=[
            accounts,
            self._holdings_resp([("CUR:USD", 1000.0), ("AAPL", 10.0)]),
        ]):
            result = usp.get_all_holdings("token")
        assert "CUR:USD" not in result
        assert "AAPL" in result


# ── _shorten_account_name() ───────────────────────────────────────────────────

class TestShortenAccountName:
    def test_strips_numeric_mask(self):
        assert usp._shorten_account_name("Robinhood individual (...8902)") == "Robinhood individual"

    def test_strips_alphanumeric_mask(self):
        assert usp._shorten_account_name("ROTH IRA (...*****4882)") == "ROTH IRA"

    def test_no_mask_unchanged(self):
        assert usp._shorten_account_name("Fidelity 401k") == "Fidelity 401k"

    def test_empty_string_returns_empty(self):
        assert usp._shorten_account_name("") == ""


# ── get_holdings_by_account() ─────────────────────────────────────────────────

class TestGetHoldingsByAccount:
    def _accounts_resp(self, accounts):
        return {"data": {"accounts": accounts}}

    def _holdings_resp(self, ticker_qtys):
        return {
            "data": {
                "portfolio": {
                    "aggregateHoldings": {
                        "edges": [
                            {"node": {"quantity": qty, "holdings": [{"ticker": t}]}}
                            for t, qty in ticker_qtys
                        ]
                    }
                }
            }
        }

    def test_returns_per_account_breakdown(self):
        accounts = self._accounts_resp([
            {"id": "acc1", "displayName": "Robinhood IRA (...1111)", "deactivatedAt": None, "type": {"name": "brokerage"}},
            {"id": "acc2", "displayName": "Robinhood individual (...2222)", "deactivatedAt": None, "type": {"name": "brokerage"}},
        ])
        with patch("sync_us_portfolio._monarch_request", side_effect=[
            accounts,
            self._holdings_resp([("HROW", 5.0)]),
            self._holdings_resp([("HROW", 10.0)]),
        ]):
            result = usp.get_holdings_by_account("token")
        assert result["HROW"]["Robinhood IRA"] == 5.0
        assert result["HROW"]["Robinhood individual"] == 10.0

    def test_skips_non_brokerage_accounts(self):
        accounts = self._accounts_resp([
            {"id": "acc1", "displayName": "Chase Checking", "deactivatedAt": None, "type": {"name": "checking"}},
        ])
        with patch("sync_us_portfolio._monarch_request", return_value=accounts):
            result = usp.get_holdings_by_account("token")
        assert result == {}

    def test_skips_skip_tickers(self):
        accounts = self._accounts_resp([
            {"id": "acc1", "displayName": "Robinhood individual", "deactivatedAt": None, "type": {"name": "brokerage"}},
        ])
        with patch("sync_us_portfolio._monarch_request", side_effect=[
            accounts,
            self._holdings_resp([("SGOV", 100.0), ("AAPL", 5.0)]),
        ]):
            result = usp.get_holdings_by_account("token")
        assert "SGOV" not in result
        assert "AAPL" in result


# ── _get_or_create_tab() ─────────────────────────────────────────────────────

class TestGetOrCreateTab:
    def _meta_resp(self, titles):
        return {"sheets": [{"properties": {"title": t, "sheetId": i + 10}} for i, t in enumerate(titles)]}

    def test_returns_existing_sheet_id(self):
        mock_svc = MagicMock()
        mock_svc.spreadsheets.return_value.get.return_value.execute.return_value = (
            self._meta_resp(["US Portfolio", "Holdings by Account"])
        )
        result = usp._get_or_create_tab(mock_svc, "Holdings by Account")
        assert result == 11  # second tab → sheetId 11

    def test_creates_tab_when_missing(self):
        mock_svc = MagicMock()
        mock_svc.spreadsheets.return_value.get.return_value.execute.return_value = (
            self._meta_resp(["US Portfolio"])
        )
        mock_svc.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{"addSheet": {"properties": {"sheetId": 99}}}]
        }
        result = usp._get_or_create_tab(mock_svc, "Holdings by Account")
        assert result == 99
        mock_svc.spreadsheets.return_value.batchUpdate.assert_called_once()

    def test_recovers_when_create_fails_with_already_exists(self):
        from googleapiclient.errors import HttpError
        mock_svc = MagicMock()
        # First get returns no matching tab, create fails, second get finds it
        mock_svc.spreadsheets.return_value.get.return_value.execute.side_effect = [
            self._meta_resp(["US Portfolio"]),
            self._meta_resp(["US Portfolio", "Holdings by Account"]),
        ]
        already_exists_err = HttpError(
            resp=MagicMock(status=400),
            content=b'{"error":{"message":"already exists"}}',
        )
        mock_svc.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = (
            already_exists_err
        )
        result = usp._get_or_create_tab(mock_svc, "Holdings by Account")
        assert result == 11  # second tab in second meta_resp


# ── sync_account_tab() ────────────────────────────────────────────────────────

class TestSyncAccountTab:
    def _make_svc(self, existing_tabs=None):
        mock_svc = MagicMock()
        titles = existing_tabs or ["Holdings by Account"]
        mock_svc.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": t, "sheetId": i}} for i, t in enumerate(titles)]
        }
        return mock_svc

    def test_writes_header_row(self):
        mock_svc = self._make_svc()
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.sync_account_tab({"AAPL": {"Robinhood IRA": 5.0}})
        update_call = mock_svc.spreadsheets.return_value.values.return_value.update
        body = update_call.call_args[1]["body"]
        assert body["values"][0] == ["Ticker", "Account", "Qty"]

    def test_writes_rows_sorted_by_ticker_then_account(self):
        mock_svc = self._make_svc()
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.sync_account_tab({
                "MSFT": {"IRA": 3.0},
                "AAPL": {"Individual": 10.0, "IRA": 5.0},
            })
        update_call = mock_svc.spreadsheets.return_value.values.return_value.update
        body = update_call.call_args[1]["body"]
        rows = body["values"][1:]  # skip header
        assert rows[0] == ["AAPL", "IRA", 5.0]
        assert rows[1] == ["AAPL", "Individual", 10.0]
        assert rows[2] == ["MSFT", "IRA", 3.0]

    def test_clears_before_writing(self):
        mock_svc = self._make_svc()
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        mock_svc.spreadsheets.return_value.values.return_value.clear.assert_called_once()

    def test_creates_tab_if_missing(self):
        mock_svc = self._make_svc(existing_tabs=["US Portfolio"])
        mock_svc.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{"addSheet": {"properties": {"sheetId": 42}}}]
        }
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        # batchUpdate called once for tab creation, once for header formatting
        assert mock_svc.spreadsheets.return_value.batchUpdate.call_count == 2

    def test_formats_header_bold_with_frozen_row(self):
        mock_svc = self._make_svc()
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        format_call = mock_svc.spreadsheets.return_value.batchUpdate
        body = format_call.call_args[1]["body"]
        request_types = [list(r.keys())[0] for r in body["requests"]]
        assert "repeatCell" in request_types
        assert "updateSheetProperties" in request_types


# ── sync() ────────────────────────────────────────────────────────────────────

class TestSync:
    def _run(self, holdings, sheet_tickers, old_quantities=None):
        if old_quantities is None:
            old_quantities = {}
        with patch("sync_us_portfolio.get_all_holdings", return_value=holdings), \
             patch("sync_us_portfolio.get_sheet_tickers", return_value=sheet_tickers), \
             patch("sync_us_portfolio.get_sheet_quantities", return_value=old_quantities), \
             patch("sync_us_portfolio.delete_closed_rows"), \
             patch("sync_us_portfolio.insert_new_rows"), \
             patch("sync_us_portfolio.update_quantities"), \
             patch("sync_us_portfolio.get_holdings_by_account", return_value={}), \
             patch("sync_us_portfolio.sync_account_tab"):
            usp.sync("token")

    def test_emits_diff_for_quantity_increase(self, capsys):
        self._run(
            holdings={"HROW": 15.0},
            sheet_tickers=[(2, "HROW")],
            old_quantities={"HROW": 10.0},
        )
        assert "[US] Diff: HROW +5.00" in capsys.readouterr().out

    def test_emits_diff_for_quantity_decrease(self, capsys):
        self._run(
            holdings={"HROW": 5.0},
            sheet_tickers=[(2, "HROW")],
            old_quantities={"HROW": 10.0},
        )
        assert "[US] Diff: HROW -5.00" in capsys.readouterr().out

    def test_no_diff_when_quantity_unchanged(self, capsys):
        self._run(
            holdings={"AAPL": 10.0},
            sheet_tickers=[(2, "AAPL")],
            old_quantities={"AAPL": 10.0},
        )
        assert "[US] Diff:" not in capsys.readouterr().out

    def test_suppresses_diff_below_threshold(self, capsys):
        # Floating-point noise from Monarch: diff of 0.002 should not be reported
        self._run(
            holdings={"ALAB": 100.002},
            sheet_tickers=[(2, "ALAB")],
            old_quantities={"ALAB": 100.0},
        )
        assert "[US] Diff:" not in capsys.readouterr().out

    def test_reports_diff_at_threshold(self, capsys):
        self._run(
            holdings={"ALAB": 100.01},
            sheet_tickers=[(2, "ALAB")],
            old_quantities={"ALAB": 100.0},
        )
        assert "[US] Diff: ALAB" in capsys.readouterr().out

    def test_emits_added_for_new_position(self, capsys):
        self._run(
            holdings={"NVDA": 92.3431},
            sheet_tickers=[],
        )
        assert "[US] Added: NVDA" in capsys.readouterr().out

    def test_emits_closed_for_removed_position(self, capsys):
        self._run(
            holdings={},
            sheet_tickers=[(2, "ZS")],
        )
        assert "[US] Closed: ZS" in capsys.readouterr().out

    def test_calls_sync_account_tab(self):
        breakdown = {"HROW": {"IRA": 5.0, "Individual": 10.0}}
        with patch("sync_us_portfolio.get_all_holdings", return_value={"HROW": 15.0}), \
             patch("sync_us_portfolio.get_sheet_tickers", return_value=[(2, "HROW")]), \
             patch("sync_us_portfolio.get_sheet_quantities", return_value={"HROW": 15.0}), \
             patch("sync_us_portfolio.update_quantities"), \
             patch("sync_us_portfolio.get_holdings_by_account", return_value=breakdown) as mock_gba, \
             patch("sync_us_portfolio.sync_account_tab") as mock_sat:
            usp.sync("token")
        mock_gba.assert_called_once_with("token")
        mock_sat.assert_called_once_with(breakdown)
