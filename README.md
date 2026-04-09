# portfolio-sync

Daily automation that keeps a personal Google Sheet portfolio tracker in sync with live brokerage data. Runs on GitHub Actions, Mon–Sat at 5 AM EST (Saturday captures Friday's US market close). Sends an HTML email summary on every run (success or failure).

## What it does

Five scripts run in sequence each weekday (Mon–Sat):

### 1. `kite_auth.py` — Zerodha headless login

Validates a cached enctoken (`KITE_ENCTOKEN_CACHE` GitHub variable) before attempting a full login. If the token is still valid it is reused silently — no fresh login, no Zerodha security email. A full login (user ID + password + TOTP via `pyotp`) only runs when the cached token has expired. Writes the enctoken to `$GITHUB_OUTPUT` for downstream steps.

### 2. `sync_indian_portfolio.py` — Zerodha holdings → Indian Portfolio sheet

- Fetches all settled DEMAT holdings from Zerodha (via enctoken auth)
- **Updates** Column C (Quantity) for all tickers already in the *Indian Portfolio* tab
- **Removes** rows for tickers no longer held in Zerodha (closed positions)
- **Inserts** new rows for tickers in Zerodha not yet in the sheet — Theme is left blank for manual entry
- Fetches available cash balance from the Kite margins API and emits a `[Indian] Margin:` log line for the email

### 3. `sync.py` — Monarch balances → PF Summary sheet + email data

- Reads the **Indian PF** USD balance from the *PF Summary* tab and updates the manual Zerodha account in **Monarch Money**
- Reads all US brokerage account balances from Monarch and writes them to the *PF Summary* tab (bank accounts, PPF, CDs)
- Fetches SGOV holdings across all brokerage accounts — writes total share count to the PF Summary sheet, and emits per-account dollar values as `[SGOV]` log lines for the email
- Emits per-account balance lines as `[EF]` log lines (bank, PPF, CDs) for the Liquid Reserves email section
- Reads the **PF Breakdown** table (Indian PF / US PF / Cash rows) and emits a parseable `PF Summary:` line for the email

### 4. `sync_us_portfolio.py` — Monarch holdings → US Portfolio sheet

Monarch Money is the source of truth (it integrates with all US brokerage accounts). This script:

- **Updates** Column D (Quantity) for all tickers already in the *US Portfolio* tab
- **Removes** rows for tickers no longer held in any brokerage account (closed positions)
- **Inserts** new rows for tickers that appear in Monarch but not yet in the sheet — Theme and Conviction Rating are left blank for manual entry
- **Syncs the *US Holdings By Account* tab** with a flat Ticker | Account | Qty breakdown across all brokerage accounts — updated incrementally (preserves user-added columns)

Skips cash/money-market instruments and SGOV (managed separately via PF Summary): `CUR:USD`, `FCASH`, `FDRXX`, `SPAXX`, `SGOV`.

The Holdings column (E) auto-recalculates via `GOOGLEFINANCE` formulas once quantities are updated.

### 5. `format_email.py` — HTML email builder

Parses the combined log (`sync_output.txt`) from all prior steps and generates an HTML email with:

- **Portfolio summary table** — Indian PF / US PF / Cash / Total with allocation percentages
- **Indian PF changes** — quantity diffs (±shares), new positions, closed positions
- **US PF changes** — new positions, closed positions
- **Zerodha Margin** — available cash from Kite margins API; only shown when < 0 (needs attention) or > ₹1000 (meaningful idle cash)
- **SGOV (0–3M Treasury)** — per-account dollar value breakdown, grouped by institution (Fidelity / Robinhood)
- **Liquid Reserves** — bank / PPF / CDs balances grouped by category, with total
- **Warning banner** — shown if any WARNING or ERROR lines were emitted

A success email is sent after every run. A failure email (with the raw log) is sent if any step exits non-zero.

## Architecture

```
                          Zerodha (user + password + TOTP)
                                        │
                                        ▼
                                  kite_auth.py
                              (validates cached enctoken,
                               fresh login only if expired)
                                        │
                            enctoken (via $GITHUB_OUTPUT)
                                        │
          ┌─────────────────────────────┼──────────────────────────────────────┐
          │                             │                                        │
          ▼                             ▼                                        ▼
Zerodha (enctoken)    Google Sheets (Indian PF balance)    Monarch Money (brokerage-linked)
        │                           │                                   │
        ▼                           ▼                                   ▼
sync_indian_portfolio.py        sync.py                  sync_us_portfolio.py
        │                    ┌──────┴──────┐              ┌─────────────┴─────────────┐
        ▼                    ▼             ▼              ▼             ▼             ▼
 Update Indian         Update Zerodha  Write balances  Update qty  Remove closed  Add new
 Portfolio tab         in Monarch      +SGOV to sheet  for tickers  positions    positions
                            │                                                        │
                            └───────────────────┬────────────────────────────────────┘
                                                ▼
                                  Google Sheets (Personal tracker)
                                                │
                                                ▼
                                       format_email.py
                                                │
                                                ▼
                                      HTML email (Gmail SMTP)
```

## Sheet structure

| Tab | Managed by | Description |
|-----|-----------|-------------|
| PF Summary | `sync.py` | Net worth overview — bank, CDs, PPF, SGOV quantity, Indian + US PF + Cash totals |
| US Portfolio | `sync_us_portfolio.py` | US equity positions with Theme, Quantity, Holdings, Conviction |
| US Holdings By Account | `sync_us_portfolio.py` | Flat Ticker \| Account \| Qty table; per-brokerage breakdown across all accounts |
| US PF P&L | Manual | Realized gains by year; performance vs SPY/QQQ |
| Indian Portfolio | `sync_indian_portfolio.py` | Indian equity holdings — Zerodha quantities synced daily |
| Indian PF P&L | Manual | Realized gains by Indian FY |
| Subscriptions | Manual | Recurring subscription tracker |

### PF Summary tab

Account balances (driven by `ACCOUNTS_JSON`) and portfolio totals. `sync.py` writes to column C.

```
┌─────────────┬──────────────┬─────────────┐
│ Category    │ Institution  │ Balance     │  ← written by sync.py
├─────────────┼──────────────┼─────────────┤
│ Bank        │ Chase        │  $5,000.00  │
│ Bank        │ Chase        │  $2,500.00  │
│ CDs         │ Marcus       │ $10,000.00  │
│ PPF         │ ICICI        │  $8,000.00  │
│ Indian PF   │              │ $180,000.00 │  ← read by sync.py → pushed to Monarch
├─────────────┼──────────────┼─────────────┤
│ SGOV        │ Total:       │         120 │  ← share count, written by sync.py
├─────────────┼──────────────┼─────────────┤
│ PF Breakdown│              │             │  ← header row (PF_BREAKDOWN_LABEL)
│ Indian PF   │ $180,000     │     32.00%  │
│ US PF       │ $380,000     │     68.00%  │
│ Cash        │  $50,000     │      9.00%  │
│ Total       │ $560,000     │            │
└─────────────┴──────────────┴─────────────┘
```

### Indian Portfolio tab

Zerodha DEMAT holdings. `sync_indian_portfolio.py` updates column C daily.

```
┌───────────┬────────────┬──────────┐
│ Theme     │ Ticker     │ Quantity │  ← col C written by sync_indian_portfolio.py
├───────────┼────────────┼──────────┤
│ Finance   │ FEDFINA    │     1200 │
│ Infra     │ GPIL       │     5804 │
│ Healthcare│ NH         │      300 │
│           │ NIFTYBEES  │      500 │  ← Theme left blank for new positions
│ ...       │ ...        │      ... │
└───────────┴────────────┴──────────┘
```

### US Portfolio tab

Monarch Money holdings. `sync_us_portfolio.py` updates column D daily. Column E auto-recalculates via `GOOGLEFINANCE`.

```
┌───────────┬────────┬──────────┬──────────┬──────────────┬────────────┐
│ Theme     │ Ticker │ % Total  │ Quantity │ Holdings ($) │ Conviction │
├───────────┼────────┼──────────┼──────────┼──────────────┼────────────┤
│ Cloud     │ AMZN   │   8.50%  │   45.00  │   $9,200.00  │ High       │
│ Defense   │ RKLB   │   3.20%  │  460.87  │   $3,500.00  │ Medium     │
│           │ NVDA   │   5.10%  │   22.00  │   $5,500.00  │            │  ← blank for new
│ ...       │ ...    │    ...   │     ...  │        ...   │ ...        │
└───────────┴────────┴──────────┴──────────┴──────────────┴────────────┘
  col A       col B    col C      col D       col E          col F
              ↑ read   formula    ↑ written   GOOGLEFINANCE
              by script           by script   formula
```

### Cells read by `sync.py` (PF Summary tab)

| What | How located | Used for |
|------|------------|----------|
| Indian PF balance | Row where col A/B = `GSHEET_LABEL` (default `Indian PF`), value in next column | Push to Monarch as Zerodha balance |
| Account balance rows | Col A = `sheet_category`, Col B = `sheet_institution` from `ACCOUNTS_JSON` | Pull from Monarch, write to Col C |
| SGOV quantity cell | Cell immediately to the right of `SGOV_LABEL` (default `Total:`) | Write total SGOV share count |
| PF Breakdown table | Rows below `PF_BREAKDOWN_LABEL` header; columns: label \| amount \| pct | Email summary — Indian PF / US PF / Cash / Total |

### Cells written by `sync.py` (PF Summary tab)

| What | Column | Notes |
|------|--------|-------|
| Account balances | C | One row per `ACCOUNTS_JSON` entry, matched by category + institution |
| SGOV quantity | Right of `SGOV_LABEL` | Total share count across all brokerage accounts |

### Cells read/written by `sync_indian_portfolio.py` (Indian Portfolio tab)

| What | Column | Notes |
|------|--------|-------|
| Read tickers + quantities | B, C | Diff against Zerodha holdings |
| Quantity | C | Updated for all existing positions |
| New rows | A–C | Inserted at end; Theme (col A) left blank |
| Closed rows | — | Entire row deleted via `deleteRange` (not `deleteDimension` — see note below) |

### Cells read/written by `sync_us_portfolio.py` (US Portfolio tab)

| What | Column | Notes |
|------|--------|-------|
| Read tickers | B | Diff against Monarch holdings |
| Quantity | D | Updated for all existing positions |
| New rows | A–F | Theme (A), Ticker (B), % of total (C formula), Qty (D), Holdings/GOOGLEFINANCE (E formula), Conviction (F) |
| Closed rows | — | Entire row deleted via `deleteRange` (not `deleteDimension` — see note below) |

### US Holdings By Account tab

Flat Ticker | Account | Qty table, updated incrementally each run. Account names have the mask suffix stripped (e.g. `Robinhood IRA (...8902)` → `Robinhood IRA`).

```
┌────────┬──────────────────┬─────────┬────────────────────────────────┐
│ Ticker │ Account          │ Qty     │ Amount (user-added col D)       │
├────────┼──────────────────┼─────────┼────────────────────────────────┤
│ HROW   │ Robinhood IRA    │    5.00 │ =IF(A2="",,IFERROR(C2*GOOG..)) │
│ HROW   │ Robinhood Indiv  │   10.00 │                                │
│ AAPL   │ Fidelity ROTH    │   12.50 │                                │
│ ...    │ ...              │     ... │                                │
└────────┴──────────────────┴─────────┴────────────────────────────────┘
```

Sync behaviour: only columns A–C are touched. User-added columns (D+) are preserved. New rows inserted with `insertDimension + inheritFromBefore=True` so the column D formula extends automatically.

> **Note on row deletion:** Both sheets are Google Sheets native tables with Finance smart chips. The `deleteDimension` API call returns HTTP 500 on such sheets. Both scripts use `deleteRange` with `shiftDimension: ROWS` instead, which is Google's recommended approach for table row deletion.

## Structured log lines

| Pattern | Example | Used for |
|---------|---------|----------|
| `[Indian] Diff: TICKER ±N` | `[Indian] Diff: FEDFINA +500` | Indian PF quantity change |
| `[Indian] Closed: TICKER` | `[Indian] Closed: WINDLAS` | Indian position exited |
| `[Indian] Added: TICKER +QTY` | `[Indian] Added: GPIL +5804` | New Indian position |
| `[Indian] Margin: N.NN` | `[Indian] Margin: 12345.67` | Available cash in Zerodha (INR) |
| `[US] Diff: TICKER ±N.NN` | `[US] Diff: HROW +5.00` | US quantity change (suppressed if < 0.01) |
| `[US] Closed: TICKER` | `[US] Closed: ZS` | US position exited |
| `[US] Added: TICKER +QTY` | `[US] Added: RKLB +460.87` | New US position |
| `[SGOV] NAME: $VALUE` | `[SGOV] Fidelity ROTH (...1234): $12000.00` | Per-account SGOV dollar value |
| `[EF] CATEGORY\|INSTITUTION: $BALANCE` | `[EF] Bank\|Chase: $5000.00` | Per-account liquid reserves balance |
| `PF Summary: ...` | `PF Summary: Indian PF $180k 32% \| US PF $380k 68% \| Cash $50k 9% \| Total $560k` | Portfolio breakdown for email header |

## Email preview

A sample of the HTML email generated by `format_email.py` is included at [`email_sample.html`](email_sample.html). The email includes portfolio summary, Indian/US PF changes, SGOV breakdown, Zerodha margin, and liquid reserves.

![Email preview](assets/email_preview.png)

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
| `ZERODHA_USER_ID` | Zerodha login user ID |
| `ZERODHA_PASSWORD` | Zerodha login password |
| `ZERODHA_TOTP_KEY` | Base32 TOTP secret from Zerodha 2FA setup — found in the QR code during 2FA enrollment |
| `MONARCH_TOKEN` | Monarch Money API token — expires every few months (see [Token expiry](#token-expiry)) |
| `GSHEET_SERVICE_ACCOUNT_JSON` | Full contents of the service account JSON key |
| `NOTIFY_EMAIL` | Gmail address used to send and receive sync notifications |
| `NOTIFY_EMAIL_APP_PASSWORD` | Gmail App Password — create at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) |
| `GH_VARIABLES_PAT` | Personal Access Token with `repo` scope — used to update the `KITE_ENCTOKEN_CACHE` variable after each run. Create at [github.com/settings/tokens](https://github.com/settings/tokens) |

### GitHub Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GSHEET_SHEET_ID` | — | Google Sheet ID from the URL (`/spreadsheets/d/<ID>/edit`) |
| `GSHEET_TAB` | `PF Summary` | Tab name for `sync.py` |
| `GSHEET_LABEL` | `Indian PF` | Row label used to locate the Indian PF balance cell |
| `MONARCH_ACCOUNT_NAME` | `Zerodha` | Monarch display name of the manual Zerodha account |
| `ACCOUNTS_JSON` | *(required — see below)* | Maps Monarch accounts to PF Summary rows |
| `SGOV_LABEL` | `Total:` | Label to locate the SGOV quantity cell in PF Summary |
| `PF_BREAKDOWN_LABEL` | `PF Breakdown` | Header label that marks the portfolio breakdown table in PF Summary |
| `INDIAN_PORTFOLIO_TAB` | `Indian Portfolio` | Tab name for `sync_indian_portfolio.py` |
| `US_PORTFOLIO_TAB` | `US Portfolio` | Tab name for `sync_us_portfolio.py` |
| `ACCOUNT_BREAKDOWN_TAB` | `US Holdings By Account` | Tab name for the per-account breakdown |
| `KITE_ENCTOKEN_CACHE` | *(auto-managed)* | Cached Zerodha enctoken — updated automatically after each run to avoid repeated logins |

`ACCOUNTS_JSON` maps each Monarch account to a row in the PF Summary tab:
```json
[
  {"mask": "1234", "sheet_category": "Bank", "sheet_institution": "Chase"},
  {"mask": "5678", "sheet_category": "CDs",  "sheet_institution": "Marcus"},
  {"monarch_name": "PayPal", "sheet_category": "Bank", "sheet_institution": "PayPal"}
]
```
Use `mask` (last 4 digits) for institution-synced accounts, `monarch_name` for manual accounts. For duplicate `sheet_category` + `sheet_institution` pairs (e.g. two Chase accounts), entries are matched in the order they appear in the sheet. These same entries also drive the **Liquid Reserves** section of the email.

## Running locally

```bash
pip install google-auth google-auth-httplib2 google-api-python-client pyotp requests

# 1. Get Zerodha enctoken
ZERODHA_USER_ID=... ZERODHA_PASSWORD=... ZERODHA_TOTP_KEY=... python kite_auth.py
# Copy the printed enctoken, then:

# 2. Sync Indian portfolio
KITE_ACCESS_TOKEN=<enctoken> \
GSHEET_SHEET_ID=... \
GSHEET_SERVICE_ACCOUNT_JSON="$(cat gsheet-key.json)" \
python sync_indian_portfolio.py

# 3. Sync PF Summary
MONARCH_TOKEN=... \
GSHEET_SHEET_ID=... \
GSHEET_SERVICE_ACCOUNT_JSON="$(cat gsheet-key.json)" \
python sync.py

# 4. Sync US portfolio
MONARCH_TOKEN=... \
GSHEET_SHEET_ID=... \
GSHEET_SERVICE_ACCOUNT_JSON="$(cat gsheet-key.json)" \
python sync_us_portfolio.py
```

Copy `.env.example` to `.env` and fill in your values for a more convenient local setup.

## Running tests

```bash
python -m pytest tests/
```

Tests cover `kite_auth.py` (login flow), `sync.py` (pure logic functions), `sync_indian_portfolio.py` (sync logic), `sync_us_portfolio.py` (holdings sync + account tab), and `format_email.py` (parsing and HTML generation). Google Sheets and Monarch API calls are mocked. 171 tests.

## Maintenance

### Token expiry

Monarch tokens last several months. When one expires the workflow fails and you'll receive a failure email. To refresh:

1. Re-run the login script above to get a new `monarch_session.pickle`
2. Extract the token and update the `MONARCH_TOKEN` GitHub Secret

### Zerodha login emails

`kite_auth.py` caches the enctoken in `KITE_ENCTOKEN_CACHE` (GitHub Variable) and reuses it across runs. The enctoken expires at midnight IST each day, so one fresh login (and one Zerodha security email) will occur on the first run of each trading day. Repeated manual triggers within the same day reuse the cache silently.

Long-term fix to eliminate the daily email: use a **self-hosted GitHub Actions runner** with a static IP — Zerodha stops flagging a recognized IP after the first login.

### New positions (US Portfolio)

When `sync_us_portfolio.py` inserts a new row, **Theme** and **Conviction Rating** are left blank — fill these in manually after the next run. Column B (ticker) will show a Google Sheets suggestion to "Add Finance chip" — clicking it is optional and purely cosmetic; the `GOOGLEFINANCE` formula in column E uses the ticker directly.

### Closed positions

Rows are deleted automatically when a ticker is no longer found in the source (Zerodha for Indian Portfolio, Monarch for US Portfolio). If a position disappears temporarily due to a brokerage sync delay, it will be re-inserted on the next run (with blank Theme/Conviction — keep an eye on this).

### SGOV

SGOV is excluded from the US Portfolio tab (`_SKIP_TICKERS`) and tracked separately:
- Total share count is written to the cell to the right of `SGOV_LABEL` in PF Summary
- Per-account dollar values appear in the **SGOV (0–3M Treasury)** email section, grouped by institution (Fidelity / Robinhood)
