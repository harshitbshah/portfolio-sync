import os
import sys

# Must be set before any sync_* module is imported — they read env at module level.
_TEST_ENV = {
    "GSHEET_SHEET_ID": "test_sheet_id",
    "KITE_API_KEY": "test_kite_key",
    "KITE_ACCESS_TOKEN": "test_kite_token",
    "GSHEET_SERVICE_ACCOUNT_JSON": '{"type": "service_account"}',
    "MONARCH_TOKEN": "test_monarch_token",
}
for k, v in _TEST_ENV.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
