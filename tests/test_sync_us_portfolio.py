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


# ── _format_breakdown() ───────────────────────────────────────────────────────

class TestFormatBreakdown:
    def test_single_account(self):
        assert usp._format_breakdown({"Robinhood IRA": 5.0}) == "Robinhood IRA: 5"

    def test_multiple_accounts_sorted_alphabetically(self):
        result = usp._format_breakdown({"Individual": 10.0, "IRA": 5.0})
        assert result == "IRA: 5 | Individual: 10"

    def test_fractional_qty_shows_decimal(self):
        assert usp._format_breakdown({"IRA": 5.5}) == "IRA: 5.5"

    def test_whole_qty_no_trailing_zero(self):
        assert usp._format_breakdown({"IRA": 10.0}) == "IRA: 10"


# ── write_breakdowns() ────────────────────────────────────────────────────────

class TestWriteBreakdowns:
    def test_writes_correct_range_and_value(self):
        mock_svc = MagicMock()
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.write_breakdowns(
                {"HROW": {"IRA": 5.0, "Individual": 10.0}},
                [(2, "HROW")],
            )
        batch_call = mock_svc.spreadsheets.return_value.values.return_value.batchUpdate
        body = batch_call.call_args[1]["body"]
        ranges = [d["range"] for d in body["data"]]
        assert any("G2" in r for r in ranges)
        values = {d["range"]: d["values"][0][0] for d in body["data"] if "G2" in d["range"]}
        assert values[f"'US Portfolio'!G2"] == "IRA: 5 | Individual: 10"

    def test_writes_header_on_g1(self):
        mock_svc = MagicMock()
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.write_breakdowns({"AAPL": {"IRA": 5.0}}, [(2, "AAPL")])
        batch_call = mock_svc.spreadsheets.return_value.values.return_value.batchUpdate
        body = batch_call.call_args[1]["body"]
        header_entry = next(d for d in body["data"] if "G1" in d["range"])
        assert header_entry["values"] == [["By Account"]]

    def test_skips_tickers_not_in_sheet(self):
        mock_svc = MagicMock()
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.write_breakdowns(
                {"HROW": {"IRA": 5.0}, "AAPL": {"IRA": 10.0}},
                [(2, "HROW")],  # AAPL not in sheet
            )
        batch_call = mock_svc.spreadsheets.return_value.values.return_value.batchUpdate
        body = batch_call.call_args[1]["body"]
        ranges = [d["range"] for d in body["data"]]
        assert not any("AAPL" in r for r in ranges)

    def test_no_api_call_when_no_overlap(self):
        mock_svc = MagicMock()
        with patch("sync_us_portfolio._sheets_service", return_value=mock_svc):
            usp.write_breakdowns({"AAPL": {"IRA": 5.0}}, [])
        mock_svc.spreadsheets.assert_not_called()


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
             patch("sync_us_portfolio.write_breakdowns"):
            usp.sync("token")

    def test_emits_diff_for_quantity_increase(self, capsys):
        self._run(
            holdings={"HROW": 15.0},
            sheet_tickers=[(2, "HROW")],
            old_quantities={"HROW": 10.0},
        )
        assert "[US] Diff: HROW +5.0" in capsys.readouterr().out

    def test_emits_diff_for_quantity_decrease(self, capsys):
        self._run(
            holdings={"HROW": 5.0},
            sheet_tickers=[(2, "HROW")],
            old_quantities={"HROW": 10.0},
        )
        assert "[US] Diff: HROW -5.0" in capsys.readouterr().out

    def test_no_diff_when_quantity_unchanged(self, capsys):
        self._run(
            holdings={"AAPL": 10.0},
            sheet_tickers=[(2, "AAPL")],
            old_quantities={"AAPL": 10.0},
        )
        assert "[US] Diff:" not in capsys.readouterr().out

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

    def test_calls_write_breakdowns(self):
        breakdown = {"HROW": {"IRA": 5.0, "Individual": 10.0}}
        with patch("sync_us_portfolio.get_all_holdings", return_value={"HROW": 15.0}), \
             patch("sync_us_portfolio.get_sheet_tickers", return_value=[(2, "HROW")]), \
             patch("sync_us_portfolio.get_sheet_quantities", return_value={"HROW": 15.0}), \
             patch("sync_us_portfolio.update_quantities"), \
             patch("sync_us_portfolio.get_holdings_by_account", return_value=breakdown) as mock_gba, \
             patch("sync_us_portfolio.write_breakdowns") as mock_wb:
            usp.sync("token")
        mock_gba.assert_called_once_with("token")
        mock_wb.assert_called_once_with(breakdown, ANY)
