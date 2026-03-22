# zerodha-monarch-sync

Automatically syncs your Indian portfolio balance from Google Sheets to [Monarch Money](https://www.monarchmoney.com) daily via GitHub Actions.

## Why this approach

Monarch Money is a US-based personal finance app with no official API. Zerodha (India's largest broker) has an official API — [Kite Connect](https://kite.trade) — but it requires a paid subscription (₹2,000/month) and its session tokens expire daily, requiring a browser-based OAuth login each day.

This repo takes a pragmatic middle path:

- **Google Sheets as the source of truth** — many NRI investors already maintain an Indian portfolio tracker in Sheets with live INR prices via `GOOGLEFINANCE` and a manual INR→USD conversion. No Kite API needed.
- **Direct Monarch GraphQL** — Monarch has no public API, but their web app uses a GraphQL endpoint. The token is long-lived (months), making it suitable for unattended automation.
- **GitHub Actions** — runs daily in the cloud, no local machine dependency.

### Long-term: direct Kite → Monarch sync

If you want to eliminate the Google Sheets middleman entirely, the full end-to-end would be:

```
Kite Connect API → sum holdings in INR → Frankfurter API (INR/USD) → Monarch Money
```

The main friction point is **Kite's daily token expiry**. To fully automate it you'd need:

1. A paid [Kite Connect](https://kite.trade) subscription (₹2,000/month)
2. TOTP-based programmatic login — store your Zerodha TOTP secret in GitHub Secrets and generate the OTP in the script using the `pyotp` library
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

1. Reads your Indian portfolio USD value from a Google Sheet (searches for a labeled row)
2. Updates a manual account in Monarch Money with that balance
3. Runs daily at 9 AM IST via GitHub Actions — no local machine needed

## Setup

### 1. Google Sheets

Your sheet should have a row with a label (e.g. `Indian PF`) and the USD value in the next column:

| Component | Amount | Percentage |
|-----------|--------|------------|
| Indian PF | $230,044.96 | 35.16% |
| US PF | $424,178.51 | 64.84% |

Update `sync.py` with your values:
```python
SHEET_ID = "your-google-sheet-id"        # from the sheet URL
SHEET_TAB = "PF Summary"                 # tab name
LABEL_TO_FIND = "Indian PF"              # label to search for
MONARCH_ACCOUNT_ID = "your-account-id"   # see step 3
```

### 2. Google Cloud service account

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable **Google Sheets API**
3. Create a **Service Account** → download the JSON key
4. Share your Google Sheet with the service account email (Viewer access)

### 3. Monarch Money account ID

In a Claude Code session with the Monarch Money MCP, run:
```
get my accounts
```
Find your manual account and copy its ID.

### 4. Monarch Money token

In a Claude Code session with the Monarch Money MCP (`--enable-write=true`), run:
```
update my Zerodha account balance to 1.00
```
This confirms your setup works. To extract the token for GitHub Actions, run locally:
```bash
python3 -c "
import pickle
with open('monarch_session.pickle', 'rb') as f:
    s = pickle.load(f)
print(s['token'])
"
```
Or authenticate fresh using the `monarchmoney` Python library:
```bash
pip install monarchmoneycommunity
python3 -c "
import asyncio
from monarchmoney import MonarchMoney
async def main():
    mm = MonarchMoney()
    await mm.login('your@email.com', 'yourpassword', save_session=True)
asyncio.run(main())
"
```

### 5. GitHub Secrets & Variables

In your repo → **Settings → Secrets and variables → Actions**:

**Secrets** (sensitive, never visible after saving):

| Secret | Value |
|--------|-------|
| `GSHEET_SERVICE_ACCOUNT_JSON` | Full contents of the service account JSON key file |
| `MONARCH_TOKEN` | Your Monarch Money auth token (from step 4) |

**Variables** (non-sensitive config, visible in logs):

| Variable | Value | Default |
|----------|-------|---------|
| `GSHEET_SHEET_ID` | Your Google Sheet ID (from the URL) | required |
| `GSHEET_TAB` | Sheet tab name | `PF Summary` |
| `GSHEET_LABEL` | Row label to search for | `Indian PF` |
| `MONARCH_ACCOUNT_NAME` | Display name of your Monarch manual account | `Zerodha` |

### 6. Fork and enable Actions

Fork this repo, add your secrets, and the workflow will run daily at 9 AM IST. You can also trigger it manually from the **Actions** tab.

## Token expiry

Monarch tokens are long-lived (months). If the sync starts failing with auth errors, re-extract your token and update the `MONARCH_TOKEN` secret.

## Local cron alternative

If you prefer running locally instead of GitHub Actions:
```bash
pip install google-auth google-auth-httplib2 google-api-python-client monarchmoneycommunity
crontab -e
# Add: 30 3 * * * /path/to/venv/bin/python /path/to/sync.py >> /path/to/sync.log 2>&1
```
