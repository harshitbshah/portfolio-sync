import subprocess

import pytest
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
# Since 2026-11-13 the sheet stores the per-cycle price (Cycle column J says how
# often), so _annualize no longer scales by cycle/frequency — it only rounds.

class TestAnnualize:
    @pytest.mark.parametrize("cycle,frequency", [
        (1, 1),        # daily
        (2, 1),        # weekly
        (3, 1),        # monthly
        (3, 2),        # every 2 months
        (4, 1),        # yearly
        (3, 0),        # zero frequency — no divide-by-zero
        (None, None),  # unknown cycle
    ])
    def test_price_is_per_cycle_not_annualized(self, cycle, frequency):
        assert sw._annualize(25.0, cycle=cycle, frequency=frequency) == 25.0

    def test_rounds_to_cents(self):
        assert sw._annualize(9.999, cycle=3, frequency=1) == 10.0
        assert sw._annualize(4.994, cycle=3, frequency=1) == 4.99


# ── _sheet_cycle() ───────────────────────────────────────────────────────────

class TestSheetCycle:
    @pytest.mark.parametrize("cycle,frequency,expected", [
        (3, 1, "Monthly"),
        (3, 3, "Quarterly"),
        (3, 12, "Yearly"),     # every 12 months
        (4, 1, "Yearly"),
        (3, None, "Monthly"),  # missing frequency treated as 1
        (3, 0, "Monthly"),     # zero frequency treated as 1
        (4, None, "Yearly"),
    ])
    def test_maps_to_sheet_values(self, cycle, frequency, expected):
        assert sw._sheet_cycle(cycle, frequency) == expected

    @pytest.mark.parametrize("cycle,frequency", [
        (1, 1),        # daily — no sheet value
        (2, 1),        # weekly
        (3, 2),        # every 2 months
        (3, 6),        # semi-annual
        (4, 2),        # every 2 years
        (None, None),  # unknown
        (99, 1),       # unrecognised Wallos id
    ])
    def test_unmappable_cadence_returns_none(self, cycle, frequency):
        assert sw._sheet_cycle(cycle, frequency) is None


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

    def test_single_entry_uses_raw_expiring_date(self):
        desired = sw.build_desired_rows([self._row(next_payment="2026-09-16")])
        assert desired["Some Newsletter"]["expiring"] == "2026-09-16"

    def test_single_entry_keeps_per_cycle_price(self):
        desired = sw.build_desired_rows([self._row(price=25.0, cycle=3, frequency=1)])
        assert desired["Some Newsletter"]["price"] == 25.0

    @pytest.mark.parametrize("cycle,frequency,expected", [
        (3, 1, "Monthly"), (3, 3, "Quarterly"), (4, 1, "Yearly"), (2, 1, None),
    ])
    def test_cycle_derived_from_wallos(self, cycle, frequency, expected):
        desired = sw.build_desired_rows([self._row(cycle=cycle, frequency=frequency)])
        assert desired["Some Newsletter"]["cycle"] == expected

    def test_missing_cycle_fields_give_none(self):
        row = self._row()
        del row["cycle"], row["frequency"]
        desired = sw.build_desired_rows([row])
        assert desired["Some Newsletter"]["cycle"] is None

    def test_duplicate_names_kept_as_separate_rows(self):
        entries = [self._row(name="Google One", price=20.0), self._row(name="Google One", price=20.0)]
        desired = sw.build_desired_rows(entries)
        assert "Google One" in desired
        assert "Google One (2)" in desired
        assert desired["Google One"]["price"] == 20.0
        assert desired["Google One (2)"]["price"] == 20.0

    def test_duplicate_names_keep_own_expiring_date(self):
        entries = [
            self._row(name="Google One", next_payment="2027-05-16"),
            self._row(name="Google One", next_payment="2027-04-22"),
        ]
        desired = sw.build_desired_rows(entries)
        assert desired["Google One"]["expiring"] == "2027-05-16"
        assert desired["Google One (2)"]["expiring"] == "2027-04-22"

    def test_third_duplicate_gets_incrementing_suffix(self):
        entries = [self._row(name="Google One") for _ in range(3)]
        desired = sw.build_desired_rows(entries)
        assert set(desired) == {"Google One", "Google One (2)", "Google One (3)"}

    def test_duplicate_names_use_own_auto_renew_status(self):
        entries = [
            self._row(name="Google One", auto_renew=1),
            self._row(name="Google One", auto_renew=0),
        ]
        desired = sw.build_desired_rows(entries)
        assert desired["Google One"]["status"] == "Subscribed"
        assert desired["Google One (2)"]["status"] == "Expiring"

    def test_inr_price_converted_to_usd(self):
        desired = sw.build_desired_rows(
            [self._row(currency="INR", price=1000.0, cycle=4, frequency=1)],
            usd_inr_rate=100.0,
        )
        assert desired["Some Newsletter"]["price"] == 10.0

    def test_usd_price_unaffected_by_rate(self):
        desired = sw.build_desired_rows(
            [self._row(currency="USD", price=100.0, cycle=4, frequency=1)],
            usd_inr_rate=100.0,
        )
        assert desired["Some Newsletter"]["price"] == 100.0

    def test_inr_monthly_converted_per_cycle(self):
        # Monthly, INR 1000/mo -> $10/mo at rate 100 (no annualizing)
        desired = sw.build_desired_rows(
            [self._row(currency="INR", price=1000.0, cycle=3, frequency=1)],
            usd_inr_rate=100.0,
        )
        assert desired["Some Newsletter"]["price"] == 10.0
        assert desired["Some Newsletter"]["cycle"] == "Monthly"

    def test_inr_conversion_applies_to_each_duplicate_separately(self):
        entries = [
            self._row(name="Google One", currency="INR", price=1000.0, cycle=4),
            self._row(name="Google One", currency="INR", price=2000.0, cycle=4),
        ]
        desired = sw.build_desired_rows(entries, usd_inr_rate=100.0)
        assert desired["Google One"]["price"] == 10.0
        assert desired["Google One (2)"]["price"] == 20.0


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

    def test_reads_cycle_from_column_j(self):
        rows = [["United States", "Investing", "Substack", "Some Newsletter", "Subscribed", "8/8/2026", 100, "Monthly"]]
        with patch("sync_wallos_subscriptions._sheets_service", return_value=self._mock_svc(rows)):
            result = sw.get_sheet_subscriptions()
        assert result["Some Newsletter"]["cycle"] == "Monthly"

    def test_short_row_has_empty_cycle(self):
        rows = [["United States", "Investing", "Substack", "Some Newsletter", "Subscribed", "8/8/2026", 100]]
        with patch("sync_wallos_subscriptions._sheets_service", return_value=self._mock_svc(rows)):
            result = sw.get_sheet_subscriptions()
        assert result["Some Newsletter"]["cycle"] == ""

    def test_reads_through_column_j(self):
        svc = self._mock_svc([])
        with patch("sync_wallos_subscriptions._sheets_service", return_value=svc):
            sw.get_sheet_subscriptions()
        rng = svc.spreadsheets.return_value.values.return_value.get.call_args[1]["range"]
        assert rng.endswith(":J")

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


# ── delete_unmatched() ──────────────────────────────────────────────────────────

class TestDeleteUnmatched:
    def _svc_with_grid_id(self, grid_id=1986684834):
        svc = MagicMock()
        svc.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": sw.SUBSCRIPTIONS_TAB, "sheetId": grid_id}}]
        }
        return svc

    def test_deletes_whole_row_with_delete_dimension(self):
        # Sheets Tables reject partial-row deleteRange (sync failures 2026-11-13/14),
        # so the whole row goes — which also keeps columns J/L aligned.
        svc = self._svc_with_grid_id()
        sheet_subs = {"Stale Sub": {"row": 10}}
        sw.delete_unmatched(svc, {"Stale Sub"}, sheet_subs)

        request = svc.spreadsheets.return_value.batchUpdate.call_args[1]["body"]["requests"][0]
        assert set(request) == {"deleteDimension"}
        rng = request["deleteDimension"]["range"]
        assert rng == {"sheetId": 1986684834, "dimension": "ROWS", "startIndex": 9, "endIndex": 10}

    def test_all_deletes_sent_in_one_batch(self):
        svc = self._svc_with_grid_id()
        sw.delete_unmatched(svc, {"A", "B"}, {"A": {"row": 5}, "B": {"row": 6}})
        assert svc.spreadsheets.return_value.batchUpdate.call_count == 1

    def test_multiple_rows_deleted_highest_row_first(self):
        svc = self._svc_with_grid_id()
        sheet_subs = {"A": {"row": 5}, "B": {"row": 20}, "C": {"row": 12}}
        sw.delete_unmatched(svc, {"A", "B", "C"}, sheet_subs)

        requests = svc.spreadsheets.return_value.batchUpdate.call_args[1]["body"]["requests"]
        rows_deleted = [r["deleteDimension"]["range"]["startIndex"] + 1 for r in requests]
        assert rows_deleted == [20, 12, 5]

    def test_no_op_when_nothing_to_delete(self):
        svc = MagicMock()
        sw.delete_unmatched(svc, set(), {})
        svc.spreadsheets.return_value.batchUpdate.assert_not_called()


# ── sort_by_expiring() ─────────────────────────────────────────────────────────

class TestUpdateCycles:
    def _run(self, desired, sheet_subs):
        svc = MagicMock()
        with patch("sync_wallos_subscriptions.get_sheet_subscriptions", return_value=sheet_subs):
            unmapped = sw.update_cycles(svc, desired)
        return svc, unmapped

    def _written(self, svc):
        batch = svc.spreadsheets.return_value.values.return_value.batchUpdate
        if not batch.called:
            return None
        return {d["range"]: d["values"][0][0] for d in batch.call_args[1]["body"]["data"]}

    def test_writes_changed_cycle_to_column_j(self):
        svc, unmapped = self._run({"A": {"cycle": "Monthly"}}, {"A": {"row": 7, "cycle": "Yearly"}})
        assert self._written(svc) == {f"'{sw.SUBSCRIPTIONS_TAB}'!J7": "Monthly"}
        assert unmapped == []

    def test_fills_empty_cycle(self):
        svc, _ = self._run({"A": {"cycle": "Quarterly"}}, {"A": {"row": 9, "cycle": ""}})
        assert self._written(svc) == {f"'{sw.SUBSCRIPTIONS_TAB}'!J9": "Quarterly"}

    def test_unchanged_cycle_makes_no_api_call(self):
        svc, _ = self._run({"A": {"cycle": "Monthly"}}, {"A": {"row": 7, "cycle": "Monthly"}})
        assert self._written(svc) is None

    def test_unmappable_cadence_reported_and_left_untouched(self):
        svc, unmapped = self._run(
            {"Weekly Thing": {"cycle": None}, "A": {"cycle": "Monthly"}},
            {"Weekly Thing": {"row": 5, "cycle": "Monthly"}, "A": {"row": 6, "cycle": "Yearly"}},
        )
        assert unmapped == ["Weekly Thing"]
        assert self._written(svc) == {f"'{sw.SUBSCRIPTIONS_TAB}'!J6": "Monthly"}  # J5 not touched

    def test_row_missing_from_sheet_is_skipped(self):
        svc, unmapped = self._run({"New": {"cycle": "Monthly"}}, {})
        assert self._written(svc) is None and unmapped == []

    def test_uses_fresh_row_positions(self):
        # Rows may have moved during sort; J must follow the post-sort row number.
        svc, _ = self._run({"A": {"cycle": "Yearly"}, "B": {"cycle": "Monthly"}},
                           {"A": {"row": 12, "cycle": ""}, "B": {"row": 4, "cycle": ""}})
        assert self._written(svc) == {
            f"'{sw.SUBSCRIPTIONS_TAB}'!J12": "Yearly",
            f"'{sw.SUBSCRIPTIONS_TAB}'!J4": "Monthly",
        }

    def test_batched_into_one_call_user_entered(self):
        svc, _ = self._run({"A": {"cycle": "Monthly"}, "B": {"cycle": "Yearly"}},
                           {"A": {"row": 3, "cycle": ""}, "B": {"row": 4, "cycle": ""}})
        batch = svc.spreadsheets.return_value.values.return_value.batchUpdate
        assert batch.call_count == 1
        assert batch.call_args[1]["body"]["valueInputOption"] == "USER_ENTERED"


class TestSortByExpiring:
    def _mock_svc(self, grid_id=1986684834):
        svc = MagicMock()
        svc.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"title": sw.SUBSCRIPTIONS_TAB, "sheetId": grid_id}}]
        }
        return svc

    def test_sorts_by_column_h_ascending_scoped_to_c_through_i(self):
        svc = self._mock_svc()
        sw.sort_by_expiring(svc, last_row=10)

        request = svc.spreadsheets.return_value.batchUpdate.call_args[1]["body"]["requests"][0]
        sort_range = request["sortRange"]
        assert sort_range["range"]["startColumnIndex"] == 2  # C
        assert sort_range["range"]["endColumnIndex"] == 9    # exclusive, through I
        assert sort_range["sortSpecs"] == [{"dimensionIndex": 7, "sortOrder": "ASCENDING"}]

    def test_row_range_covers_first_data_row_through_last_row(self):
        svc = self._mock_svc()
        sw.sort_by_expiring(svc, last_row=10)

        request = svc.spreadsheets.return_value.batchUpdate.call_args[1]["body"]["requests"][0]
        sheet_range = request["sortRange"]["range"]
        assert sheet_range["startRowIndex"] == sw._FIRST_DATA_ROW - 1
        assert sheet_range["endRowIndex"] == 10

    def test_no_op_on_empty_sheet(self):
        svc = self._mock_svc()
        sw.sort_by_expiring(svc, last_row=sw._FIRST_DATA_ROW - 1)
        svc.spreadsheets.return_value.batchUpdate.assert_not_called()


# ── get_usd_inr_rate() ──────────────────────────────────────────────────────────

class TestGetUsdInrRate:
    def test_returns_inr_rate_from_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"amount": 1.0, "base": "USD", "rates": {"INR": 87.5}}
        with patch("sync_wallos_subscriptions.requests.get", return_value=mock_response) as mock_get:
            rate = sw.get_usd_inr_rate()
        assert rate == 87.5
        mock_get.assert_called_once()

    def test_raises_on_http_error(self):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("boom")
        with patch("sync_wallos_subscriptions.requests.get", return_value=mock_response):
            try:
                sw.get_usd_inr_rate()
                assert False, "expected an exception"
            except Exception as e:
                assert str(e) == "boom"


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
