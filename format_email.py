#!/usr/bin/env python3
"""Parse sync_output.txt, write email_body.txt and subject to GITHUB_OUTPUT."""

import os
import re


def parse(text: str) -> dict:
    data = {
        "run_url": None,
        "zerodha_balance": None,
        "us_added": [],   # list of (ticker, qty_str)
        "us_removed": [], # list of ticker
        "us_updated": 0,
        "warnings": [],
    }

    for line in text.splitlines():
        if line.startswith("Run: http"):
            data["run_url"] = line[5:].strip()

        # "Updated: Zerodha → $234,794.88"
        m = re.match(r"Updated: .+ → \$([0-9,]+\.\d+)", line.strip())
        if m:
            data["zerodha_balance"] = m.group(1)

        # "  NVDA   : 92.3431 shares (Theme/Conviction: fill manually)"
        m = re.match(r"\s+(\w+)\s*: ([0-9,]+\.\d+) shares \(Theme", line)
        if m:
            data["us_added"].append((m.group(1), m.group(2)))

        # "Removing N closed positions: ['XYZ', ...]"
        m = re.match(r"Removing \d+ closed positions: \[(.+)\]", line)
        if m:
            data["us_removed"] = [t.strip().strip("'") for t in m.group(1).split(",")]

        # "Done. Updated 30, removed 0, added 0."
        m = re.match(r"Done\. Updated (\d+), removed \d+, added \d+\.", line)
        if m:
            data["us_updated"] = int(m.group(1))

        # Warnings from our scripts only (skip Node/pip noise)
        if re.search(r"^\s*(WARNING|ERROR):", line):
            data["warnings"].append(line.strip())

    return data


def build_subject(data: dict) -> str:
    emoji = "⚠️" if data["warnings"] else "✅"
    balance = f"${data['zerodha_balance']}" if data["zerodha_balance"] else ""

    changes = []
    for t, _ in data["us_added"]:
        changes.append(f"+{t}")
    for t in data["us_removed"]:
        changes.append(f"−{t}")

    if changes:
        change_str = ", ".join(changes)
        return f"{emoji} Portfolio sync | {balance} | {change_str}"
    else:
        return f"{emoji} Portfolio sync | {balance} | no changes"


def build_body(data: dict) -> str:
    lines = []

    if data["warnings"]:
        lines += ["WARNINGS"]
        for w in data["warnings"]:
            lines.append(f"  {w}")
        lines.append("")

    if data["zerodha_balance"]:
        lines.append(f"Zerodha synced: ${data['zerodha_balance']}")

    has_changes = data["us_removed"] or data["us_added"]

    if has_changes:
        lines.append("")
        lines.append("US Portfolio changes:")
        if data["us_removed"]:
            lines.append(f"  Closed:  {', '.join(data['us_removed'])}")
        if data["us_added"]:
            lines.append(f"  New:     {', '.join(t for t, _ in data['us_added'])}")
            for ticker, qty in data["us_added"]:
                lines.append(f"           {ticker}: {qty} shares")
            lines.append("  → fill Theme/Conviction for new positions")
    else:
        n = data["us_updated"]
        lines.append(f"US Portfolio: {n} position{'s' if n != 1 else ''}, no changes")

    if data["run_url"]:
        lines += ["", f"── view run: {data['run_url']}"]

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    with open("sync_output.txt") as f:
        text = f.read()

    data = parse(text)
    subject = build_subject(data)
    body = build_body(data)

    with open("email_body.txt", "w") as f:
        f.write(body)

    # Write subject to GITHUB_OUTPUT for the workflow to pick up
    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"subject={subject}\n")

    print(f"Subject: {subject}\n")
    print(body, end="")
