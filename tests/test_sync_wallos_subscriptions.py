import subprocess
from unittest.mock import MagicMock, patch

import sync_wallos_subscriptions as sw


# ── _parse_platform() ───────────────────────────────────────────────────────

class TestParsePlatform:
    def test_extracts_platform_from_notes(self):
        assert sw._parse_platform("Platform: Substack") == "Substack"

    def test_defaults_to_independent_when_no_notes(self):
        assert sw._parse_platform(None) == "Independent"
        assert sw._parse_platform("") == "Independent"

    def test_defaults_to_independent_when_no_platform_tag(self):
        assert sw._parse_platform("just a random note") == "Independent"

    def test_strips_whitespace(self):
        assert sw._parse_platform("Platform:   X  ") == "X"


# ── _annualize() ─────────────────────────────────────────────────────────────

class TestAnnualize:
    def test_yearly_cycle_unchanged(self):
        assert sw._annualize(100.0, cycle=4, frequency=1) == 100.0

    def test_monthly_cycle_multiplied_by_twelve(self):
        assert sw._annualize(25.0, cycle=3, frequency=1) == 300.0

    def test_weekly_cycle_multiplied_by_fifty_two(self):
        assert sw._annualize(10.0, cycle=2, frequency=1) == 520.0

    def test_daily_cycle_multiplied_by_365(self):
        assert sw._annualize(1.0, cycle=1, frequency=1) == 365.0

    def test_frequency_divides_periods(self):
        # Billed every 2 months → 6 times/year, not 12.
        assert sw._annualize(25.0, cycle=3, frequency=2) == 150.0

    def test_unknown_cycle_defaults_to_unchanged(self):
        assert sw._annualize(100.0, cycle=None, frequency=None) == 100.0

    def test_zero_frequency_does_not_divide_by_zero(self):
        assert sw._annualize(25.0, cycle=3, frequency=0) == 300.0


# ── _status_from_auto_renew() ─────────────────────────────────────────────────

class TestStatusFromAutoRenew:
    def test_all_renewing_is_subscribed(self):
        assert sw._status_from_auto_renew([1]) == "Subscribed"
        assert sw._status_from_auto_renew([1, 1]) == "Subscribed"

    def test_any_not_renewing_is_expiring(self):
        assert sw._status_from_auto_renew([0]) == "Expiring"
        assert sw._status_from_auto_renew([1, 0]) == "Expiring"


# ── build_desired_rows() ─────────────────────────────────────────────────────

class TestBuildDesiredRows:
    def _row(self, **overrides):
        row = {
            "name": "Some Newsletter",
            "price": 100.0,
            "currency": "USD",
            "next_payment": "2026-08-08",
            "cycle": 4,
            "frequency": 1,
            "auto_renew": 1,
            "notes": "Platform: Substack",
            "category": "Investing",
        }
        row.update(overrides)
        return row

    def test_maps_currency_to_country(self):
        desired = sw.build_desired_rows([self._row(currency="INR")])
        assert desired["Some Newsletter"]["country"] == "India"
        desired = sw.build_desired_rows([self._row(currency="USD")])
        assert desired["Some Newsletter"]["country"] == "United States"

    def test_maps_category_via_category_map(self):
        desired = sw.build_desired_rows([self._row(category="Entertainment", name="Netflix")])
        assert desired["Netflix"]["category"] == "Streaming"

    def test_unknown_category_defaults_to_miscellaneous(self):
        desired = sw.build_desired_rows([self._row(category="Some New Category")])
        assert desired["Some Newsletter"]["category"] == "Miscellaneous"

    def test_name_override_beats_category_map(self):
        # Monarch Money is Banking in Wallos, but the sheet uses Personal for it.
        desired = sw.build_desired_rows(
            [self._row(name="Monarch Money", category="Banking")]
        )
        assert desired["Monarch Money"]["category"] == "Personal"

    def test_extracts_platform_from_notes(self):
        desired = sw.build_desired_rows([self._row(notes="Platform: X")])
        assert desired["Some Newsletter"]["platform"] == "X"

    def test_auto_renew_true_maps_to_subscribed(self):
        desired = sw.build_desired_rows([self._row(auto_renew=1)])
        assert desired["Some Newsletter"]["status"] == "Subscribed"

    def test_auto_renew_false_maps_to_expiring(self):
        desired = sw.build_desired_rows([self._row(auto_renew=0)])
        assert desired["Some Newsletter"]["status"] == "Expiring"

    def test_duplicate_names_expiring_if_any_not_renewing(self):
        entries = [
            self._row(name="Google One", auto_renew=1),
            self._row(name="Google One", auto_renew=0),
        ]
        desired = sw.build_desired_rows(entries)
        assert desired["Google One x2"]["status"] == "Expiring"

    def test_single_entry_uses_raw_expiring_date(self):
        desired = sw.build_desired_rows([self._row(next_payment="2026-09-16")])
        assert desired["Some Newsletter"]["expiring"] == "2026-09-16"

    def test_single_entry_annualizes_price(self):
        desired = sw.build_desired_rows([self._row(price=25.0, cycle=3, frequency=1)])
        assert desired["Some Newsletter"]["price"] == 300.0

    def test_duplicate_names_combined_with_xn_suffix(self):
        entries = [self._row(name="Google One", price=20.0), self._row(name="Google One", price=20.0)]
        desired = sw.build_desired_rows(entries)
        assert "Google One x2" in desired
        assert "Google One" not in desired

    def test_duplicate_names_sum_annualized_price(self):
        entries = [
            self._row(name="Google One", price=20.0, cycle=4),
            self._row(name="Google One", price=20.0, cycle=4),
        ]
        desired = sw.build_desired_rows(entries)
        assert desired["Google One x2"]["price"] == 40.0

    def test_duplicate_names_have_no_expiring_date(self):
        entries = [self._row(name="Google One"), self._row(name="Google One")]
        desired = sw.build_desired_rows(entries)
        assert desired["Google One x2"]["expiring"] is None


# ── get_sheet_subscriptions() ────────────────────────────────────────────────

class TestGetSheetSubscriptions:
    def _mock_svc(self, rows):
        svc = MagicMock()
        (svc.spreadsheets.return_value
            .values.return_value
            .get.return_value
            .execute.return_value) = {"values": rows}
        return svc

    def test_parses_rows_into_dict_keyed_by_service(self):
        rows = [["United States", "Investing", "Substack", "Some Newsletter", "Subscribed", "8/8/2026", 100]]
        with patch("sync_wallos_subscriptions._sheets_service", return_value=self._mock_svc(rows)):
            result = sw.get_sheet_subscriptions()
        assert result["Some Newsletter"]["row"] == sw._FIRST_DATA_ROW
        assert result["Some Newsletter"]["country"] == "United States"
        assert result["Some Newsletter"]["price"] == 100

    def test_skips_rows_with_no_service_name(self):
        rows = [["United States", "Investing", "Substack"]]  # short row, no Service cell
        with patch("sync_wallos_subscriptions._sheets_service", return_value=self._mock_svc(rows)):
            result = sw.get_sheet_subscriptions()
        assert result == {}

    def test_row_numbers_offset_from_first_data_row(self):
        rows = [["", "", "", "First"], ["", "", "", "Second"]]
        with patch("sync_wallos_subscriptions._sheets_service", return_value=self._mock_svc(rows)):
            result = sw.get_sheet_subscriptions()
        assert result["First"]["row"] == sw._FIRST_DATA_ROW
        assert result["Second"]["row"] == sw._FIRST_DATA_ROW + 1


# ── update_matched() ──────────────────────────────────────────────────────────

class TestUpdateMatched:
    def test_writes_country_category_platform_price(self):
        svc = MagicMock()
        desired = {"Netflix": {"country": "United States", "category": "Streaming",
                                "platform": "Netflix", "status": "Subscribed",
                                "price": 100.0, "expiring": None}}
        sheet_subs = {"Netflix": {"row": 10}}
        sw.update_matched(svc, {"Netflix"}, desired, sheet_subs)

        call_kwargs = svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args[1]
        ranges_written = {d["range"] for d in call_kwargs["body"]["data"]}
        assert "'Subscriptions'!C10" in ranges_written
        assert "'Subscriptions'!D10" in ranges_written
        assert "'Subscriptions'!E10" in ranges_written
        assert "'Subscriptions'!G10" in ranges_written
        assert "'Subscriptions'!I10" in ranges_written

    def test_writes_status_from_auto_renew(self):
        svc = MagicMock()
        desired = {"Netflix": {"country": "United States", "category": "Streaming",
                                "platform": "Netflix", "status": "Expiring",
                                "price": 100.0, "expiring": None}}
        sheet_subs = {"Netflix": {"row": 10}}
        sw.update_matched(svc, {"Netflix"}, desired, sheet_subs)

        call_kwargs = svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args[1]
        by_range = {d["range"]: d["values"][0][0] for d in call_kwargs["body"]["data"]}
        assert by_range["'Subscriptions'!G10"] == "Expiring"

    def test_skips_expiring_on_when_no_date(self):
        svc = MagicMock()
        desired = {"Netflix": {"country": "United States", "category": "Streaming",
                                "platform": "Netflix", "status": "Subscribed",
                                "price": 100.0, "expiring": None}}
        sheet_subs = {"Netflix": {"row": 10}}
        sw.update_matched(svc, {"Netflix"}, desired, sheet_subs)

        call_kwargs = svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args[1]
        ranges_written = {d["range"] for d in call_kwargs["body"]["data"]}
        assert "'Subscriptions'!H10" not in ranges_written

    def test_writes_formatted_expiring_date_when_present(self):
        svc = MagicMock()
        desired = {"Netflix": {"country": "United States", "category": "Streaming",
                                "platform": "Netflix", "status": "Subscribed",
                                "price": 100.0, "expiring": "2026-08-07"}}
        sheet_subs = {"Netflix": {"row": 10}}
        sw.update_matched(svc, {"Netflix"}, desired, sheet_subs)

        call_kwargs = svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args[1]
        by_range = {d["range"]: d["values"][0][0] for d in call_kwargs["body"]["data"]}
        assert by_range["'Subscriptions'!H10"] == "8/7/2026"

    def test_no_op_when_nothing_to_update(self):
        svc = MagicMock()
        sw.update_matched(svc, set(), {}, {})
        svc.spreadsheets.return_value.values.return_value.batchUpdate.assert_not_called()


# ── insert_new() ──────────────────────────────────────────────────────────────

class TestInsertNew:
    def _svc_with_grid_id(self, grid_id=1986684834):
        svc = MagicMock()
        svc.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": sw.SUBSCRIPTIONS_TAB, "sheetId": grid_id}}]
        }
        return svc

    def test_insert_range_scoped_to_columns_c_through_i(self):
        svc = self._svc_with_grid_id()
        desired = {"New Sub": {"country": "United States", "category": "Investing",
                                "platform": "Independent", "status": "Subscribed",
                                "price": 50.0, "expiring": None}}
        sheet_subs = {"Existing": {"row": 10}}
        sw.insert_new(svc, {"New Sub"}, desired, sheet_subs)

        batch_calls = svc.spreadsheets.return_value.batchUpdate.call_args_list
        insert_request = batch_calls[0][1]["body"]["requests"][0]["insertRange"]
        assert insert_request["range"]["startColumnIndex"] == 2  # C
        assert insert_request["range"]["endColumnIndex"] == 9    # exclusive, through I
        assert insert_request["range"]["startRowIndex"] == 10    # row 11, 0-indexed

    def test_inserts_after_last_existing_row(self):
        svc = self._svc_with_grid_id()
        desired = {"New Sub": {"country": "United States", "category": "Investing",
                                "platform": "Independent", "status": "Subscribed",
                                "price": 50.0, "expiring": None}}
        sheet_subs = {"Existing": {"row": 10}}
        sw.insert_new(svc, {"New Sub"}, desired, sheet_subs)

        values_call = svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args[1]
        data = values_call["body"]["data"]
        assert data[0]["range"] == f"'{sw.SUBSCRIPTIONS_TAB}'!C11:I11"

    def test_writes_status_from_desired_for_new_rows(self):
        svc = self._svc_with_grid_id()
        desired = {"New Sub": {"country": "United States", "category": "Investing",
                                "platform": "Independent", "status": "Expiring",
                                "price": 50.0, "expiring": None}}
        sw.insert_new(svc, {"New Sub"}, desired, {})

        values_call = svc.spreadsheets.return_value.values.return_value.batchUpdate.call_args[1]
        row_values = values_call["body"]["data"][0]["values"][0]
        # Country, Category, Platform, Service, Status, Expiring On, Price
        assert row_values == ["United States", "Investing", "Independent", "New Sub", "Expiring", "", 50.0]

    def test_no_op_when_nothing_to_add(self):
        svc = MagicMock()
        sw.insert_new(svc, set(), {}, {})
        svc.spreadsheets.return_value.batchUpdate.assert_not_called()


# ── _wallos_query() ────────────────────────────────────────────────────────────

class TestWallosQuery:
    def test_local_mode_runs_sqlite_directly(self):
        with patch("sync_wallos_subscriptions.WALLOS_SSH_HOST", None), \
             patch("sync_wallos_subscriptions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout='[{"id": 1}]')
            result = sw._wallos_query("SELECT 1;")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "sudo"
        assert cmd[1] == "sqlite3"
        assert result == [{"id": 1}]

    def test_ssh_mode_wraps_command_over_ssh(self):
        with patch("sync_wallos_subscriptions.WALLOS_SSH_HOST", "ubuntu@1.2.3.4"), \
             patch("sync_wallos_subscriptions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="[]")
            sw._wallos_query("SELECT 1;")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh"
        assert cmd[1] == "ubuntu@1.2.3.4"
        assert "sudo sqlite3" in cmd[2]

    def test_empty_output_returns_empty_list(self):
        with patch("sync_wallos_subscriptions.WALLOS_SSH_HOST", None), \
             patch("sync_wallos_subscriptions.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            assert sw._wallos_query("SELECT 1;") == []
