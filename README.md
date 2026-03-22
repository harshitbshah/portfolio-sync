# zerodha-monarch-sync

Automatically syncs your Indian portfolio balance from Google Sheets to [Monarch Money](https://www.monarchmoney.com) daily via GitHub Actions. No local machine needed.

## Why this approach

Monarch Money is a US-based personal finance app with no official API. Zerodha (India's largest broker) has an official API — [Kite Connect](https://kite.trade) — but it requires a paid subscription (₹2,000/month) and its session tokens expire daily, requiring a browser-based OAuth login each day.

This repo takes a pragmatic middle path:

- **Google Sheets as the source of truth** — many NRI investors already maintain an Indian portfolio tracker in Sheets with live INR prices via `GOOGLEFINANCE` and a manual INR→USD conversion. No Kite API needed.
- **Direct Monarch GraphQL** — Monarch has no public API, but their web app uses a GraphQL endpoint. The token is long-lived (months), making it suitable for unattended automation.
- **GitHub Actions** — runs daily in the cloud after Indian market close, no local machine dependency.

### Long-term: direct Kite → Monarch sync

If you want to eliminate the Google Sheets middleman entirely, the full end-to-end would be:

```
Kite Connect API → sum holdings in INR → Frankfurter API (INR/USD) → Monarch Money
```

The main friction point is **Kite's daily token expiry**. To fully automate it you'd need:

1. A paid [Kite Connect](https://kite.trade) subscription (₹2,000/month)
2. TOTP-based programmatic login — store your Zerodha TOTP secret in GitHub Secrets and generate the OTP using the `pyotp` library
3. Replace `get_indian_pf_balance()` in `sync.py` with a Kite SDK call:
   ```python
   from kiteconnect import KiteConnect
   kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
   holdings = kite.holdings()
   total_inr = sum(h["last_price"] * h["quantity"] for h in holdings)
   rate = requests.get("https://api.frankfurter.app/latest?from=INR&to=USD").json()["rates"]["USD"]
   balance_usd = total_inr * rate
   ```

If you use the [Kite MCP](https://kite.trade/docs/connect/v3/) in Claude Code, you can also trigger the sync interactively without writing auth code — though this requires a manual login click per session and is better suited for on-demand use rather than scheduled automation.

## How it works

1. Reads your Google Sheet — searches for a labeled row (e.g. `Indian PF`) and reads the USD value in the next column
2. Looks up your Monarch Money manual account by display name
3. Updates the account balance via Monarch's GraphQL API
4. Runs Mon–Fri at 10 AM UTC (5 AM EST / 6 AM EDT) — after Indian markets close at 3:30 PM IST

## Setup

### 1. Google Sheets

Your sheet should have a row with a label and the USD value in the next column. Example:

| Component | Amount | Percentage |
|-----------|--------|------------|
| Indian PF | $230,044.96 | 35.16% |
| US PF | $424,178.51 | 64.84% |

The label and tab name are configurable via GitHub Variables (see step 4).

Get your **Sheet ID** from the URL:
```
https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit
```

### 2. Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable **Google Sheets API**
3. Go to **APIs & Services → Credentials → Create Credentials → Service Account**
4. Download the JSON key
5. Share your Google Sheet with the service account email (Viewer access is enough)

### 3. Monarch Money token

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

Then extract the token:
```bash
python3 -c "
import pickle
with open('monarch_session.pickle', 'rb') as f:
    s = pickle.load(f)
print(s['token'])
"
```

### 4. GitHub Secrets & Variables

In your repo → **Settings → Secrets and variables → Actions**:

**Secrets** (sensitive):

| Secret | Value |
|--------|-------|
| `GSHEET_SERVICE_ACCOUNT_JSON` | Full contents of the service account JSON key file |
| `MONARCH_TOKEN` | Token extracted in step 3 |
| `NOTIFY_EMAIL` | Gmail address to send failure notifications from |
| `NOTIFY_EMAIL_APP_PASSWORD` | Gmail App Password (not your login password) — create at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) |

**Variables** (non-sensitive config):

| Variable | Description | Example |
|----------|-------------|---------|
| `GSHEET_SHEET_ID` | Your Google Sheet ID from the URL | `10AjE53pQ...` |
| `GSHEET_TAB` | Sheet tab name | `PF Summary` |
| `GSHEET_LABEL` | Row label to search for | `Indian PF` |
| `MONARCH_ACCOUNT_NAME` | Display name of your Monarch manual account | `Zerodha` |

### 5. Run it

**GitHub Actions (recommended — fully automated, no local machine needed):**
Fork this repo, add your secrets and variables from step 4, and the workflow runs automatically on schedule. Trigger a manual run anytime from the **Actions** tab.

**Locally:**
Clone the repo, copy `.env.example` to `.env`, fill in your values, then run:
```bash
pip install google-auth google-auth-httplib2 google-api-python-client
cp .env.example .env
# edit .env with your values
export $(cat .env | xargs) && python sync.py
```

For daily automated runs, add to crontab:
```
0 10 * * 1-5 cd /path/to/zerodha-monarch-sync && export $(cat .env | xargs) && /path/to/venv/bin/python sync.py >> sync.log 2>&1
```

## Failure notifications

If the workflow fails, an email is sent automatically to the address configured in `NOTIFY_EMAIL`. This is useful for catching Monarch token expiry without having to manually check GitHub Actions.

## Token expiry

Monarch tokens are long-lived (months). If the sync starts failing with auth errors (you'll get an email), re-run step 3 to get a fresh token and update the `MONARCH_TOKEN` secret.

