"""
בדיקה ידנית (לא pytest) לעיבוד העדכונים המקבילי (2026-09-16).

**הרקע.** poll_forever עיבד עדכונים בטור: handle_update נקרא בלופ, אחד
אחרי השני. משתמש אחד ששאל שאלה חופשית (הרמת תהליך MCP + כמה סבבי
Gemini — שניות) עצר את *כל* השאר עד שקיבל תשובה. זו הייתה התקרה
האמיתית על מספר משתמשים בו-זמנית, ולא שום דבר בחומרה.

**מה שהשינוי חייב לשמור, ולכן נבדק כאן.** מקביליות בין שיחות היא
המטרה, אבל מקביליות *בתוך* שיחה היא באג: שתי הודעות מאותו משתמש
חייבות להתעבד בסדר שנשלחו, אחרת "/start_session" ו-"תל אביב" עלולים
להתחלף, ושתי לחיצות על בורר סוג-העור יתנגשו על _pending_skin_type_pick.
לכן מנעול לכל chat_id — טורי בתוך שיחה, מקבילי בין שיחות.

לא נוגעים ברשת: handle_update מוחלף בכפיל שרק ישן ורושם.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import bot_commands as bc

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


def _msg(chat_id, update_id, text="x"):
    return {"update_id": update_id,
            "message": {"message_id": update_id, "text": text,
                        "chat": {"id": chat_id, "type": "private"},
                        "from": {"id": chat_id, "username": f"u{chat_id}"}}}


SLEEP = 0.15


def _run(updates, workers=8):
    """מריץ את העדכונים דרך ה-pool ומחזיר (יומן, זמן קיר)."""
    log = []
    log_lock = threading.Lock()

    def fake_handle(update):
        cid = update["message"]["chat"]["id"]
        with log_lock:
            log.append(("start", cid, update["update_id"], time.monotonic()))
        time.sleep(SLEEP)
        with log_lock:
            log.append(("end", cid, update["update_id"], time.monotonic()))

    with patch.object(bc, "handle_update", fake_handle):
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for u in updates:
                pool.submit(bc._handle_update_serialized, u)
        return log, time.monotonic() - t0


# ---------------------------------------------------------------------
# 1. שיחות שונות מתעבדות במקביל
# ---------------------------------------------------------------------
log, elapsed = _run([_msg(cid, i) for i, cid in enumerate([101, 102, 103, 104])])
check("כל ארבעת העדכונים טופלו", len([e for e in log if e[0] == "end"]) == 4)
check("ארבע שיחות שונות רצו במקביל, לא בטור",
      elapsed < SLEEP * 2, f"-> {elapsed:.2f}s (בטור היה לוקח {SLEEP*4:.2f}s)")

# ---------------------------------------------------------------------
# 2. אותה שיחה — טורי, ובסדר
# ---------------------------------------------------------------------
log, elapsed = _run([_msg(500, i) for i in range(3)])
check("שלושת העדכונים של אותה שיחה טופלו", len([e for e in log if e[0] == "end"]) == 3)
check("ובטור — לא במקביל", elapsed >= SLEEP * 3 * 0.9,
      f"-> {elapsed:.2f}s (מקבילי היה ~{SLEEP:.2f}s)")

# אין חפיפה: כל start בא אחרי ה-end שלפניו
events = sorted(log, key=lambda e: e[3])
overlapping = False
depth = 0
for kind, _cid, _uid, _t in events:
    depth += 1 if kind == "start" else -1
    if depth > 1:
        overlapping = True
check("ושני handlers של אותה שיחה לא רצו בו-זמנית", not overlapping)

order = [uid for kind, _c, uid, _t in events if kind == "start"]
check("והסדר נשמר", order == [0, 1, 2], f"-> {order}")

# ---------------------------------------------------------------------
# 3. עדכון חריג לא מפיל את ה-thread
# ---------------------------------------------------------------------
def boom(update):
    raise RuntimeError("handler exploded")


with patch.object(bc, "handle_update", boom):
    crashed = False
    try:
        bc._handle_update_serialized(_msg(900, 1))
    except Exception:
        crashed = True
check("חריגה ב-handler נבלעת ונרשמת, לא מתפשטת ל-pool", not crashed)

# ---------------------------------------------------------------------
# 4. _chat_id_of מזהה את כל סוגי העדכונים
# ---------------------------------------------------------------------
check("message", bc._chat_id_of(_msg(7, 1)) == 7)
check("edited_message",
      bc._chat_id_of({"edited_message": {"chat": {"id": 8}}}) == 8)
check("callback_query",
      bc._chat_id_of({"callback_query": {"message": {"chat": {"id": 9}}}}) == 9)
check("עדכון בלי chat -> None, ולא חריגה",
      bc._chat_id_of({"update_id": 1, "poll": {}}) is None)

# ---------------------------------------------------------------------
# 5. אותו chat_id מקבל תמיד את אותו מנעול
# ---------------------------------------------------------------------
check("מנעול יציב לכל chat_id", bc._chat_lock(42) is bc._chat_lock(42))
check("ומנעולים שונים לצ'אטים שונים", bc._chat_lock(42) is not bc._chat_lock(43))

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
