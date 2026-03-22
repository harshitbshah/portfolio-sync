# zerodha-monarch-sync

Automatically syncs your Indian portfolio balance from Google Sheets to [Monarch Money](https://www.monarchmoney.com) daily via GitHub Actions.

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

### 5. GitHub Secrets

In your repo → **Settings → Secrets → Actions**, add:

| Secret | Value |
|--------|-------|
| `GSHEET_SERVICE_ACCOUNT_JSON` | Full contents of the service account JSON key file |
| `MONARCH_TOKEN` | Token string from step 4 |

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
