"""Gain % column helpers shared by the US and Indian portfolio syncs.

The sync writes a formula per position:  =IFERROR(<value cell>/<cost> - 1, "")
The value cell is the sheet's live Holdings column (GOOGLEFINANCE), so the percentage
stays current between syncs; only the cost is refreshed by the daily run.
"""

GAIN_HEADER = "Gain %"


def gain_formula(value_cell: str, cost: float) -> str:
    return f'=IFERROR({value_cell}/{round(cost, 2)}-1,"")'


def build_gain_updates(
    tab: str,
    value_col: str,
    gain_col: str,
    rows: list[tuple[int, str]],
    costs: dict[str, float],
    total_row: int | None,
) -> tuple[list[dict], list[str]]:
    """Return (values batchUpdate data, tickers left blank because their cost is unknown).

    rows: [(sheet_row, ticker)] for every position row currently in the tab.
    costs: {ticker: total cost}; a ticker missing here (or with cost <= 0) gets a blank cell.
    total_row: the table's totals row. It gets the portfolio-wide %; positions with an
    unknown cost are left out of it (their value and cost both), so one small position
    without basis (e.g. a 401k holding) doesn't blank the whole total.
    """
    data = [{"range": f"'{tab}'!{gain_col}1", "values": [[GAIN_HEADER]]}]
    missing = []
    for row, ticker in rows:
        cost = costs.get(ticker, 0.0)
        if cost > 0:
            value = gain_formula(f"{value_col}{row}", cost)
        else:
            value = ""
            missing.append(ticker)
        data.append({"range": f"'{tab}'!{gain_col}{row}", "values": [[value]]})
    if total_row is not None:
        known = [(r, t) for r, t in rows if t not in missing]
        known_cost = sum(costs[t] for _, t in known)
        if not known or known_cost <= 0:
            value = ""
        elif not missing:
            value = gain_formula(f"{value_col}{total_row}", known_cost)
        else:
            value_sum = "(" + "+".join(f"{value_col}{r}" for r, _ in known) + ")"
            value = gain_formula(value_sum, known_cost)
        data.append({"range": f"'{tab}'!{gain_col}{total_row}", "values": [[value]]})
    return data, sorted(missing)


def percent_format_request(grid_id: int, col_index: int, end_row: int) -> dict:
    """Format rows 2..end_row of one column as a percentage with one decimal."""
    return {
        "repeatCell": {
            "range": {
                "sheetId": grid_id,
                "startRowIndex": 1,
                "endRowIndex": end_row,
                "startColumnIndex": col_index,
                "endColumnIndex": col_index + 1,
            },
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
            "fields": "userEnteredFormat.numberFormat",
        }
    }


def total_row_after(rows: list[tuple[int, str]], last_used_row: int) -> int | None:
    """The totals row sits directly below the last position row, if that row exists."""
    if not rows:
        return None
    candidate = max(r for r, _ in rows) + 1
    return candidate if candidate <= last_used_row else None
