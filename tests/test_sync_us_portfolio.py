from unittest.mock import MagicMock, patch

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
