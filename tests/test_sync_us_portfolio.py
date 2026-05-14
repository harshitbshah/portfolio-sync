import pytest
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

class TestGetSheetQuantities:
    def _mock_svc(self, rows):
        svc = MagicMock()
        (svc.spreadsheets.return_value
            .values.return_value
            .get.return_value
            .execute.return_value) = {"values": rows}
        return svc

    def test_reads_quantity_for_normal_position(self):
        rows = [["Ticker", "% Portfolio", "Qty"], ["AAPL", 0.05, 82.4781]]
        with patch("sync_us_portfolio._sheets_service", return_value=self._mock_svc(rows)):
            result = usp.get_sheet_quantities()
        assert result["AAPL"] == pytest.approx(82.4781)

    def test_reads_quantity_for_large_position(self):
        # Regression: FORMATTED_VALUE returns "1,021.03" which float() can't parse.
        # UNFORMATTED_VALUE returns the raw number — this test ensures we use it.
        rows = [["Ticker", "% Portfolio", "Qty"], ["DLO", 0.02, 1021.026989]]
        with patch("sync_us_portfolio._sheets_service", return_value=self._mock_svc(rows)):
            result = usp.get_sheet_quantities()
        assert result["DLO"] == pytest.approx(1021.026989)

    def test_returns_zero_for_missing_qty_column(self):
        rows = [["Ticker", "% Portfolio", "Qty"], ["AAPL"]]  # no qty cell
        with patch("sync_us_portfolio._sheets_service", return_value=self._mock_svc(rows)):
            result = usp.get_sheet_quantities()
        assert result["AAPL"] == 0.0

    def test_skips_rows_where_ticker_cell_is_numeric(self):
        # UNFORMATTED_VALUE returns int/float for formula cells; totals rows can
        # have a number in column B — must not crash on .strip()
        rows = [["Ticker", "% Portfolio", "Qty"], [29, 1.0, 5000.0]]
        with patch("sync_us_portfolio._sheets_service", return_value=self._mock_svc(rows)):
            result = usp.get_sheet_quantities()
        assert result == {}

    def test_uses_unformatted_value_render_option(self):
        svc = self._mock_svc([])
        with patch("sync_us_portfolio._sheets_service", return_value=svc):
            usp.get_sheet_quantities()
        get_call = svc.spreadsheets.return_value.values.return_value.get
        kwargs = get_call.call_args[1]
        assert kwargs.get("valueRenderOption") == "UNFORMATTED_VALUE"


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
    def test_keeps_digits_from_mask(self):
        assert usp._shorten_account_name("Robinhood individual (...8902)") == "Robinhood individual (8902)"

    def test_keeps_alphanumeric_mask(self):
        assert usp._shorten_account_name("ROTH IRA (...*****4882)") == "ROTH IRA (*****4882)"

    def test_no_mask_unchanged(self):
        assert usp._shorten_account_name("Fidelity 401k") == "Fidelity 401k"

    def test_empty_string_returns_empty(self):
        assert usp._shorten_account_name("") == ""

    def test_two_same_name_accounts_produce_distinct_keys(self):
        name1 = usp._shorten_account_name("Robinhood individual (...8902)")
        name2 = usp._shorten_account_name("Robinhood individual (...1234)")
        assert name1 != name2


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
        assert result["HROW"]["Robinhood IRA (1111)"] == 5.0
        assert result["HROW"]["Robinhood individual (2222)"] == 10.0

    def test_two_accounts_same_name_kept_separate(self):
        """Two Robinhood Individual accounts must not be merged into one."""
        accounts = self._accounts_resp([
            {"id": "acc1", "displayName": "Robinhood individual (...8902)", "deactivatedAt": None, "type": {"name": "brokerage"}},
            {"id": "acc2", "displayName": "Robinhood individual (...1234)", "deactivatedAt": None, "type": {"name": "brokerage"}},
        ])
        with patch("sync_us_portfolio._monarch_request", side_effect=[
            accounts,
            self._holdings_resp([("AAPL", 10.0)]),
            self._holdings_resp([("AAPL", 5.0)]),
        ]):
            result = usp.get_holdings_by_account("token")
        assert result["AAPL"]["Robinhood individual (8902)"] == 10.0
        assert result["AAPL"]["Robinhood individual (1234)"] == 5.0
        assert len(result["AAPL"]) == 2

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
    def _make_svc(self, rows):
        """Mock service returning given rows from values().get()."""
        svc = MagicMock()
        (svc.spreadsheets.return_value.values.return_value
            .get.return_value.execute.return_value) = {"values": rows}
        return svc

    def test_writes_header_on_first_run(self):
        """Empty tab → write header + bold/freeze."""
        svc = self._make_svc([])
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        update_call = svc.spreadsheets.return_value.values.return_value.update
        update_call.assert_called_once()
        assert update_call.call_args[1]["body"]["values"][0] == ["Ticker", "Account", "Qty", "Amount"]

    def test_skips_header_when_already_present(self):
        """Tab already has 'Ticker' header → do not re-write header."""
        rows = [["Ticker", "Account", "Qty", "Amount"], ["AAPL", "IRA", 5.0]]
        svc = self._make_svc(rows)
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        svc.spreadsheets.return_value.values.return_value.update.assert_not_called()

    def test_formats_header_bold_with_frozen_row_on_first_run(self):
        """First run writes bold+freeze via batchUpdate."""
        svc = self._make_svc([])
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        all_batch_calls = svc.spreadsheets.return_value.batchUpdate.call_args_list
        format_call = next(
            c for c in all_batch_calls
            if "repeatCell" in str(c)
        )
        request_types = [list(r.keys())[0] for r in format_call[1]["body"]["requests"]]
        assert "repeatCell" in request_types
        assert "updateSheetProperties" in request_types

    def test_no_op_when_all_quantities_unchanged(self):
        """No inserts/deletes/qty-updates → sort and formula repair still run, no RAW qty writes."""
        rows = [["Ticker", "Account", "Qty"], ["AAPL", "IRA", 5.0]]
        svc = self._make_svc(rows)
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        # Header not rewritten when already present
        svc.spreadsheets.return_value.values.return_value.update.assert_not_called()
        # No RAW qty write — formula repair (USER_ENTERED) always runs, but no qty update
        raw_calls = [
            c for c in svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args_list
            if c[1].get("body", {}).get("valueInputOption") == "RAW"
        ]
        assert raw_calls == []

    def test_updates_changed_quantity(self):
        """Existing row with different qty → the new qty appears in a batchUpdate call."""
        rows = [["Ticker", "Account", "Qty"], ["AAPL", "IRA", 5.0]]
        svc = self._make_svc(rows)
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({"AAPL": {"IRA": 7.0}})
        batch_update = svc.spreadsheets.return_value.values.return_value.batchUpdate
        batch_update.assert_called()
        all_data = [
            item
            for call in batch_update.call_args_list
            for item in call[1].get("body", {}).get("data", [])
        ]
        assert any(7.0 in r["values"][0] for r in all_data)

    def test_deletes_removed_rows_in_reverse_order(self):
        """Rows to remove are deleted highest row-number first."""
        rows = [
            ["Ticker", "Account", "Qty"],
            ["AAPL", "IRA", 5.0],   # row 2 → 0-indexed startRowIndex=1
            ["MSFT", "IRA", 3.0],   # row 3 → 0-indexed startRowIndex=2
        ]
        svc = self._make_svc(rows)
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({})  # empty breakdown → both rows removed
        all_batch_calls = svc.spreadsheets.return_value.batchUpdate.call_args_list
        delete_call = next(c for c in all_batch_calls if "deleteRange" in str(c))
        indices = [
            r["deleteRange"]["range"]["startRowIndex"]
            for r in delete_call[1]["body"]["requests"]
        ]
        assert indices == sorted(indices, reverse=True)

    def test_inserts_new_rows_with_inherit_when_existing_data(self):
        """New rows added to a tab with existing data use inheritFromBefore=True."""
        rows = [["Ticker", "Account", "Qty"], ["AAPL", "IRA", 5.0]]
        svc = self._make_svc(rows)
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}, "MSFT": {"IRA": 3.0}})
        all_batch_calls = svc.spreadsheets.return_value.batchUpdate.call_args_list
        insert_call = next(c for c in all_batch_calls if "insertDimension" in str(c))
        req = insert_call[1]["body"]["requests"][0]["insertDimension"]
        assert req["inheritFromBefore"] is True

    def test_inserts_new_rows_no_inherit_on_fresh_tab(self):
        """New rows on an empty tab use inheritFromBefore=False (avoid copying bold header)."""
        svc = self._make_svc([])
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        all_batch_calls = svc.spreadsheets.return_value.batchUpdate.call_args_list
        insert_call = next(c for c in all_batch_calls if "insertDimension" in str(c))
        req = insert_call[1]["body"]["requests"][0]["insertDimension"]
        assert req["inheritFromBefore"] is False

    def test_inserts_googlefinance_formula_in_amount_column(self):
        """New rows must include =C{row}*GOOGLEFINANCE(ticker) in column D."""
        svc = self._make_svc([])
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio._get_or_create_tab", return_value=5):
            usp.sync_account_tab({"AAPL": {"IRA": 5.0}})
        batch_update = svc.spreadsheets.return_value.values.return_value.batchUpdate
        batch_update.assert_called_once()
        written = batch_update.call_args[1]["body"]["data"][0]["values"][0]
        assert any("GOOGLEFINANCE" in str(v) and "AAPL" in str(v) for v in written)


# ── sort_portfolio_sheet() ────────────────────────────────────────────────────

class TestSortPortfolioSheet:
    def test_issues_sort_range_request(self):
        svc = MagicMock()
        with patch("sync_us_portfolio._sheets_service", return_value=svc), \
             patch("sync_us_portfolio.get_sheet_grid_id", return_value=7):
            usp.sort_portfolio_sheet([(2, "MSFT"), (3, "AAPL")])
        batch_call = svc.spreadsheets.return_value.batchUpdate
        batch_call.assert_called_once()
        req = batch_call.call_args[1]["body"]["requests"][0]
        assert "sortRange" in req
        sort = req["sortRange"]
        assert sort["sortSpecs"][0]["sortOrder"] == "ASCENDING"
        assert sort["range"]["startRowIndex"] == 1  # skips header

    def test_no_op_on_empty_tickers(self):
        with patch("sync_us_portfolio._sheets_service") as mock_svc:
            usp.sort_portfolio_sheet([])
        mock_svc.assert_not_called()


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
             patch("sync_us_portfolio.sort_portfolio_sheet"), \
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

    def test_sort_called_when_new_tickers_added(self):
        with patch("sync_us_portfolio.get_all_holdings", return_value={"NVDA": 5.0}), \
             patch("sync_us_portfolio.get_sheet_tickers", return_value=[]), \
             patch("sync_us_portfolio.get_sheet_quantities", return_value={}), \
             patch("sync_us_portfolio.insert_new_rows"), \
             patch("sync_us_portfolio.sort_portfolio_sheet") as mock_sort, \
             patch("sync_us_portfolio.update_quantities"), \
             patch("sync_us_portfolio.get_holdings_by_account", return_value={}), \
             patch("sync_us_portfolio.sync_account_tab"):
            usp.sync("token")
        mock_sort.assert_called_once()

    def test_sort_not_called_when_no_new_tickers(self):
        with patch("sync_us_portfolio.get_all_holdings", return_value={"AAPL": 10.0}), \
             patch("sync_us_portfolio.get_sheet_tickers", return_value=[(2, "AAPL")]), \
             patch("sync_us_portfolio.get_sheet_quantities", return_value={"AAPL": 10.0}), \
             patch("sync_us_portfolio.update_quantities"), \
             patch("sync_us_portfolio.sort_portfolio_sheet") as mock_sort, \
             patch("sync_us_portfolio.get_holdings_by_account", return_value={}), \
             patch("sync_us_portfolio.sync_account_tab"):
            usp.sync("token")
        mock_sort.assert_not_called()

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
