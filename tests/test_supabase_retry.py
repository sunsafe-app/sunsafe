"""
בדיקה ידנית (לא pytest) למנגנון ה-retry ב-select_rows, ולכך שכשל
ב-Supabase לא משאיר את המשתמש בלי תשובה (2026-09-13).

המקרה האמיתי שהוביל לזה, מלוגים של פרודקשן: משתמש שיתף מיקום כדי
לפתוח session, Supabase החזיר 504 Gateway Timeout בודד על שליפת
users, החריגה עלתה עד poll_forever ונרשמה ללוג — והמשתמש לא קיבל
שום הודעה. שני תיקונים נבדקים כאן:
  1. select_rows מנסה שוב על 5xx/תקלת רשת, כך שרוב ה-504-ים החולפים
     לא מגיעים בכלל למשתמש.
  2. גם אם הכשל מתמיד, נתיבי התמונה והמיקום ב-handle_update כבר לא
     שותקים — הם מחזירים "נסו שוב בעוד רגע", כמו נתיב הפקודות.

לא נוגעים ברשת: httpx.get/httpx.post ממוקים.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

# tests/ נמצא רמה אחת מתחת לשורש הריפו — מוסיפים את שורש הריפו ל-sys.path
# כדי ש-import bot_commands (ומודולים אחיים אחרים) ימשיך לעבוד גם כשמריצים
# מ-tests/ ולא משורש הריפו.
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import httpx

import supabase_client as sc
import bot_commands as bc

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.text = "" if status < 400 else f'{{"message":"status {status}"}}'
        self._payload = payload if payload is not None else []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=self
            )

    def json(self):
        return self._payload


def responder(sequence):
    """מחזיר httpx.get מדומה שמחזיר את התשובות לפי הסדר."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        item = sequence[min(len(calls) - 1, len(sequence) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return fake_get, calls


# ---------------------------------------------------------------------
# 1) 504 בודד -> ניסיון שני מצליח, בלי שהקורא ידע
# ---------------------------------------------------------------------
fake_get, calls = responder([_Resp(504), _Resp(200, [{"skin_type": 3}])])
with patch.object(sc.httpx, "get", fake_get), patch.object(sc.time, "sleep", lambda s: None):
    rows = sc.select_rows("users", {"telegram_username": "eq.gil612"})
check("a single 504 is retried and succeeds", rows == [{"skin_type": 3}], f"-> {rows}")
check("it took exactly two attempts", len(calls) == 2, f"-> {len(calls)}")

# ---------------------------------------------------------------------
# 2) 504 מתמשך -> נכשל בסוף, אחרי כל הניסיונות
# ---------------------------------------------------------------------
fake_get, calls = responder([_Resp(504)])
with patch.object(sc.httpx, "get", fake_get), patch.object(sc.time, "sleep", lambda s: None):
    try:
        sc.select_rows("users", {"telegram_username": "eq.gil612"})
        check("a persistent 504 still raises SupabaseError", False, "-> no exception")
    except sc.SupabaseError:
        check("a persistent 504 still raises SupabaseError", True)
check("it gave up after 3 attempts, not more", len(calls) == 3, f"-> {len(calls)}")

# ---------------------------------------------------------------------
# 3) שגיאת 4xx -> לא חוזרים עליה בכלל (לא תסתדר לעולם)
# ---------------------------------------------------------------------
fake_get, calls = responder([_Resp(401)])
with patch.object(sc.httpx, "get", fake_get), patch.object(sc.time, "sleep", lambda s: None):
    try:
        sc.select_rows("users", {})
    except sc.SupabaseError:
        pass
check("a 4xx is not retried", len(calls) == 1, f"-> {len(calls)} attempt(s)")

# ---------------------------------------------------------------------
# 4) תקלת רשת -> כן חוזרים
# ---------------------------------------------------------------------
fake_get, calls = responder([httpx.ConnectError("boom"), _Resp(200, [{"ok": True}])])
with patch.object(sc.httpx, "get", fake_get), patch.object(sc.time, "sleep", lambda s: None):
    rows = sc.select_rows("users", {})
check("a network error is retried too", rows == [{"ok": True}] and len(calls) == 2, f"-> {calls}")

# ---------------------------------------------------------------------
# 5) insert לא מקבל retry — 504 על כתיבה עלול להיות כתיבה שכן קרתה
# ---------------------------------------------------------------------
post_calls = []


def fake_post(url, **kwargs):
    post_calls.append(url)
    return _Resp(504)


with patch.object(sc.httpx, "post", fake_post), patch.object(sc.time, "sleep", lambda s: None):
    try:
        sc.insert_row("exposure_log", {"telegram_username": "gil612"})
    except Exception:
        pass
check("insert_row is deliberately NOT retried (no duplicate rows)",
      len(post_calls) == 1, f"-> {len(post_calls)} attempt(s)")

# ---------------------------------------------------------------------
# 6) המשתמש לא נשאר בשתיקה: מיקום + כשל Supabase -> הודעה
# ---------------------------------------------------------------------
messages = []
with patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append(text)), \
     patch.object(bc, "update_rows"), \
     patch.object(bc, "_mirror_incoming_to_admin", lambda *a, **k: None), \
     patch.object(bc, "handle_start_session_location",
                  side_effect=bc.SupabaseError("שליפת נתונים מ-users נכשלה")):
    bc.handle_update({
        "message": {
            "chat": {"id": 123},
            "from": {"username": "gil612"},
            "location": {"latitude": 32.8, "longitude": 35.0},
        }
    })
check("a Supabase failure on the location path answers the user",
      len(messages) == 1 and "נסו שוב" in messages[0], f"-> {messages}")

# אותו דבר בנתיב התמונה
messages.clear()
with patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append(text)), \
     patch.object(bc, "update_rows"), \
     patch.object(bc, "_mirror_incoming_to_admin", lambda *a, **k: None), \
     patch.object(bc, "handle_skin_type_photo",
                  side_effect=bc.SupabaseError("שליפת נתונים מ-users נכשלה")):
    bc.handle_update({
        "message": {
            "chat": {"id": 123},
            "from": {"username": "gil612"},
            "photo": [{"file_id": "f1"}],
        }
    })
check("a Supabase failure on the photo path answers the user too",
      len(messages) == 1 and "נסו שוב" in messages[0], f"-> {messages}")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
print("All checks passed.")
