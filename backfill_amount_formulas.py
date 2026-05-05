#!/usr/bin/env python3
"""One-time backfill: add =C*GOOGLEFINANCE(ticker) formula to existing rows in
the Holdings By Account tab that are missing column D."""

import json
import os
import re

from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID   = os.environ["GSHEET_SHEET_ID"]
ACCOUNT_TAB = os.getenv("ACCOUNT_BREAKDOWN_TAB", "US Holdings By Account")
_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def _sheets_service(readonly: bool = True):
    key_info = json.loads(os.environ["GSHEET_SERVICE_ACCOUNT_JSON"])
    scope = (
        "https://www.googleapis.com/auth/spreadsheets.readonly"
        if readonly
        else "https://www.googleapis.com/auth/spreadsheets"
    )
    creds = service_account.Credentials.from_service_account_info(key_info, scopes=[scope])
    return build("sheets", "v4", credentials=creds)


def backfill():
    service = _sheets_service(readonly=False)

    raw_rows = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SHEET_ID,
            range=f"'{ACCOUNT_TAB}'!A:D",
            valueRenderOption="UNFORMATTED_VALUE",
        )
        .execute()
        .get("values", [])
    )

    updates = []
    for i, row in enumerate(raw_rows):
        if i == 0:
            continue  # skip header
        ticker  = str(row[0]).strip() if row else ""
        account = str(row[1]).strip() if len(row) > 1 else ""
        if not ticker or not account or not _TICKER_RE.match(ticker):
            continue
        has_formula = len(row) > 3 and row[3] not in ("", None)
        if has_formula:
            continue  # already populated

        row_num = i + 1
        updates.append({
            "range": f"'{ACCOUNT_TAB}'!D{row_num}",
            "values": [[f'=C{row_num}*GOOGLEFINANCE("{ticker}")']],
        })

    if not updates:
        print("Nothing to backfill — all rows already have Amount formulas.")
        return

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": updates},
    ).execute()

    print(f"Backfilled {len(updates)} rows with GOOGLEFINANCE formula.")


if __name__ == "__main__":
    backfill()
