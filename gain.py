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
    total_row: the table's totals row; it gets the portfolio-wide % only when every
    position has a cost (a partial total would be misleading).
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
        total_cost = sum(costs.get(t, 0.0) for _, t in rows)
        value = gain_formula(f"{value_col}{total_row}", total_cost) if not missing and total_cost > 0 else ""
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
