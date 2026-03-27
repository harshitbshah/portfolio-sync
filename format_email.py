#!/usr/bin/env python3
"""Parse sync_output.txt and write an HTML email + subject to GITHUB_OUTPUT."""

import os
import re


def parse(text: str) -> dict:
    data = {
        "run_url":       None,
        "net_worth":     None,   # "$1,144,966.35"
        "indian_pf":     None,   # "$234,629.00"
        "indian_pct":    None,   # "29.81%"
        "us_pf":         None,   # "$552,332.00"
        "us_pct":        None,   # "70.19%"
        "total":         None,   # "$786,962.00"
        "cash":          None,   # "$112,907.25"
        "cash_pct":      None,   # "14.64%"
        "indian_diffs":  [],     # [("FEDFINA", "+500")]
        "indian_closed": [],     # ["WINDLAS"]
        "indian_new":    [],     # [("GPIL", "5804")]
        "us_closed":     [],     # ["ZS"]
        "us_new":        [],     # [("RKLB", "460.87")]
        "zerodha_margin": None,  # 12345.67
        "sgov":          [],     # [("Robinhood individual (...8902)", 56.76)]
        "ef":            [],     # [("Bank", "Chase", 11995.54)]
        "warnings":      [],
    }

    for line in text.splitlines():
        line_s = line.strip()

        if line_s.startswith("Run: http"):
            data["run_url"] = line_s[5:].strip()

        # [Monarch] Net Worth: $1,144,966.35
        m = re.match(r"\[Monarch\] Net Worth: \$([0-9,]+\.\d+)", line_s)
        if m:
            data["net_worth"] = f"${m.group(1)}"

        # PF Summary: Indian PF $234,629.00 29.81% | US PF $552,332.00 70.19% | Total $786,962.00
        if line_s.startswith("PF Summary:"):
            for part in line_s[len("PF Summary:"):].strip().split("|"):
                part = part.strip()
                m = re.match(r"Indian PF \$([0-9,]+\.\d+) ([0-9.]+%)", part)
                if m:
                    data["indian_pf"]  = f"${m.group(1)}"
                    data["indian_pct"] = m.group(2)
                m = re.match(r"US PF \$([0-9,]+\.\d+) ([0-9.]+%)", part)
                if m:
                    data["us_pf"]  = f"${m.group(1)}"
                    data["us_pct"] = m.group(2)
                m = re.match(r"Cash \$([0-9,]+\.\d+) ([0-9.]+%)", part)
                if m:
                    data["cash"]     = f"${m.group(1)}"
                    data["cash_pct"] = m.group(2)
                m = re.match(r"Total \$([0-9,]+\.\d+)", part)
                if m:
                    data["total"] = f"${m.group(1)}"

        # [Indian] Diff: FEDFINA +500
        m = re.match(r"\[Indian\] Diff: (\S+) ([+-]\d+)", line_s)
        if m:
            sign = "+" if m.group(2).startswith("+") else "−"
            data["indian_diffs"].append((m.group(1), f"{sign}{m.group(2).lstrip('+-')}"))

        # [Indian] Closed: WINDLAS
        m = re.match(r"\[Indian\] Closed: (\S+)", line_s)
        if m:
            data["indian_closed"].append(m.group(1))

        # [Indian] Added: GPIL +5804
        m = re.match(r"\[Indian\] Added: (\S+) \+(\S+)", line_s)
        if m:
            data["indian_new"].append((m.group(1), m.group(2)))

        # [US] Closed: ZS
        m = re.match(r"\[US\] Closed: (\S+)", line_s)
        if m:
            data["us_closed"].append(m.group(1))

        # [US] Added: RKLB +460.870000
        m = re.match(r"\[US\] Added: (\S+) \+(\S+)", line_s)
        if m:
            data["us_new"].append((m.group(1), m.group(2)))

        # [Indian] Margin: 12345.67
        m = re.match(r"\[Indian\] Margin: (-?[0-9.]+)", line_s)
        if m:
            data["zerodha_margin"] = float(m.group(1))

        # [SGOV] Robinhood individual (...8902): $5710.85
        m = re.match(r"\[SGOV\] (.+): \$([0-9.]+)", line_s)
        if m:
            data["sgov"].append((m.group(1), float(m.group(2))))

        # [EF] Bank|Chase: $11995.54
        m = re.match(r"\[EF\] ([^|]+)\|([^:]+): \$([0-9.]+)", line_s)
        if m:
            data["ef"].append((m.group(1), m.group(2), float(m.group(3))))

        if re.match(r"(WARNING|ERROR):", line_s):
            data["warnings"].append(line_s)

    return data


def _pill(label, color, bg):
    return (f'<span style="background:{bg};color:{color};font-size:11px;'
            f'padding:2px 7px;border-radius:4px;font-weight:600;">{label}</span>')


def _change_row(ticker, value_html):
    return (f'\n      <tr>'
            f'<td style="padding:4px 0;font-family:monospace;font-size:13px;width:55%;">{ticker}</td>'
            f'<td style="padding:4px 0;font-size:13px;text-align:right;">{value_html}</td>'
            f'</tr>')


def _sgov_row(name, value_html):
    return (f'\n      <tr>'
            f'<td style="padding:3px 0;font-size:12px;color:#555;width:65%;">{name}</td>'
            f'<td style="padding:3px 0;font-size:12px;text-align:right;font-family:monospace;">{value_html}</td>'
            f'</tr>')


def _changes_section(title, rows_html):
    return (f'\n    <div style="padding:0 24px 20px;">'
            f'\n      <div style="color:#aaa;font-size:11px;text-transform:uppercase;letter-spacing:1px;'
            f'margin-bottom:12px;border-top:1px solid #f0f0f0;padding-top:16px;">{title}</div>'
            f'\n      <table style="width:100%;border-collapse:collapse;">{rows_html}\n      </table>'
            f'\n    </div>')


def build_html(data: dict) -> str:
    import datetime
    date_str = datetime.date.today().strftime("%a, %b %-d")

    emoji = "⚠️" if data["warnings"] else "✅"

    # Warning banner
    warning_html = ""
    if data["warnings"]:
        msgs = "<br>".join(f"⚠ {w}" for w in data["warnings"])
        warning_html = (f'\n    <div style="padding:14px 24px;background:#fffbeb;border-bottom:1px solid #fde68a;">'
                        f'\n      <span style="font-size:13px;color:#92400e;">{msgs}</span>'
                        f'\n    </div>')

    # Summary table
    indian_pf  = data["indian_pf"]  or "—"
    indian_pct = data["indian_pct"] or ""
    us_pf      = data["us_pf"]      or "—"
    us_pct     = data["us_pct"]     or ""
    cash       = data["cash"]       or "—"
    cash_pct   = data["cash_pct"]   or ""
    total      = data["total"]      or "—"

    cash_row = ""
    if data["cash"]:
        cash_row = f"""
        <tr>
          <td style="padding:5px 0;color:#555;">Cash</td>
          <td style="padding:5px 0;text-align:right;font-weight:500;">{cash}</td>
          <td style="padding:5px 0;text-align:right;color:#aaa;font-size:13px;padding-left:16px;">{cash_pct}</td>
        </tr>"""

    summary_html = f"""
    <div style="padding:16px 24px 12px;">
      <table style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="padding:5px 0;color:#555;">Indian PF</td>
          <td style="padding:5px 0;text-align:right;font-weight:500;">{indian_pf}</td>
          <td style="padding:5px 0;text-align:right;color:#aaa;font-size:13px;padding-left:16px;">{indian_pct}</td>
        </tr>
        <tr>
          <td style="padding:5px 0;color:#555;">US PF</td>
          <td style="padding:5px 0;text-align:right;font-weight:500;">{us_pf}</td>
          <td style="padding:5px 0;text-align:right;color:#aaa;font-size:13px;padding-left:16px;">{us_pct}</td>
        </tr>{cash_row}
        <tr style="border-top:1px solid #eee;">
          <td style="padding:8px 0 2px;font-weight:600;">Total</td>
          <td style="padding:8px 0 2px;text-align:right;font-weight:600;font-size:16px;">{total}</td>
          <td></td>
        </tr>
      </table>
    </div>"""

    # Indian PF changes
    indian_rows = ""
    for ticker, diff in data["indian_diffs"]:
        color = "#16a34a" if diff.startswith("+") else "#dc2626"
        indian_rows += _change_row(ticker, f'<span style="color:{color};font-weight:600;">{diff}</span>')
    for ticker in data["indian_closed"]:
        indian_rows += _change_row(ticker, _pill("exited", "#dc2626", "#fee2e2"))
    for ticker, qty in data["indian_new"]:
        val = _pill("new", "#16a34a", "#dcfce7") + f' <span style="color:#555;font-size:12px;">{qty} shares</span>'
        indian_rows += _change_row(ticker, val)
    indian_section = _changes_section("Indian PF changes", indian_rows) if indian_rows else ""

    # US PF changes
    us_rows = ""
    for ticker in data["us_closed"]:
        us_rows += _change_row(ticker, _pill("exited", "#dc2626", "#fee2e2"))
    for ticker, qty in data["us_new"]:
        val = _pill("new", "#16a34a", "#dcfce7") + f' <span style="color:#555;font-size:12px;">{qty} shares</span>'
        us_rows += _change_row(ticker, val)
    us_section = _changes_section("US PF changes", us_rows) if us_rows else ""

    # Zerodha margin — only shown if negative or > 1000
    margin_section = ""
    margin = data["zerodha_margin"]
    if margin is not None and (margin < 0 or margin > 1000):
        color = "#dc2626" if margin < 0 else "#16a34a"
        margin_row = (f'\n      <tr>'
                      f'<td style="padding:4px 0;color:#555;">Available cash</td>'
                      f'<td style="padding:4px 0;text-align:right;font-weight:500;color:{color};">₹{margin:,.2f}</td>'
                      f'</tr>')
        margin_section = _changes_section("Zerodha Margin", margin_row)

    # SGOV breakdown — grouped by institution
    sgov_section = ""
    if data["sgov"]:
        groups: dict[str, list] = {}
        for name, value in data["sgov"]:
            inst = "Robinhood" if "robinhood" in name.lower() else "Fidelity"
            groups.setdefault(inst, []).append((name, value))

        sgov_rows = ""
        total_sgov_value = 0.0
        for inst in sorted(groups.keys()):
            entries = sorted(groups[inst], key=lambda x: x[0])
            sgov_rows += (f'\n      <tr><td colspan="2" style="padding:6px 0 2px;'
                          f'font-size:11px;color:#888;text-transform:uppercase;'
                          f'letter-spacing:0.8px;font-weight:700;">{inst}</td></tr>')
            for name, value in entries:
                total_sgov_value += value
                sgov_rows += _sgov_row(name, f"${value:,.0f}")
        sgov_rows += (f'\n      <tr style="border-top:1px solid #f0f0f0;">'
                      f'<td style="padding:5px 0 2px;font-size:12px;font-weight:600;">Total</td>'
                      f'<td style="padding:5px 0 2px;font-size:12px;text-align:right;font-family:monospace;font-weight:600;">${total_sgov_value:,.0f}</td>'
                      f'</tr>')
        sgov_section = _changes_section("SGOV (0–3M Treasury)", sgov_rows)

    # Liquid Reserves breakdown — grouped by category
    ef_section = ""
    if data["ef"]:
        groups: dict[str, list] = {}
        for category, institution, balance in data["ef"]:
            groups.setdefault(category, []).append((institution, balance))

        ef_rows = ""
        total_ef = 0.0
        for category in groups:
            ef_rows += (f'\n      <tr><td colspan="2" style="padding:6px 0 2px;'
                        f'font-size:11px;color:#888;text-transform:uppercase;'
                        f'letter-spacing:0.8px;font-weight:700;">{category}</td></tr>')
            for institution, balance in groups[category]:
                total_ef += balance
                ef_rows += _sgov_row(institution, f"${balance:,.2f}")
        ef_rows += (f'\n      <tr style="border-top:1px solid #f0f0f0;">'
                    f'<td style="padding:5px 0 2px;font-size:12px;font-weight:600;">Total</td>'
                    f'<td style="padding:5px 0 2px;font-size:12px;text-align:right;font-family:monospace;font-weight:600;">${total_ef:,.2f}</td>'
                    f'</tr>')
        ef_section = _changes_section("Liquid Reserves", ef_rows)

    # Footer
    footer_html = ""
    if data["run_url"]:
        footer_html = (f'\n    <div style="padding:8px 24px 10px;border-top:1px solid #f0f0f0;">'
                       f'\n      <a href="{data["run_url"]}" style="color:#aaa;font-size:12px;text-decoration:none;">view run →</a>'
                       f'\n    </div>')

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:14px;color:#1a1a1a;">
<div style="max-width:520px;margin:20px auto;padding:0 16px;">
  <div style="background:#fff;border-radius:8px;overflow:hidden;">
    <div style="padding:20px 24px 16px;border-bottom:1px solid #f0f0f0;">
      <span style="font-size:18px;font-weight:600;">{emoji} Portfolio sync</span>
      <span style="color:#888;margin-left:8px;font-size:13px;">{date_str}</span>
      {f'<div style="margin-top:8px;font-size:26px;font-weight:700;letter-spacing:-0.5px;">{data["net_worth"]}<span style="font-size:13px;font-weight:400;color:#888;margin-left:6px;">net worth</span></div>' if data["net_worth"] else ""}
    </div>{warning_html}{summary_html}{indian_section}{us_section}{margin_section}{sgov_section}{ef_section}{footer_html}
  </div>
  <p style="text-align:center;color:#ccc;font-size:11px;margin-top:8px;">portfolio-sync · GitHub Actions</p>
</div>
</body>
</html>"""


def build_subject(data: dict) -> str:
    emoji = "⚠️" if data["warnings"] else "✅"
    total = data["total"] or ""
    return f"{emoji} Portfolio sync | {total}"


if __name__ == "__main__":
    with open("sync_output.txt") as f:
        text = f.read()

    data = parse(text)
    subject = build_subject(data)
    html = build_html(data)

    with open("email_body.html", "w") as f:
        f.write(html)

    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"subject={subject}\n")

    print(f"Subject: {subject}")
