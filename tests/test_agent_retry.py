"""
בדיקה ידנית (לא pytest) ל-_send_with_retry ב-mcp_agent_loop (2026-10-15).

הרקע: ב-15.10 Gemini החזיר 503 UNAVAILABLE, והגייטקיפר ספג את זה
(fail open ל-VALID) בזמן שה-Agent Loop קרס לגמרי — המשתמשת החיצונית
הראשונה קיבלה הודעת שגיאה גנרית על ההודעה הראשונה שלה, בגלל כשל
שחלף תוך שניות. לא היה שם שום retry.

אי אפשר לגרום ל-503 אמיתי מרצון, ולכן הבדיקות כאן מזריקות אותו דרך
chat מזויף. אין כאן רשת, אין מפתח, ואין קריאה ל-Gemini.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.genai import errors as genai_errors

import mcp_agent_loop as mal

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


def api_error(code):
    """שגיאה אמיתית מה-SDK — ServerError ל-5xx, ClientError ל-4xx, כמו ב-raise_for_response."""
    body = {"error": {"code": code, "message": "injected", "status": "INJECTED"}}
    cls = genai_errors.ServerError if code >= 500 else genai_errors.ClientError
    return cls(code, body)


class FakeChat:
    """מחקה chat.send_message: נכשל N פעמים ואז מצליח, ושומר מה נשלח."""

    def __init__(self, fail_times=0, code=503):
        self.fail_times = fail_times
        self.code = code
        self.sent = []

    def send_message(self, message):
        self.sent.append(message)
        if len(self.sent) <= self.fail_times:
            raise api_error(self.code)
        return f"answer after {len(self.sent)} call(s)"


# ההשהיה המעריכית האמיתית היא 1 ואז 2 שניות. כאן מאפסים אותה —
# בודקים את ההתנהגות, לא את השעון.
mal.AGENT_RETRY_BASE_SECONDS = 0.0

# התקציב האמיתי הוא דלי אסימונים עם שעון; מחליפים אותו במונה.
spend_calls = []


def fake_spend(purpose, timeout=None):
    spend_calls.append(purpose)
    return True


mal.spend_gemini = fake_spend


# ---------------------------------------------------------------------
# 1. 503 חולף — מנסה שוב ומחזיר תשובה
# ---------------------------------------------------------------------
spend_calls.clear()
chat = FakeChat(fail_times=2, code=503)
result = mal._send_with_retry(chat, "task", "test")
check("503 פעמיים ואז הצלחה -> מחזיר תשובה", result == "answer after 3 call(s)", f"-> {result!r}")
check("נקרא בדיוק 3 פעמים", len(chat.sent) == 3, f"-> {len(chat.sent)}")

# ---------------------------------------------------------------------
# 2. אותה הודעה בדיוק נשלחת בכל ניסיון — בלי כפל ובלי מוטציה
# ---------------------------------------------------------------------
check("אותה הודעה בכל ניסיון", chat.sent == ["task", "task", "task"], f"-> {chat.sent}")

# ---------------------------------------------------------------------
# 3. כל ניסיון צורך טוקן מהתקציב (זו באמת קריאה נוספת ל-Gemini)
# ---------------------------------------------------------------------
check("3 ניסיונות -> 3 חיובי תקציב", len(spend_calls) == 3, f"-> {len(spend_calls)}")

# ---------------------------------------------------------------------
# 4. שגיאה שאינה זמנית נכשלת מיד, בלי לבזבז ניסיונות ותקציב
# ---------------------------------------------------------------------
for code in (400, 401, 403, 404):
    spend_calls.clear()
    chat = FakeChat(fail_times=99, code=code)
    try:
        mal._send_with_retry(chat, "task", "test")
        check(f"{code} נכשל מיד", False, "-> לא נזרקה שגיאה")
    except genai_errors.APIError as e:
        check(f"{code} נכשל מיד", e.code == code and len(chat.sent) == 1,
              f"-> {len(chat.sent)} קריאה/ות")
        check(f"{code} לא בזבז תקציב", len(spend_calls) == 1, f"-> {len(spend_calls)}")

# ---------------------------------------------------------------------
# 5. כל הקודים הזמניים אכן נחשבים כזמניים
# ---------------------------------------------------------------------
for code in sorted(mal.GEMINI_RETRY_CODES):
    chat = FakeChat(fail_times=1, code=code)
    try:
        mal._send_with_retry(chat, "task", "test")
        check(f"{code} נחשב זמני", len(chat.sent) == 2, f"-> {len(chat.sent)} קריאות")
    except genai_errors.APIError:
        check(f"{code} נחשב זמני", False, "-> נזרק במקום לנסות שוב")

# ---------------------------------------------------------------------
# 6. כשל מתמשך — נכנע אחרי AGENT_RETRY_ATTEMPTS ומעביר את השגיאה המקורית
# ---------------------------------------------------------------------
spend_calls.clear()
chat = FakeChat(fail_times=99, code=503)
try:
    mal._send_with_retry(chat, "task", "test")
    check("503 מתמשך -> נכנע", False, "-> לא נזרקה שגיאה")
except genai_errors.ServerError as e:
    check("503 מתמשך -> נכנע", len(chat.sent) == mal.AGENT_RETRY_ATTEMPTS,
          f"-> {len(chat.sent)} קריאות (תקרה {mal.AGENT_RETRY_ATTEMPTS})")
    check("השגיאה המקורית מועברת, לא נבלעת", e.code == 503, f"-> {e.code}")

# ---------------------------------------------------------------------
# 7. תקציב שנגמר עוצר את הניסיונות במקום לחרוג מהקצב
# ---------------------------------------------------------------------
mal.spend_gemini = lambda purpose, timeout=None: False
chat = FakeChat(fail_times=0)
try:
    mal._send_with_retry(chat, "task", "test")
    check("תקציב ריק -> לא קוראים ל-Gemini", False, "-> לא נזרקה שגיאה")
except RuntimeError:
    check("תקציב ריק -> לא קוראים ל-Gemini", len(chat.sent) == 0, f"-> {len(chat.sent)} קריאות")
mal.spend_gemini = fake_spend

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
