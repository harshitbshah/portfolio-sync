# portfolio-sync

Daily automation that keeps a personal Google Sheet portfolio tracker in sync with live brokerage data. Runs on GitHub Actions, Mon–Fri at 5 AM EST.

## What it does

Two scripts run in sequence each weekday:

### 1. `sync.py` — Zerodha + Monarch balances → PF Summary sheet

- Reads the **Indian PF** USD balance from the *PF Summary* tab and updates the manual Zerodha account in **Monarch Money**
- Reads all US brokerage account balances from Monarch and writes them back to the *PF Summary* tab (bank accounts, CDs, etc.)
- Tracks the total SGOV share count across all brokerage accounts

### 2. `sync_us_portfolio.py` — Monarch holdings → US Portfolio sheet

Monarch Money is the source of truth (it integrates with all brokerage accounts). This script:

- **Updates** Column D (Quantity) for all tickers already in the *US Portfolio* tab
- **Removes** rows for tickers no longer held in any brokerage account (closed positions)
- **Inserts** new rows for tickers that appear in Monarch but not yet in the sheet — Theme and Conviction Rating are left blank for manual entry

The Holdings column (E) auto-recalculates via `GOOGLEFINANCE` formulas once quantities are updated.

## Architecture

```
Google Sheets (Indian PF balance)     Monarch Money (brokerage-linked)
              │                                   │
              ▼                                   ▼
          sync.py                    sync_us_portfolio.py
              │                                   │
     ┌────────┴────────┐              ┌───────────┴───────────┐
     ▼                 ▼              ▼           ▼           ▼
Update Zerodha   Write account    Update qty  Remove closed  Add new
in Monarch       balances + SGOV  for tickers  positions    positions
                       │                                   │
                       └──────────────┬────────────────────┘
                                      ▼
                         Google Sheets (Personal tracker)
```

## Sheet structure

| Tab | Managed by | Description |
|-----|-----------|-------------|
| PF Summary | `sync.py` | Net worth overview — bank, CDs, bonds, Indian + US PF totals |
| US Portfolio | `sync_us_portfolio.py` | US equity positions with Theme, Quantity, Holdings, Conviction |
| US PF P&L | Manual | Realized gains by year; performance vs SPY/QQQ |
| Indian Portfolio | Manual | Indian equity holdings (INR) |
| Indian PF P&L | Manual | Realized gains by Indian FY |
| Subscriptions | Manual | Recurring subscription tracker |

## Setup

### Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable **Google Sheets API**
3. **APIs & Services → Credentials → Create Credentials → Service Account**
4. Download the JSON key
5. Share your Google Sheet with the service account email (**Editor** access — needed for writes)

### Monarch Money token

Authenticate locally using the `monarchmoneycommunity` library:

```bash
pip install monarchmoneycommunity
```

```python
import asyncio, pickle
from monarchmoney import MonarchMoney, RequireMFAException

async def main():
    mm = MonarchMoney(session_file="monarch_session.pickle")
    try:
        await mm.login("your@email.com", "yourpassword", save_session=True)
    except RequireMFAException:
        mfa = input("2FA code: ")
        await mm.multi_factor_authenticate("your@email.com", "yourpassword", mfa)
        mm.save_session("monarch_session.pickle")

asyncio.run(main())
```

Extract the token:
```bash
python3 -c "
import pickle
with open('monarch_session.pickle', 'rb') as f:
    s = pickle.load(f)
print(s['token'])
"
```

### GitHub Secrets

| Secret | Description |
|--------|-------------|
| `MONARCH_TOKEN` | Monarch Money API token — expires every few months (see [Token expiry](#token-expiry)) |
| `GSHEET_SERVICE_ACCOUNT_JSON` | Full contents of the service account JSON key |
| `NOTIFY_EMAIL` | Gmail address to send failure alerts from/to |
| `NOTIFY_EMAIL_APP_PASSWORD` | Gmail App Password — create at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) |

### GitHub Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GSHEET_SHEET_ID` | — | Google Sheet ID from the URL (`/spreadsheets/d/<ID>/edit`) |
| `GSHEET_TAB` | `PF Summary` | Tab name for `sync.py` |
| `GSHEET_LABEL` | `Indian PF` | Row label used to locate the Indian PF balance cell |
| `MONARCH_ACCOUNT_NAME` | `Zerodha` | Monarch display name of the manual Zerodha account |
| `ACCOUNTS_JSON` | *(see below)* | Maps Monarch accounts to PF Summary rows |
| `SGOV_LABEL` | `Total:` | Label to locate the SGOV quantity cell in PF Summary |

`ACCOUNTS_JSON` maps each brokerage account to a row in the PF Summary tab:
```json
[
  {"mask": "1234", "sheet_category": "Bank", "sheet_institution": "Chase"},
  {"mask": "5678", "sheet_category": "CDs",  "sheet_institution": "Marcus"},
  {"monarch_name": "PayPal", "sheet_category": "Bank", "sheet_institution": "PayPal"}
]
```
Use `mask` (last 4 digits) for institution-synced accounts, `monarch_name` for manual accounts.

## Running locally

```bash
pip install google-auth google-auth-httplib2 google-api-python-client

# sync.py
MONARCH_TOKEN=... \
GSHEET_SHEET_ID=... \
GSHEET_SERVICE_ACCOUNT_JSON="$(cat gsheet-key.json)" \
python sync.py

# sync_us_portfolio.py
MONARCH_TOKEN=... \
GSHEET_SHEET_ID=... \
GSHEET_SERVICE_ACCOUNT_JSON="$(cat gsheet-key.json)" \
python sync_us_portfolio.py
```

## Maintenance

### Token expiry

Monarch tokens last several months. When one expires the workflow fails and you'll receive a failure email. To refresh:

1. Re-run the login script above to get a new `monarch_session.pickle`
2. Extract the token and update the `MONARCH_TOKEN` GitHub Secret

### New positions (US Portfolio)

When `sync_us_portfolio.py` inserts a new row, **Theme** and **Conviction Rating** are left blank — fill these in manually after the next run. Column B (ticker) will show a Google Sheets suggestion to "Add Finance chip" — clicking it is optional and purely cosmetic; the `GOOGLEFINANCE` formula in column E uses the ticker directly.

### Closed positions (US Portfolio)

Rows are deleted automatically when a ticker is no longer found in any Monarch brokerage account. If a position disappears temporarily due to a brokerage sync delay, it will be re-inserted on the next run (with blank Theme/Conviction — keep an eye on this).

### Why not Kite Connect directly?

Kite Connect (Zerodha's official API) requires a paid subscription (₹2,000/month) and its session tokens expire daily — requiring a browser OAuth login each day. Using Google Sheets as the intermediary avoids both costs. If you want to eliminate the Sheets middleman:

```python
from kiteconnect import KiteConnect
kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
holdings = kite.holdings()
total_inr = sum(h["last_price"] * h["quantity"] for h in holdings)
rate = requests.get("https://api.frankfurter.app/latest?from=INR&to=USD").json()["rates"]["USD"]
balance_usd = total_inr * rate
```

You'd also need TOTP-based programmatic login using `pyotp` with your Zerodha TOTP secret stored in GitHub Secrets.
