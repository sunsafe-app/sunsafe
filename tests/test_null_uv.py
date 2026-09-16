"""
בדיקה ידנית (לא pytest) לתקלת הפרודקשן של 16.9.2026 — /start_session
שנפל בשקט (2026-09-16).

**מה קרה בפועל.** משתמש שלח "/start_session חיפה" ב-11:56. הלוג הראה:

    ERROR:sunsafe.bot_commands:Failed to handle update: {... '/start_session חיפה'}
    ...
      File "bot_commands.py", line 1527, in _begin_session
        insert_row(
      File "supabase_client.py", line 52, in insert_row
        response.raise_for_status()
    httpx.HTTPStatusError: Client error '400 Bad Request' for url
    '.../rest/v1/exposure_log'

שעתיים קודם, באותו יום ועם אותו קוד, session זהה נפתח בהצלחה. לא
הקוד השתנה — התשובה מ-Open-Meteo השתנתה.

**שלושה כשלים נפרדים הצטרפו כאן, וכל אחד נבדק למטה בנפרד:**

1. get_current_uv היה `return response.json()["current"]["uv_index"]`
   בלי שום בדיקה. Open-Meteo מחזיר לפעמים 200 עם null בשדה הזה, ואז
   None זרם הלאה.
2. uv_index מוגדר בסכמה `double precision not null`, אז None הפך ל-400.
3. insert_row הייתה הפונקציה היחידה ב-supabase_client.py עם
   raise_for_status() חשוף — בלי לוג ובלי SupabaseError. גוף התשובה
   של PostgREST, שמסביר *בדיוק* מה נדחה, נזרק לפח.

ומעל שלושתם כשל רביעי: החריגה עלתה עד poll_forever, נרשמה ללוג,
והמשתמש קיבל **שתיקה מוחלטת**.

לא נוגעים ברשת: httpx ממוקה.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from unittest.mock import patch

import httpx

import supabase_client as sc
import bot_commands as bc
from geo_uv_core import UvUnavailableError, get_current_uv, _nearest_hourly_uv

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = "" if status < 400 else '{"code":"23502","message":"null value in column \\"uv_index\\""}'

    @property
    def is_success(self):
        return self.status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def json(self):
        return self._payload


def _hourly(values):
    return {"time": [f"2026-09-16T{h:02d}:00" for h in range(24)], "uv_index": values}


class _Client:
    """httpx.Client מזויף שמחזיר תמיד את אותו גוף."""
    def __init__(self, payload):
        self._payload = payload

    def get(self, *a, **k):
        return _Resp(self._payload)


# ---------------------------------------------------------------------
# 1. get_current_uv — הערך הרגיל
# ---------------------------------------------------------------------
uv = get_current_uv(_Client({"current": {"uv_index": 6.9}, "hourly": _hourly([1.0] * 24)}), 32.8, 35.0)
check("current תקין מוחזר כמו שהוא", uv == 6.9, f"-> {uv}")

# ---------------------------------------------------------------------
# 2. current=null — נפילה לשעה הקרובה, לא None
# ---------------------------------------------------------------------
# זה המקרה של 11:56 בדיוק: 200 עם null ב-current.
values = [None if h < 6 else float(h) for h in range(24)]
uv = get_current_uv(_Client({"current": {"uv_index": None}, "hourly": _hourly(values)}), 32.8, 35.0)
check("current=null נופל לשעה הקרובה", isinstance(uv, float) and uv > 0, f"-> {uv}")
check("ולעולם לא מחזיר None", uv is not None)

# ---------------------------------------------------------------------
# 3. הכל null — חריגה מפורשת, לא None שזורם ל-DB
# ---------------------------------------------------------------------
# **זו הבדיקה שהייתה תופסת את הבאג המקורי.** הגרסה הקודמת החזירה None
# כאן בשקט, וה-None הגיע עד עמודת not null ב-Postgres.
raised = False
try:
    get_current_uv(_Client({"current": {"uv_index": None}, "hourly": _hourly([None] * 24)}), 32.8, 35.0)
except UvUnavailableError:
    raised = True
check("הכל null -> UvUnavailableError", raised)

# בלוק current חסר לגמרי (לא רק null) לא אמור להיות KeyError
raised = False
try:
    get_current_uv(_Client({}), 32.8, 35.0)
except UvUnavailableError:
    raised = True
check("תשובה בלי current ובלי hourly -> UvUnavailableError", raised)

# ---------------------------------------------------------------------
# 4. _nearest_hourly_uv — מדלג על null, בוחר את הקרוב
# ---------------------------------------------------------------------
now = datetime(2026, 9, 16, 11, 56, tzinfo=timezone.utc)
picked = _nearest_hourly_uv(_hourly([None if h < 6 else float(h) for h in range(24)]), now)
check("נבחרת השעה הקרובה ל-11:56 (12:00)", picked == 12.0, f"-> {picked}")
check("אין ערכים -> None", _nearest_hourly_uv({}, now) is None)
check("חותמת זמן פגומה לא מפילה", _nearest_hourly_uv({"time": ["nope"], "uv_index": [5.0]}, now) is None)

# ---------------------------------------------------------------------
# 5. insert_row — 400 מסביר את עצמו במקום להיעלם
# ---------------------------------------------------------------------
with patch.object(sc.httpx, "post", return_value=_Resp({}, status=400)):
    err = None
    try:
        sc.insert_row("exposure_log", {"telegram_username": "x", "uv_index": None})
    except Exception as e:
        err = e
check("insert_row זורקת SupabaseError ולא HTTPStatusError", isinstance(err, sc.SupabaseError),
      f"-> {type(err).__name__}")
check("והשגיאה מכילה את גוף התשובה של PostgREST", err is not None and "uv_index" in str(err),
      f"-> {str(err)[:80]}")

# ---------------------------------------------------------------------
# 6. _dispatch — כשל ב-handler לא משאיר את המשתמש בשתיקה
# ---------------------------------------------------------------------
def _boom_uv(chat_id, username, args):
    raise UvUnavailableError("no UV")


def _boom_other(chat_id, username, args):
    raise RuntimeError("anything else")


sent = []
with patch.object(bc, "send_message", lambda chat_id, text, **k: sent.append(text)):
    bc._dispatch(_boom_uv, 1, "tester", "", bc.i18n.DEFAULT_LANGUAGE)
    bc._dispatch(_boom_other, 1, "tester", "", bc.i18n.DEFAULT_LANGUAGE)

check("כשל UV -> נשלחה הודעה למשתמש", len(sent) >= 1 and "UV" in sent[0], f"-> {sent[0][:50] if sent else '(אין)'}")
check("כשל UV -> ההודעה אומרת שזו לא אשמת המשתמש", sent and "לא אצלכם" in sent[0])
check("כשל אחר -> גם הוא מקבל תשובה", len(sent) == 2, f"-> {len(sent)} הודעות")
check("כשל אחר -> ההודעה גנרית ולא חושפת traceback",
      len(sent) == 2 and "Traceback" not in sent[1] and "Error" not in sent[1])

# handler שמצליח לא אמור לשלוח שום דבר מהמנגנון הזה
sent.clear()
with patch.object(bc, "send_message", lambda chat_id, text, **k: sent.append(text)):
    bc._dispatch(lambda chat_id, username, args: None, 1, "tester", "", bc.i18n.DEFAULT_LANGUAGE)
check("handler שמצליח לא מייצר הודעת שגיאה", sent == [], f"-> {sent}")

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
