import pytest
import format_email as fe

# ── Sample log fixtures ───────────────────────────────────────────────────────

QUIET_LOG = """\
Run: https://github.com/org/repo/actions/runs/123
─────────────────────────────────────────

PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 55.55% | Cash $112,907.25 14.64% | Total $786,962.00
Done. Updated 30, removed 0, added 0.
"""

CHANGES_LOG = """\
Run: https://github.com/org/repo/actions/runs/456

PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 55.55% | Cash $112,907.25 14.64% | Total $786,962.00
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
PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 55.55% | Cash $112,907.25 14.64% | Total $786,962.00
WARNING: could not match Monarch accounts: ['9999']
Done. Updated 30, removed 0, added 0.
"""

ERROR_LOG = """\
PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 55.55% | Cash $112,907.25 14.64% | Total $786,962.00
ERROR: Sheet update failed
"""

MULTI_REMOVED_LOG = """\
Removing 2 closed positions: ['AAA', 'BBB']
[US] Closed: AAA
[US] Closed: BBB
Done. Updated 0, removed 2, added 0.
"""

SGOV_LOG = """\
[SGOV] Robinhood individual (...8902): $5710.85
[SGOV] Robinhood individual (...8195): $34709.35
[SGOV] ROTH IRA (...*****4882): $37860.19
"""

EF_LOG = """\
[EF] Bank|Chase: $11995.54
[EF] Bank|Chase: $1500.00
[EF] Bank|ICICI: $918.54
[EF] PPF|ICICI: $2269.00
[EF] CDs|Marcus: $11574.97
[EF] CDs|Marcus: $11558.50
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
        assert data["us_pct"] == "55.55%"
        assert data["cash"] == "$112,907.25"
        assert data["cash_pct"] == "14.64%"
        assert data["total"] == "$786,962.00"

    def test_parses_cash_when_present(self):
        log = "PF Summary: Indian PF $100.00 50% | Cash $50.00 25% | Total $200.00\n"
        data = fe.parse(log)
        assert data["cash"] == "$50.00"
        assert data["cash_pct"] == "25%"

    def test_cash_none_when_absent(self):
        log = "PF Summary: Indian PF $100.00 50% | US PF $100.00 50% | Total $200.00\n"
        data = fe.parse(log)
        assert data["cash"] is None
        assert data["cash_pct"] is None

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

    def test_parses_us_diffs(self):
        log = "[US] Diff: HROW +5.000000\n[US] Diff: TSLA -10.5\n"
        data = fe.parse(log)
        tickers = [t for t, _ in data["us_diffs"]]
        assert "HROW" in tickers
        assert "TSLA" in tickers

    def test_us_diff_sign(self):
        log = "[US] Diff: HROW +5.000000\n[US] Diff: TSLA -10.5\n"
        data = fe.parse(log)
        diff_map = {t: v for t, v in data["us_diffs"]}
        assert diff_map["HROW"] == "+5.000000"
        assert diff_map["TSLA"] == "−10.5"

    def test_us_diffs_empty_when_absent(self):
        assert fe.parse("")["us_diffs"] == []

    def test_parses_warning_line(self):
        data = fe.parse(WARNING_LOG)
        assert len(data["warnings"]) == 1
        assert "could not match" in data["warnings"][0]

    def test_parses_error_line(self):
        data = fe.parse(ERROR_LOG)
        assert any("Sheet update failed" in w for w in data["warnings"])

    def test_parses_zerodha_margin(self):
        data = fe.parse("[Indian] Margin: 12345.67\n")
        assert data["zerodha_margin"] == pytest.approx(12345.67)

    def test_parses_negative_zerodha_margin(self):
        data = fe.parse("[Indian] Margin: -500.00\n")
        assert data["zerodha_margin"] == pytest.approx(-500.0)

    def test_zerodha_margin_none_when_absent(self):
        assert fe.parse("")["zerodha_margin"] is None

    def test_parses_sgov_entries(self):
        data = fe.parse(SGOV_LOG)
        names = [n for n, _ in data["sgov"]]
        assert "Robinhood individual (...8902)" in names
        assert "Robinhood individual (...8195)" in names

    def test_parses_sgov_values(self):
        data = fe.parse(SGOV_LOG)
        val_map = {n: v for n, v in data["sgov"]}
        assert val_map["Robinhood individual (...8902)"] == pytest.approx(5710.85)
        assert val_map["ROTH IRA (...*****4882)"] == pytest.approx(37860.19)

    def test_parses_ef_entries(self):
        data = fe.parse(EF_LOG)
        rows = [(cat, inst) for cat, inst, _ in data["ef"]]
        assert ("Bank", "Chase") in rows
        assert ("PPF", "ICICI") in rows
        assert ("CDs", "Marcus") in rows

    def test_parses_ef_values(self):
        data = fe.parse(EF_LOG)
        bank_chase_vals = [bal for cat, inst, bal in data["ef"] if cat == "Bank" and inst == "Chase"]
        assert pytest.approx(11995.54) in bank_chase_vals
        assert pytest.approx(1500.00) in bank_chase_vals
        ppf_vals = [bal for cat, inst, bal in data["ef"] if cat == "PPF"]
        assert ppf_vals == [pytest.approx(2269.00)]

    def test_parses_home_value(self):
        log = "[Home] Value: $732,000.00\n[Home] Mortgage: $393,584.94\n[Home] Equity: $338,415.06\n"
        data = fe.parse(log)
        assert data["home_value"] == pytest.approx(732000.0)
        assert data["home_mortgage"] == pytest.approx(393584.94)
        assert data["home_equity"] == pytest.approx(338415.06)

    def test_home_none_when_absent(self):
        data = fe.parse("")
        assert data["home_value"] is None
        assert data["home_mortgage"] is None
        assert data["home_equity"] is None

    def test_empty_log_gives_safe_defaults(self):
        data = fe.parse("")
        assert data["run_url"] is None
        assert data["indian_pf"] is None
        assert data["us_pf"] is None
        assert data["cash"] is None
        assert data["cash_pct"] is None
        assert data["zerodha_margin"] is None
        assert data["total"] is None
        assert data["indian_diffs"] == []
        assert data["indian_closed"] == []
        assert data["indian_new"] == []
        assert data["us_closed"] == []
        assert data["us_diffs"] == []
        assert data["us_new"] == []
        assert data["sgov"] == []
        assert data["ef"] == []
        assert data["warnings"] == []
        assert data["home_value"] is None
        assert data["home_mortgage"] is None
        assert data["home_equity"] is None


# ── build_subject() ───────────────────────────────────────────────────────────

class TestBuildSubject:
    def _d(self, **kw):
        base = {
            "total": "$786,962.00",
            "cash": None,
            "cash_pct": None,
            "zerodha_margin": None,
            "indian_diffs": [],
            "indian_closed": [],
            "indian_new": [],
            "us_closed": [],
            "us_diffs": [],
            "us_new": [],
            "sgov": [],
            "uninvested_cash": [],
            "ef": [],
            "home_value": None,
            "home_mortgage": None,
            "home_equity": None,
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
            "us_pct": "55.55%",
            "cash": "$112,907.25",
            "cash_pct": "14.64%",
            "net_worth": None,
            "zerodha_margin": None,
            "total": "$786,962.00",
            "indian_diffs": [],
            "indian_closed": [],
            "indian_new": [],
            "us_closed": [],
            "us_diffs": [],
            "us_new": [],
            "sgov": [],
            "uninvested_cash": [],
            "ef": [],
            "home_value": None,
            "home_mortgage": None,
            "home_equity": None,
            "warnings": [],
        }
        base.update(kw)
        return base

    def test_includes_portfolio_totals(self):
        html = fe.build_html(self._quiet())
        assert "$234,629.00" in html
        assert "$552,332.00" in html
        assert "$112,907.25" in html
        assert "$786,962.00" in html

    def test_includes_percentages(self):
        html = fe.build_html(self._quiet())
        assert "29.81%" in html
        assert "55.55%" in html
        assert "14.64%" in html

    def test_cash_row_shown_when_present(self):
        html = fe.build_html(self._quiet(cash="$112,907.25", cash_pct="14.64%"))
        assert "Cash" in html
        assert "$112,907.25" in html

    def test_cash_row_hidden_when_absent(self):
        html = fe.build_html(self._quiet(cash=None, cash_pct=None))
        assert "Cash" not in html

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

    def test_us_diff_shown(self):
        html = fe.build_html(self._quiet(us_diffs=[("HROW", "+5.0")]))
        assert "HROW" in html
        assert "+5.0" in html

    def test_us_diff_negative_shown(self):
        html = fe.build_html(self._quiet(us_diffs=[("TSLA", "−10.5")]))
        assert "TSLA" in html
        assert "−10.5" in html

    def test_html_starts_with_doctype(self):
        html = fe.build_html(self._quiet())
        assert html.startswith("<!DOCTYPE html>")

    def test_sgov_section_shown(self):
        html = fe.build_html(self._quiet(sgov=[
            ("Robinhood individual (...8902)", 5710.85),
            ("ROTH IRA (...*****4882)", 37860.19),
        ]))
        assert "SGOV" in html
        assert "Robinhood individual" in html
        assert "ROTH IRA" in html
        assert "$5,711" in html or "$5710" in html or "5,711" in html

    def test_sgov_section_hidden_when_empty(self):
        html = fe.build_html(self._quiet(sgov=[]))
        assert "SGOV" not in html

    def test_sgov_total_shown(self):
        html = fe.build_html(self._quiet(sgov=[
            ("Account A", 50000.0),
            ("Account B", 63000.0),
        ]))
        assert "113,000" in html or "$113,000" in html

    def test_ef_section_shown(self):
        html = fe.build_html(self._quiet(ef=[
            ("Bank", "Chase", 11995.54),
            ("CDs", "Marcus", 11574.97),
        ]))
        assert "Liquid Reserves" in html
        assert "Bank" in html
        assert "Chase" in html
        assert "11,995.54" in html

    def test_ef_section_hidden_when_empty(self):
        html = fe.build_html(self._quiet(ef=[]))
        assert "Liquid Reserves" not in html

    def test_margin_section_shown_when_above_1000(self):
        html = fe.build_html(self._quiet(zerodha_margin=5000.0))
        assert "Zerodha Margin" in html
        assert "5,000.00" in html

    def test_margin_section_shown_when_negative(self):
        html = fe.build_html(self._quiet(zerodha_margin=-200.0))
        assert "Zerodha Margin" in html
        assert "-200.00" in html

    def test_margin_section_hidden_when_small_positive(self):
        html = fe.build_html(self._quiet(zerodha_margin=500.0))
        assert "Zerodha Margin" not in html

    def test_margin_section_hidden_when_none(self):
        html = fe.build_html(self._quiet(zerodha_margin=None))
        assert "Zerodha Margin" not in html

    def test_ef_grouped_by_category(self):
        html = fe.build_html(self._quiet(ef=[
            ("Bank", "Chase", 1000.0),
            ("CDs", "Marcus", 2000.0),
        ]))
        assert html.index("Bank") < html.index("CDs")
