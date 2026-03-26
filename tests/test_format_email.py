import format_email as fe

# ── Sample log fixtures ───────────────────────────────────────────────────────

QUIET_LOG = """\
Run: https://github.com/org/repo/actions/runs/123
─────────────────────────────────────────

PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 70.19% | Total $786,962.00
Done. Updated 30, removed 0, added 0.
"""

CHANGES_LOG = """\
Run: https://github.com/org/repo/actions/runs/456

PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 70.19% | Total $786,962.00
[Indian] Diff: FEDFINA +500
[Indian] Diff: TDPOWERSYS -200
[Indian] Closed: WINDLAS
[Indian] Added: GPIL +5804
  NVDA  : 92.3431 shares (Theme/Conviction: fill manually)
[US] Added: NVDA +92.343100
Removing 1 closed positions: ['ZS']
[US] Closed: ZS
"""

WARNING_LOG = """\
PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 70.19% | Total $786,962.00
WARNING: could not match Monarch accounts: ['9999']
Done. Updated 30, removed 0, added 0.
"""

ERROR_LOG = """\
PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 70.19% | Total $786,962.00
ERROR: Sheet update failed
"""

MULTI_REMOVED_LOG = """\
Removing 2 closed positions: ['AAA', 'BBB']
[US] Closed: AAA
[US] Closed: BBB
Done. Updated 0, removed 2, added 0.
"""


# ── parse() ───────────────────────────────────────────────────────────────────

class TestParse:
    def test_parses_run_url(self):
        data = fe.parse(QUIET_LOG)
        assert data["run_url"] == "https://github.com/org/repo/actions/runs/123"

    def test_parses_pf_summary(self):
        data = fe.parse(QUIET_LOG)
        assert data["indian_pf"] == "$234,629.00"
        assert data["indian_pct"] == "29.81%"
        assert data["us_pf"] == "$552,332.00"
        assert data["us_pct"] == "70.19%"
        assert data["total"] == "$786,962.00"

    def test_parses_indian_diffs(self):
        data = fe.parse(CHANGES_LOG)
        tickers = [t for t, _ in data["indian_diffs"]]
        assert "FEDFINA" in tickers
        assert "TDPOWERSYS" in tickers

    def test_indian_diff_sign(self):
        data = fe.parse(CHANGES_LOG)
        diff_map = {t: v for t, v in data["indian_diffs"]}
        assert diff_map["FEDFINA"] == "+500"
        assert diff_map["TDPOWERSYS"] == "−200"

    def test_parses_indian_closed(self):
        data = fe.parse(CHANGES_LOG)
        assert "WINDLAS" in data["indian_closed"]

    def test_parses_indian_new(self):
        data = fe.parse(CHANGES_LOG)
        tickers = [t for t, _ in data["indian_new"]]
        assert "GPIL" in tickers

    def test_parses_us_new(self):
        data = fe.parse(CHANGES_LOG)
        tickers = [t for t, _ in data["us_new"]]
        assert "NVDA" in tickers

    def test_parses_us_new_quantity(self):
        data = fe.parse(CHANGES_LOG)
        qty_map = {t: q for t, q in data["us_new"]}
        assert qty_map["NVDA"] == "92.343100"

    def test_parses_single_us_closed(self):
        data = fe.parse(CHANGES_LOG)
        assert data["us_closed"] == ["ZS"]

    def test_parses_multiple_us_closed(self):
        data = fe.parse(MULTI_REMOVED_LOG)
        assert set(data["us_closed"]) == {"AAA", "BBB"}

    def test_indian_removal_line_does_not_pollute_us_closed(self):
        # sync_indian_portfolio.py prints "Removing N closed positions" before [Indian] Closed:
        # Make sure that line does NOT end up in us_closed
        log = "Removing 1 closed positions: ['WINDLAS']\n[Indian] Closed: WINDLAS\n"
        data = fe.parse(log)
        assert data["us_closed"] == []
        assert data["indian_closed"] == ["WINDLAS"]

    def test_us_added_parsed_from_structured_line(self):
        log = "[US] Added: RKLB +460.870000\n"
        data = fe.parse(log)
        assert data["us_new"] == [("RKLB", "460.870000")]

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
        assert data["indian_pf"] is None
        assert data["us_pf"] is None
        assert data["total"] is None
        assert data["indian_diffs"] == []
        assert data["indian_closed"] == []
        assert data["indian_new"] == []
        assert data["us_closed"] == []
        assert data["us_new"] == []
        assert data["warnings"] == []


# ── build_subject() ───────────────────────────────────────────────────────────

class TestBuildSubject:
    def _d(self, **kw):
        base = {
            "total": "$786,962.00",
            "indian_diffs": [],
            "indian_closed": [],
            "indian_new": [],
            "us_closed": [],
            "us_new": [],
            "warnings": [],
        }
        base.update(kw)
        return base

    def test_includes_total(self):
        subj = fe.build_subject(self._d())
        assert "$786,962.00" in subj

    def test_never_has_no_changes_suffix(self):
        assert "no changes" not in fe.build_subject(self._d())
        assert "no changes" not in fe.build_subject(self._d(indian_diffs=[("FEDFINA", "+500")]))

    def test_warning_emoji_when_warnings_present(self):
        subj = fe.build_subject(self._d(warnings=["WARNING: something"]))
        assert subj.startswith("⚠️")

    def test_check_emoji_when_no_warnings(self):
        assert fe.build_subject(self._d()).startswith("✅")

    def test_none_total_not_rendered_as_none_string(self):
        subj = fe.build_subject(self._d(total=None))
        assert "None" not in subj


# ── build_html() ──────────────────────────────────────────────────────────────

class TestBuildHtml:
    def _quiet(self, **kw):
        base = {
            "run_url": "https://github.com/runs/1",
            "indian_pf": "$234,629.00",
            "indian_pct": "29.81%",
            "us_pf": "$552,332.00",
            "us_pct": "70.19%",
            "total": "$786,962.00",
            "indian_diffs": [],
            "indian_closed": [],
            "indian_new": [],
            "us_closed": [],
            "us_new": [],
            "warnings": [],
        }
        base.update(kw)
        return base

    def test_includes_portfolio_totals(self):
        html = fe.build_html(self._quiet())
        assert "$234,629.00" in html
        assert "$552,332.00" in html
        assert "$786,962.00" in html

    def test_includes_percentages(self):
        html = fe.build_html(self._quiet())
        assert "29.81%" in html
        assert "70.19%" in html

    def test_includes_run_url(self):
        html = fe.build_html(self._quiet())
        assert "https://github.com/runs/1" in html

    def test_no_run_url_omits_footer(self):
        html = fe.build_html(self._quiet(run_url=None))
        assert "view run" not in html

    def test_warning_banner_shown(self):
        html = fe.build_html(self._quiet(warnings=["WARNING: something went wrong"]))
        assert "something went wrong" in html

    def test_indian_diff_shown(self):
        html = fe.build_html(self._quiet(indian_diffs=[("FEDFINA", "+500")]))
        assert "FEDFINA" in html
        assert "+500" in html

    def test_indian_closed_shown(self):
        html = fe.build_html(self._quiet(indian_closed=["WINDLAS"]))
        assert "WINDLAS" in html
        assert "exited" in html

    def test_indian_new_shown(self):
        html = fe.build_html(self._quiet(indian_new=[("GPIL", "5804")]))
        assert "GPIL" in html
        assert "new" in html
        assert "5804" in html

    def test_us_closed_shown(self):
        html = fe.build_html(self._quiet(us_closed=["ZS"]))
        assert "ZS" in html
        assert "exited" in html

    def test_us_new_shown(self):
        html = fe.build_html(self._quiet(us_new=[("RKLB", "460.87")]))
        assert "RKLB" in html
        assert "460.87" in html

    def test_html_starts_with_doctype(self):
        html = fe.build_html(self._quiet())
        assert html.startswith("<!DOCTYPE html>")
