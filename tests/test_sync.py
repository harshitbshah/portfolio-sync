"""Tests for pure-logic functions in sync.py.

We test functions that don't require live network calls by either:
  - passing row data directly (get_indian_pf_balance, _resolve_sheet_rows,
    _find_sgov_cell, print_pf_summary)
  - patching get_monarch_accounts (get_monarch_account_id, get_account_balances)
  - patching monarch_request (get_sgov_total)
"""

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Set required env vars before importing sync (module-level reads)
os.environ.setdefault("GSHEET_SHEET_ID", "test-sheet-id")
os.environ.setdefault("GSHEET_SERVICE_ACCOUNT_JSON", json.dumps({"type": "service_account"}))
os.environ.setdefault("MONARCH_TOKEN", "test-token")

import sync


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_rows(*rows):
    """Build a list of row lists, padding short rows to length 3."""
    return [list(r) for r in rows]


# ── get_indian_pf_balance ─────────────────────────────────────────────────────

class TestGetIndianPfBalance:
    def _call(self, rows, label="Indian PF"):
        with patch("sync.LABEL_TO_FIND", label), \
             patch("sync._read_sheet_rows", return_value=rows):
            return sync.get_indian_pf_balance()

    def test_finds_label_and_returns_float(self):
        rows = [["Indian PF", "$234,629.00"]]
        assert self._call(rows) == 234629.0

    def test_strips_currency_symbols_and_commas(self):
        rows = [["Indian PF", "$1,234,567.89"]]
        assert self._call(rows) == pytest.approx(1234567.89)

    def test_label_in_non_first_column(self):
        rows = [["Category", "Indian PF", "$50,000.00"]]
        assert self._call(rows) == 50000.0

    def test_label_with_surrounding_whitespace(self):
        rows = [["  Indian PF  ", "$100.00"]]
        assert self._call(rows) == 100.0

    def test_raises_if_label_not_found(self):
        rows = [["US PF", "$500,000.00"]]
        with pytest.raises(ValueError, match="Could not find row"):
            self._call(rows)

    def test_raises_if_no_value_after_label(self):
        rows = [["Indian PF"]]  # no next cell
        with pytest.raises(ValueError, match="Could not find row"):
            self._call(rows)

    def test_zero_balance(self):
        rows = [["Indian PF", "$0.00"]]
        assert self._call(rows) == 0.0

    def test_uses_custom_label(self):
        rows = [["Zerodha INR", "42000.00"]]
        assert self._call(rows, label="Zerodha INR") == 42000.0


# ── _resolve_sheet_rows ───────────────────────────────────────────────────────

class TestResolveSheetRows:
    """_resolve_sheet_rows returns one row-number (1-indexed) per SHEET_ACCOUNTS entry."""

    def _call(self, rows, accounts):
        with patch("sync.SHEET_ACCOUNTS", accounts):
            return sync._resolve_sheet_rows(rows)

    def test_single_match(self):
        rows = [["Bank", "Chase", "50000"]]
        accounts = [{"mask": "1234", "sheet_category": "Bank", "sheet_institution": "Chase"}]
        assert self._call(rows, accounts) == [1]

    def test_returns_none_for_unmatched(self, capsys):
        rows = [["Bank", "Wells Fargo", "50000"]]
        accounts = [{"mask": "1234", "sheet_category": "Bank", "sheet_institution": "Chase"}]
        result = self._call(rows, accounts)
        assert result == [None]
        assert "WARNING" in capsys.readouterr().err

    def test_duplicate_institution_consumed_in_order(self):
        rows = [
            ["Bank", "Chase", "10000"],   # row 1
            ["Bank", "Chase", "20000"],   # row 2
        ]
        accounts = [
            {"mask": "1111", "sheet_category": "Bank", "sheet_institution": "Chase"},
            {"mask": "2222", "sheet_category": "Bank", "sheet_institution": "Chase"},
        ]
        result = self._call(rows, accounts)
        assert result == [1, 2]  # first entry gets row 1, second gets row 2

    def test_each_match_consumed_once(self):
        """Three entries, only two sheet rows → third gets None."""
        rows = [
            ["CDs", "Marcus", "5000"],
            ["CDs", "Marcus", "6000"],
        ]
        accounts = [
            {"mask": "1111", "sheet_category": "CDs", "sheet_institution": "Marcus"},
            {"mask": "2222", "sheet_category": "CDs", "sheet_institution": "Marcus"},
            {"mask": "3333", "sheet_category": "CDs", "sheet_institution": "Marcus"},
        ]
        result = self._call(rows, accounts)
        assert result == [1, 2, None]

    def test_rows_without_two_columns_ignored(self):
        rows = [
            ["Bank"],           # only one column — ignored as candidate
            ["Bank", "Chase", "50000"],  # row 2
        ]
        accounts = [{"mask": "1234", "sheet_category": "Bank", "sheet_institution": "Chase"}]
        assert self._call(rows, accounts) == [2]

    def test_empty_rows_returns_nones(self, capsys):
        accounts = [{"mask": "1234", "sheet_category": "Bank", "sheet_institution": "Chase"}]
        result = self._call([], accounts)
        assert result == [None]


# ── _find_sgov_cell ───────────────────────────────────────────────────────────

class TestFindSgovCell:
    def _call(self, rows, label="Total:"):
        with patch("sync.SGOV_LABEL", label):
            return sync._find_sgov_cell(rows)

    def test_returns_cell_to_right_of_label(self):
        rows = [["Something", "Total:", "0"]]
        assert self._call(rows) == "C1"

    def test_label_in_first_column(self):
        rows = [["Total:", "99"]]
        assert self._call(rows) == "B1"

    def test_label_in_second_row(self):
        rows = [["Header", "Header2"], ["Total:", "42"]]
        assert self._call(rows) == "B2"

    def test_raises_if_label_not_found(self):
        rows = [["SomeLabel", "value"]]
        with pytest.raises(ValueError, match="Could not find SGOV label"):
            self._call(rows)

    def test_custom_label(self):
        rows = [["SGOV Total", "200"]]
        assert self._call(rows, label="SGOV Total") == "B1"


# ── print_pf_summary ──────────────────────────────────────────────────────────

class TestPrintPfSummary:
    def _call(self, rows, label="PF Breakdown"):
        with patch("sync.PF_BREAKDOWN_LABEL", label), \
             patch("sync._read_sheet_rows", return_value=rows):
            sync.print_pf_summary()

    def test_emits_pf_summary_line(self, capsys):
        rows = [
            ["PF Breakdown", "Amount", "Pct"],
            ["Indian PF", "234629.00", "0.2981"],
            ["US PF", "552332.00", "0.7019"],
            ["", "786961.00"],  # total row (blank label)
        ]
        self._call(rows)
        out = capsys.readouterr().out
        assert out.startswith("PF Summary:")
        assert "Indian PF" in out
        assert "US PF" in out
        assert "Total" in out

    def test_handles_formatted_percentage_string(self, capsys):
        """Google Sheets may return '29.81%' instead of raw 0.2981."""
        rows = [
            ["PF Breakdown", "Amount", "Pct"],
            ["Indian PF", "$234,629.00", "29.81%"],
            ["US PF", "$552,332.00", "70.19%"],
            ["", "$786,961.00"],
        ]
        self._call(rows)
        out = capsys.readouterr().out
        assert "29.81%" in out
        assert "70.19%" in out

    def test_handles_raw_decimal_percentage(self, capsys):
        """Sheets may return 0.2981 (unformatted) — multiply by 100."""
        rows = [
            ["PF Breakdown", "Amount", "Pct"],
            ["Indian PF", "234629.00", "0.2981"],
            ["", "786961.00"],
        ]
        self._call(rows)
        out = capsys.readouterr().out
        assert "29.81%" in out

    def test_warns_if_header_not_found(self, capsys):
        rows = [["Some Other Header", "x"]]
        self._call(rows)
        err = capsys.readouterr().err
        assert "WARNING" in err

    def test_prints_nothing_to_stdout_if_header_missing(self, capsys):
        rows = [["No Header Here"]]
        self._call(rows)
        assert capsys.readouterr().out == ""

    def test_amount_with_currency_symbols_parsed(self, capsys):
        rows = [
            ["PF Breakdown"],
            ["Indian PF", "$234,629.00", "29.81%"],
            ["", "$786,961.00"],
        ]
        self._call(rows)
        out = capsys.readouterr().out
        assert "$234,629.00" in out

    def test_header_found_in_non_first_column(self, capsys):
        rows = [
            ["Misc", "PF Breakdown", "Amount", "Pct"],
            ["Misc", "Indian PF", "100000.00", "50%"],
            ["Misc", "", "200000.00"],
        ]
        self._call(rows)
        out = capsys.readouterr().out
        assert "Indian PF" in out
        assert "Total" in out

    def test_warns_when_us_pf_diverges_from_monarch_estimate(self, capsys):
        rows = [
            ["PF Breakdown", "Amount", "Pct"],
            ["US PF", "2542352.89", "88.18%"],
            ["", "2542352.89"],
        ]
        with patch("sync.PF_BREAKDOWN_LABEL", "PF Breakdown"), \
             patch("sync._read_sheet_rows", return_value=rows):
            sync.print_pf_summary(monarch_us_pf=532685.0)
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "GOOGLEFINANCE" in err

    def test_no_warning_when_us_pf_within_tolerance(self, capsys):
        rows = [
            ["PF Breakdown", "Amount", "Pct"],
            ["US PF", "540000.00", "70%"],
            ["", "540000.00"],
        ]
        with patch("sync.PF_BREAKDOWN_LABEL", "PF Breakdown"), \
             patch("sync._read_sheet_rows", return_value=rows):
            sync.print_pf_summary(monarch_us_pf=532685.0)
        err = capsys.readouterr().err
        assert "GOOGLEFINANCE" not in err

    def test_no_warning_when_monarch_estimate_not_provided(self, capsys):
        rows = [
            ["PF Breakdown", "Amount", "Pct"],
            ["US PF", "2542352.89", "88%"],
            ["", "2542352.89"],
        ]
        self._call(rows)  # no monarch_us_pf passed
        err = capsys.readouterr().err
        assert "GOOGLEFINANCE" not in err


# ── get_monarch_account_id ────────────────────────────────────────────────────

class TestGetMonarchAccountId:
    def _call(self, accounts, name="Zerodha"):
        with patch("sync.MONARCH_ACCOUNT_NAME", name), \
             patch("sync.get_monarch_accounts", return_value=accounts):
            return sync.get_monarch_account_id("tok")

    def test_returns_id_for_matching_account(self):
        accounts = [{"id": "acc1", "displayName": "Zerodha", "displayBalance": 1000}]
        assert self._call(accounts) == "acc1"

    def test_raises_if_account_not_found(self):
        accounts = [{"id": "acc1", "displayName": "Robinhood", "displayBalance": 1000}]
        with pytest.raises(ValueError, match="No Monarch account named"):
            self._call(accounts)

    def test_error_message_lists_found_names(self):
        accounts = [{"id": "acc1", "displayName": "Robinhood"}]
        with pytest.raises(ValueError, match="Robinhood"):
            self._call(accounts)

    def test_empty_account_list_raises(self):
        with pytest.raises(ValueError):
            self._call([])


# ── get_account_balances ──────────────────────────────────────────────────────

class TestGetAccountBalances:
    def _call(self, accounts, sheet_accounts):
        with patch("sync.SHEET_ACCOUNTS", sheet_accounts), \
             patch("sync.get_monarch_accounts", return_value=accounts):
            return sync.get_account_balances("tok")

    def test_matches_by_mask(self):
        accounts = [{"id": "a1", "mask": "1234", "displayName": "Chase", "displayBalance": 5000.0}]
        sheet_accounts = [{"mask": "1234", "sheet_category": "Bank", "sheet_institution": "Chase"}]
        result = self._call(accounts, sheet_accounts)
        assert result == {0: 5000.0}

    def test_matches_by_monarch_name(self):
        accounts = [{"id": "a1", "mask": None, "displayName": "PayPal", "displayBalance": 200.0}]
        sheet_accounts = [{"monarch_name": "PayPal", "sheet_category": "Bank", "sheet_institution": "PayPal"}]
        result = self._call(accounts, sheet_accounts)
        assert result == {0: 200.0}

    def test_unmatched_entry_omitted_from_result(self, capsys):
        accounts = []
        sheet_accounts = [{"mask": "9999", "sheet_category": "Bank", "sheet_institution": "Chase"}]
        result = self._call(accounts, sheet_accounts)
        assert result == {}
        assert "WARNING" in capsys.readouterr().err

    def test_none_balance_treated_as_zero(self):
        accounts = [{"id": "a1", "mask": "1234", "displayBalance": None}]
        sheet_accounts = [{"mask": "1234", "sheet_category": "Bank", "sheet_institution": "Chase"}]
        result = self._call(accounts, sheet_accounts)
        assert result == {0: 0.0}

    def test_multiple_entries_all_matched(self):
        accounts = [
            {"id": "a1", "mask": "1111", "displayBalance": 1000.0},
            {"id": "a2", "mask": "2222", "displayBalance": 2000.0},
        ]
        sheet_accounts = [
            {"mask": "1111", "sheet_category": "Bank", "sheet_institution": "Chase"},
            {"mask": "2222", "sheet_category": "CDs",  "sheet_institution": "Marcus"},
        ]
        result = self._call(accounts, sheet_accounts)
        assert result == {0: 1000.0, 1: 2000.0}


# ── get_sgov_total ────────────────────────────────────────────────────────────

def _make_holdings_response(ticker_qty_pairs):
    """Build a Monarch holdings API response dict."""
    edges = [
        {
            "node": {
                "quantity": qty,
                "holdings": [{"ticker": ticker}],
            }
        }
        for ticker, qty in ticker_qty_pairs
    ]
    return {"data": {"portfolio": {"aggregateHoldings": {"edges": edges}}}}


class TestGetSgovTotal:
    def _call(self, accounts, holdings_by_account):
        """holdings_by_account: {account_id: [(ticker, qty)]}"""
        def mock_request(token, payload):
            body = json.loads(payload)
            if "accountId" not in str(body):
                # accounts query
                return {"data": {"accounts": accounts}}
            account_id = body["variables"]["accountId"]
            return _make_holdings_response(holdings_by_account.get(account_id, []))

        with patch("sync.monarch_request", side_effect=mock_request):
            return sync.get_sgov_total("tok")

    def test_sums_sgov_across_brokerage_accounts(self):
        accounts = [
            {"id": "b1", "type": {"name": "brokerage"}, "deactivatedAt": None},
            {"id": "b2", "type": {"name": "brokerage"}, "deactivatedAt": None},
        ]
        holdings = {
            "b1": [("SGOV", 100.0), ("NVDA", 10.0)],
            "b2": [("SGOV", 50.5)],
        }
        assert self._call(accounts, holdings) == pytest.approx(150.5)

    def test_skips_deactivated_accounts(self):
        accounts = [
            {"id": "b1", "type": {"name": "brokerage"}, "deactivatedAt": None},
            {"id": "b2", "type": {"name": "brokerage"}, "deactivatedAt": "2025-01-01"},
        ]
        holdings = {
            "b1": [("SGOV", 100.0)],
            "b2": [("SGOV", 999.0)],
        }
        assert self._call(accounts, holdings) == pytest.approx(100.0)

    def test_skips_non_brokerage_accounts(self):
        accounts = [
            {"id": "b1", "type": {"name": "brokerage"}, "deactivatedAt": None},
            {"id": "b2", "type": {"name": "bank"},      "deactivatedAt": None},
        ]
        holdings = {
            "b1": [("SGOV", 50.0)],
            "b2": [("SGOV", 999.0)],
        }
        assert self._call(accounts, holdings) == pytest.approx(50.0)

    def test_returns_zero_if_no_sgov(self):
        accounts = [{"id": "b1", "type": {"name": "brokerage"}, "deactivatedAt": None}]
        holdings = {"b1": [("NVDA", 10.0), ("AAPL", 5.0)]}
        assert self._call(accounts, holdings) == 0.0

    def test_returns_zero_if_no_brokerage_accounts(self):
        accounts = [{"id": "b1", "type": {"name": "bank"}, "deactivatedAt": None}]
        assert self._call(accounts, {}) == 0.0

    def test_result_rounded_to_6_decimal_places(self):
        accounts = [{"id": "b1", "type": {"name": "brokerage"}, "deactivatedAt": None}]
        holdings = {"b1": [("SGOV", 100.123456789)]}
        result = self._call(accounts, holdings)
        assert result == round(100.123456789, 6)
