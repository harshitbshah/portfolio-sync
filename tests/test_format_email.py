import format_email as fe

# ── Sample log fixtures ───────────────────────────────────────────────────────

QUIET_LOG = """\
Run: https://github.com/org/repo/actions/runs/123
─────────────────────────────────────────

Updated: Zerodha → $233,446.16
Done. Updated 30, removed 0, added 0.
"""

CHANGES_LOG = """\
Run: https://github.com/org/repo/actions/runs/456

  NVDA  : 92.3431 shares (Theme/Conviction: fill manually)
  AAPL  : 5.0000 shares (Theme/Conviction: fill manually)
Removing 1 closed positions: ['XYZ']
Updated: Zerodha → $233,446.16
Done. Updated 28, removed 1, added 2.
"""

WARNING_LOG = """\
Updated: Zerodha → $233,446.16
WARNING: could not match Monarch accounts: ['9999']
Done. Updated 30, removed 0, added 0.
"""

ERROR_LOG = """\
Updated: Zerodha → $100,000.00
ERROR: Sheet update failed
Done. Updated 0, removed 0, added 0.
"""

MULTI_REMOVED_LOG = """\
Removing 2 closed positions: ['AAA', 'BBB']
Done. Updated 0, removed 2, added 0.
"""


# ── parse() ───────────────────────────────────────────────────────────────────

class TestParse:
    def test_parses_run_url(self):
        data = fe.parse(QUIET_LOG)
        assert data["run_url"] == "https://github.com/org/repo/actions/runs/123"

    def test_parses_zerodha_balance(self):
        data = fe.parse(QUIET_LOG)
        assert data["zerodha_balance"] == "233,446.16"

    def test_parses_us_updated_count(self):
        data = fe.parse(QUIET_LOG)
        assert data["us_updated"] == 30

    def test_parses_us_added_tickers(self):
        data = fe.parse(CHANGES_LOG)
        tickers = [t for t, _ in data["us_added"]]
        assert "NVDA" in tickers
        assert "AAPL" in tickers

    def test_parses_us_added_quantities(self):
        data = fe.parse(CHANGES_LOG)
        qty_map = {t: q for t, q in data["us_added"]}
        assert qty_map["NVDA"] == "92.3431"
        assert qty_map["AAPL"] == "5.0000"

    def test_parses_single_removed_ticker(self):
        data = fe.parse(CHANGES_LOG)
        assert data["us_removed"] == ["XYZ"]

    def test_parses_multiple_removed_tickers(self):
        data = fe.parse(MULTI_REMOVED_LOG)
        assert set(data["us_removed"]) == {"AAA", "BBB"}

    def test_parses_warning_line(self):
        data = fe.parse(WARNING_LOG)
        assert len(data["warnings"]) == 1
        assert "could not match" in data["warnings"][0]

    def test_parses_error_line(self):
        data = fe.parse(ERROR_LOG)
        assert any("Sheet update failed" in w for w in data["warnings"])

    def test_empty_log_gives_safe_defaults(self):
        data = fe.parse("")
        assert data["run_url"] is None
        assert data["zerodha_balance"] is None
        assert data["us_added"] == []
        assert data["us_removed"] == []
        assert data["us_updated"] == 0
        assert data["warnings"] == []


# ── build_subject() ───────────────────────────────────────────────────────────

class TestBuildSubject:
    def _d(self, **kw):
        base = {
            "zerodha_balance": "233,446.16",
            "us_added": [],
            "us_removed": [],
            "warnings": [],
        }
        base.update(kw)
        return base

    def test_no_changes(self):
        assert fe.build_subject(self._d()) == "✅ Portfolio sync | $233,446.16 | no changes"

    def test_added_ticker_in_subject(self):
        subj = fe.build_subject(self._d(us_added=[("NVDA", "92.34")]))
        assert "+NVDA" in subj

    def test_removed_ticker_in_subject(self):
        subj = fe.build_subject(self._d(us_removed=["XYZ"]))
        assert "−XYZ" in subj

    def test_both_added_and_removed(self):
        subj = fe.build_subject(self._d(
            us_added=[("NVDA", "10.0")],
            us_removed=["XYZ"],
        ))
        assert "+NVDA" in subj
        assert "−XYZ" in subj

    def test_warning_emoji_when_warnings_present(self):
        subj = fe.build_subject(self._d(warnings=["WARNING: something"]))
        assert subj.startswith("⚠️")

    def test_check_emoji_when_no_warnings(self):
        assert fe.build_subject(self._d()).startswith("✅")

    def test_none_balance_not_rendered_as_none_string(self):
        subj = fe.build_subject(self._d(zerodha_balance=None))
        assert "None" not in subj


# ── build_body() ──────────────────────────────────────────────────────────────

class TestBuildBody:
    def _quiet(self, **kw):
        base = {
            "run_url": "https://github.com/runs/1",
            "zerodha_balance": "233,446.16",
            "us_added": [],
            "us_removed": [],
            "us_updated": 30,
            "warnings": [],
        }
        base.update(kw)
        return base

    def test_quiet_day_includes_balance_and_position_count(self):
        body = fe.build_body(self._quiet())
        assert "Zerodha synced: $233,446.16" in body
        assert "30 positions, no changes" in body

    def test_quiet_day_includes_run_url(self):
        body = fe.build_body(self._quiet())
        assert "── view run: https://github.com/runs/1" in body

    def test_changes_show_closed_and_new_sections(self):
        body = fe.build_body(self._quiet(
            us_added=[("NVDA", "92.3431")],
            us_removed=["XYZ"],
            us_updated=28,
        ))
        assert "Closed:" in body and "XYZ" in body
        assert "New:" in body and "NVDA" in body

    def test_warning_appears_before_balance(self):
        body = fe.build_body(self._quiet(warnings=["WARNING: something went wrong"]))
        lines = body.splitlines()
        warn_idx = next(i for i, l in enumerate(lines) if "WARNINGS" in l)
        bal_idx = next(i for i, l in enumerate(lines) if "Zerodha" in l)
        assert warn_idx < bal_idx

    def test_body_ends_with_newline(self):
        assert fe.build_body(self._quiet()).endswith("\n")

    def test_no_run_url_omits_footer(self):
        body = fe.build_body(self._quiet(run_url=None))
        assert "── view run:" not in body

    def test_singular_position_uses_correct_grammar(self):
        body = fe.build_body(self._quiet(us_updated=1))
        assert "1 position, no changes" in body
        assert "1 positions" not in body

    def test_plural_positions(self):
        body = fe.build_body(self._quiet(us_updated=2))
        assert "2 positions, no changes" in body
