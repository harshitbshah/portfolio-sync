import pytest
from unittest.mock import MagicMock, patch

import sync_indian_portfolio as sip


def _mock_kite_resp(data):
    r = MagicMock()
    r.json.return_value = {"status": "success", "data": data}
    return r


def _mock_sheets_svc(rows):
    svc = MagicMock()
    (svc.spreadsheets.return_value
        .values.return_value
        .get.return_value
        .execute.return_value) = {"values": rows}
    return svc


# ── get_kite_holdings ─────────────────────────────────────────────────────────

class TestGetKiteHoldings:
    def test_returns_ticker_qty_dict(self):
        data = [
            {"tradingsymbol": "FEDFINA", "quantity": 13014},
            {"tradingsymbol": "TDPOWERSYS", "quantity": 2921},
        ]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_kite_resp(data)):
            result = sip.get_kite_holdings()
        assert result == {"FEDFINA": 13014, "TDPOWERSYS": 2921}

    def test_filters_zero_quantity_positions(self):
        data = [
            {"tradingsymbol": "FEDFINA", "quantity": 13014},
            {"tradingsymbol": "EXITED", "quantity": 0},
        ]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_kite_resp(data)):
            result = sip.get_kite_holdings()
        assert "EXITED" not in result

    def test_includes_t1_quantity(self):
        data = [{"tradingsymbol": "SENORES", "quantity": 0, "t1_quantity": 1290}]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_kite_resp(data)):
            result = sip.get_kite_holdings()
        assert result == {"SENORES": 1290}

    def test_sums_settled_and_t1(self):
        data = [{"tradingsymbol": "NIFTYBEES", "quantity": 3788, "t1_quantity": 2411}]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_kite_resp(data)):
            result = sip.get_kite_holdings()
        assert result == {"NIFTYBEES": 6199}

    def test_filters_zero_total_quantity(self):
        data = [{"tradingsymbol": "EXITED", "quantity": 0, "t1_quantity": 0}]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_kite_resp(data)):
            result = sip.get_kite_holdings()
        assert result == {}

    def test_raises_on_api_error(self):
        r = MagicMock()
        r.json.return_value = {"status": "error", "message": "Invalid token"}
        with patch("sync_indian_portfolio.requests.get", return_value=r):
            with pytest.raises(RuntimeError, match="Kite holdings fetch failed"):
                sip.get_kite_holdings()


# ── get_kite_positions ────────────────────────────────────────────────────────

def _mock_positions_resp(net):
    r = MagicMock()
    r.json.return_value = {"status": "success", "data": {"net": net, "day": net}}
    return r


class TestGetKitePositions:
    def test_returns_todays_cnc_buys(self):
        net = [{"tradingsymbol": "RATEGAIN", "product": "CNC", "quantity": 2069, "overnight_quantity": 0}]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_positions_resp(net)):
            result = sip.get_kite_positions()
        assert result == {"RATEGAIN": 2069}

    def test_filters_non_cnc_products(self):
        net = [{"tradingsymbol": "NIFTY26APR", "product": "MIS", "quantity": 50, "overnight_quantity": 0}]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_positions_resp(net)):
            result = sip.get_kite_positions()
        assert result == {}

    def test_filters_overnight_positions(self):
        """Positions with overnight_quantity > 0 are already in holdings — skip them."""
        net = [{"tradingsymbol": "FEDFINA", "product": "CNC", "quantity": 500, "overnight_quantity": 500}]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_positions_resp(net)):
            result = sip.get_kite_positions()
        assert result == {}

    def test_filters_zero_or_negative_quantity(self):
        net = [
            {"tradingsymbol": "SOLD", "product": "CNC", "quantity": 0, "overnight_quantity": 0},
            {"tradingsymbol": "SHORTED", "product": "CNC", "quantity": -100, "overnight_quantity": 0},
        ]
        with patch("sync_indian_portfolio.requests.get", return_value=_mock_positions_resp(net)):
            result = sip.get_kite_positions()
        assert result == {}

    def test_uses_net_bucket_not_day(self):
        """Ensures deduplication — only net is read, day is ignored."""
        r = MagicMock()
        r.json.return_value = {
            "status": "success",
            "data": {
                "net": [{"tradingsymbol": "SAMHI", "product": "CNC", "quantity": 8205, "overnight_quantity": 0}],
                "day": [
                    {"tradingsymbol": "SAMHI", "product": "CNC", "quantity": 8205, "overnight_quantity": 0},
                    {"tradingsymbol": "SAMHI", "product": "CNC", "quantity": 8205, "overnight_quantity": 0},
                ],
            },
        }
        with patch("sync_indian_portfolio.requests.get", return_value=r):
            result = sip.get_kite_positions()
        assert result == {"SAMHI": 8205}

    def test_raises_on_api_error(self):
        r = MagicMock()
        r.json.return_value = {"status": "error", "message": "Invalid session"}
        with patch("sync_indian_portfolio.requests.get", return_value=r):
            with pytest.raises(RuntimeError, match="Kite positions fetch failed"):
                sip.get_kite_positions()


# ── get_sheet_holdings ────────────────────────────────────────────────────────

class TestGetSheetHoldings:
    def test_returns_row_ticker_qty_tuples(self):
        rows = [["Ticker", "Quantity"], ["FEDFINA", "13014"], ["TDPOWERSYS", "2921"]]
        with patch("sync_indian_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = sip.get_sheet_holdings()
        assert result == [(2, "FEDFINA", 13014), (3, "TDPOWERSYS", 2921)]

    def test_skips_header_row(self):
        rows = [["Ticker", "Quantity"], ["FEDFINA", "100"]]
        with patch("sync_indian_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = sip.get_sheet_holdings()
        assert len(result) == 1
        assert result[0] == (2, "FEDFINA", 100)

    def test_skips_totals_row(self):
        rows = [["Ticker", "Quantity"], ["FEDFINA", "13014"], ["223184", ""]]
        with patch("sync_indian_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = sip.get_sheet_holdings()
        assert len(result) == 1  # totals row "223184" starts with digit — excluded

    def test_handles_missing_quantity_column(self):
        rows = [["Ticker", "Quantity"], ["FEDFINA"]]  # no qty cell
        with patch("sync_indian_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = sip.get_sheet_holdings()
        assert result == [(2, "FEDFINA", 0)]

    def test_handles_comma_formatted_quantity(self):
        rows = [["Ticker", "Quantity"], ["FEDFINA", "13,014"]]
        with patch("sync_indian_portfolio._sheets_service", return_value=_mock_sheets_svc(rows)):
            result = sip.get_sheet_holdings()
        assert result[0][2] == 13014


# ── sync() ────────────────────────────────────────────────────────────────────

class TestSync:
    def _run(self, kite_holdings, sheet_sequence, kite_positions=None):
        """Run sync() with mocked I/O. sheet_sequence covers initial + re-reads."""
        with patch("sync_indian_portfolio.get_kite_holdings", return_value=kite_holdings), \
             patch("sync_indian_portfolio.get_kite_positions", return_value=kite_positions or {}), \
             patch("sync_indian_portfolio.get_sheet_holdings", side_effect=sheet_sequence), \
             patch("sync_indian_portfolio.get_kite_cash", return_value=0.0), \
             patch("sync_indian_portfolio.update_quantities") as mock_upd, \
             patch("sync_indian_portfolio.delete_closed_rows") as mock_del, \
             patch("sync_indian_portfolio.insert_new_rows") as mock_ins:
            sip.sync()
        return mock_upd, mock_del, mock_ins

    def test_quiet_day_no_mutations(self, capsys):
        holdings = {"FEDFINA": 13014, "TDPOWERSYS": 2921}
        sheet = [(2, "FEDFINA", 13014), (3, "TDPOWERSYS", 2921)]
        mock_upd, mock_del, mock_ins = self._run(holdings, [sheet])

        mock_del.assert_not_called()
        mock_ins.assert_not_called()
        mock_upd.assert_called_once()

        out = capsys.readouterr().out
        assert "[Indian] Positions: 2" in out
        assert "[Indian] Unchanged: 2" in out

    def test_quantity_increase_prints_positive_diff(self, capsys):
        kite = {"FEDFINA": 13514, "TDPOWERSYS": 2921}   # FEDFINA bought 500 more
        sheet = [(2, "FEDFINA", 13014), (3, "TDPOWERSYS", 2921)]
        self._run(kite, [sheet])

        out = capsys.readouterr().out
        assert "[Indian] Diff: FEDFINA +500" in out
        assert "[Indian] Unchanged: 1" in out

    def test_quantity_decrease_prints_negative_diff(self, capsys):
        kite = {"FEDFINA": 12514, "TDPOWERSYS": 2921}   # FEDFINA sold 500
        sheet = [(2, "FEDFINA", 13014), (3, "TDPOWERSYS", 2921)]
        self._run(kite, [sheet])

        out = capsys.readouterr().out
        assert "[Indian] Diff: FEDFINA -500" in out

    def test_closed_position_triggers_delete_and_logs(self, capsys):
        kite = {"FEDFINA": 13014}
        sheet_before = [(2, "FEDFINA", 13014), (3, "TDPOWERSYS", 2921)]
        sheet_after = [(2, "FEDFINA", 13014)]
        mock_upd, mock_del, _ = self._run(kite, [sheet_before, sheet_after])

        mock_del.assert_called_once()
        to_remove = mock_del.call_args[0][0]
        assert "TDPOWERSYS" in to_remove

        out = capsys.readouterr().out
        assert "[Indian] Closed: TDPOWERSYS" in out

    def test_new_position_triggers_insert_and_logs(self, capsys):
        kite = {"FEDFINA": 13014, "NEWCO": 500}
        sheet_before = [(2, "FEDFINA", 13014)]
        sheet_after = [(2, "FEDFINA", 13014), (3, "NEWCO", 500)]
        _, _, mock_ins = self._run(kite, [sheet_before, sheet_after])

        mock_ins.assert_called_once()
        to_add = mock_ins.call_args[0][0]
        assert "NEWCO" in to_add

        out = capsys.readouterr().out
        assert "[Indian] Added: NEWCO +500" in out

    def test_intraday_position_adds_new_ticker(self, capsys):
        """A CNC buy today (not in holdings) should be inserted as a new row."""
        holdings = {"FEDFINA": 13014}
        positions = {"RATEGAIN": 2069}
        sheet_before = [(2, "FEDFINA", 13014)]
        sheet_after = [(2, "FEDFINA", 13014), (3, "RATEGAIN", 2069)]
        _, _, mock_ins = self._run(holdings, [sheet_before, sheet_after], positions)

        mock_ins.assert_called_once()
        to_add = mock_ins.call_args[0][0]
        assert "RATEGAIN" in to_add

        out = capsys.readouterr().out
        assert "[Indian] Added: RATEGAIN +2069" in out

    def test_intraday_position_adds_to_existing_holding(self, capsys):
        """Buying more of a stock already in holdings merges the quantities."""
        holdings = {"FEDFINA": 13014}
        positions = {"FEDFINA": 500}   # bought 500 more today
        sheet = [(2, "FEDFINA", 13014)]
        self._run(holdings, [sheet], positions)

        out = capsys.readouterr().out
        assert "[Indian] Diff: FEDFINA +500" in out

    def test_no_intraday_positions_logs_cleanly(self, capsys):
        holdings = {"FEDFINA": 13014}
        sheet = [(2, "FEDFINA", 13014)]
        self._run(holdings, [sheet], kite_positions={})

        out = capsys.readouterr().out
        assert "No intraday CNC buys today" in out

    def test_delete_rows_processed_in_reverse_order(self):
        """Bottom-up deletion prevents row index shifting."""
        sheet_holdings = [(2, "AAA", 100), (5, "BBB", 200), (8, "CCC", 300)]
        to_remove = {"AAA", "CCC"}
        mock_svc = MagicMock()

        with patch("sync_indian_portfolio.get_sheet_grid_id", return_value=42), \
             patch("sync_indian_portfolio._sheets_service", return_value=mock_svc):
            sip.delete_closed_rows(to_remove, sheet_holdings)

        body = mock_svc.spreadsheets.return_value.batchUpdate.call_args[1]["body"]
        indices = [r["deleteRange"]["range"]["startRowIndex"] for r in body["requests"]]
        assert indices == sorted(indices, reverse=True)
