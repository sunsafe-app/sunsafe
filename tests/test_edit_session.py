"""
בדיקה ידנית (לא pytest) ל-/edit_session — במיוחד לתיקון "זמן מקומי"
מ-2026-09-08 (ראו fetch_utc_offset_seconds/handle_add_session/
handle_edit_session ב-bot_commands.py לרציונל המלא ולתקלה האמיתית:
end=HH:MM התפרש כ-UTC גולמי בלי קשר למיקום בפועל של ה-session).

עד כה לא הייתה בדיקה ייעודית ל-/edit_session בכלל — קובץ חדש, לא
תוספת לקובץ קיים. מדמים send_message/select_rows/update_rows +
fetch_utc_offset_seconds (רשת) — לא נוגעים ברשת/DB אמיתיים.

תרחישי המפתח: end=HH:MM על session עם lat/lon שמור -> מתפרש כזמן מקומי
(ומומר ל-UTC לפני האחסון); session ישן בלי lat/lon (לפני ה-migration)
-> נופל בחזרה ל-UTC "כמו שהוא", fetch_utc_offset_seconds אפילו לא
נקראת; end=now לא מושפע מהתיקון בכלל (משתמש ב-datetime.now ישירות);
spf-only; חציית חצות מקומית; קלט לא תקין.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

# tests/ נמצא רמה אחת מתחת לשורש הריפו — מוסיפים את שורש הריפו ל-sys.path
# כדי ש-import bot_commands (ומודולים אחיים אחרים) ימשיך לעבוד גם כשמריצים
# מ-tests/ ולא משורש הריפו.
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bot_commands as bc

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


sent_messages = []
updated = []  # [(table, params, patch_dict)]
offset_calls = []  # [(lat, lon)]

OFFSET_IL = 10800  # +3h, כמו ישראל/IDT


def fake_send_message(chat_id, text, reply_markup=None):
    sent_messages.append(text)


def fake_update_rows(table, params, patch_dict):
    updated.append((table, params, patch_dict))
    return [patch_dict]


def fake_fetch_utc_offset_seconds(client, lat, lon):
    offset_calls.append((lat, lon))
    return OFFSET_IL


def make_fake_select_rows(session_row, users_row):
    def fake_select_rows(table, params):
        if table == "exposure_log":
            return [session_row] if session_row else []
        if table == "users":
            return [users_row] if users_row else []
        raise AssertionError(f"unexpected table in test: {table}")
    return fake_select_rows


START = datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)  # מיושר לשעה עגולה, נוח לבדיקות


def _session(id_=38, start=START, end=None, uv_index=6.0, spf=None, score=None, lat=30.61, lon=34.80, include_latlon=True):
    row = {
        "id": id_,
        "telegram_username": "gil612",
        "city": "מצפה רמון",
        "country": "IL",
        "start_time": start.isoformat(),
        "end_time": end.isoformat() if end else None,
        "uv_index": uv_index,
        "spf": spf,
        "exposure_score": score,
        "skin_type": 3,  # נשמר על ה-session מאז 2026-10-08 (ראו _begin_session)
    }
    if include_latlon:
        row["lat"] = lat
        row["lon"] = lon
    return row


# ---------------------------------------------------------------------
# 1) בלי מספר session -> הודעת שימוש
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message):
    sent_messages.clear()
    bc.handle_edit_session(123, "gil612", "")
    check("handle_edit_session: no session id -> usage message", "שימוש" in sent_messages[0], f"-> {sent_messages}")

# ---------------------------------------------------------------------
# 2) session לא קיים / לא שייך למשתמש -> הודעת שגיאה, בלי update
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(None, None)):
    sent_messages.clear()
    updated.clear()
    bc.handle_edit_session(123, "gil612", "38 end=now")
    check("handle_edit_session: unknown session id -> friendly message, no update", len(updated) == 0 and "לא נמצא" in sent_messages[0], f"-> {sent_messages}")

# ---------------------------------------------------------------------
# 3) end=now -> לא מושפע מהתיקון, ממשיך להשתמש ב-datetime.now(UTC) ישירות
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_session(), {"skin_type": 3})), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds):
    sent_messages.clear()
    updated.clear()
    offset_calls.clear()
    before = datetime.now(timezone.utc)
    bc.handle_edit_session(123, "gil612", "38 end=now")
    after = datetime.now(timezone.utc)
    check("handle_edit_session: end=now -> exactly one update", len(updated) == 1, f"-> {updated}")
    check("handle_edit_session: end=now -> does not call fetch_utc_offset_seconds at all", len(offset_calls) == 0, f"-> {offset_calls}")
    if updated:
        _, _, patch_dict = updated[0]
        end_dt = datetime.fromisoformat(patch_dict["end_time"])
        check("handle_edit_session: end=now -> stored end_time is really 'now' (UTC)", before <= end_dt <= after, f"-> {end_dt} not in [{before}, {after}]")

# ---------------------------------------------------------------------
# 4) end=HH:MM, session עם lat/lon שמור -> מתפרש כזמן *מקומי* (offset+3h),
#    מומר ל-UTC לפני האחסון. start=05:00 UTC (=08:00 מקומי) -> end=10:00
#    מקומי צריך להתפרש כ-07:00 UTC (לא 10:00 UTC).
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_session(start=START), {"skin_type": 3})), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds):
    sent_messages.clear()
    updated.clear()
    offset_calls.clear()
    bc.handle_edit_session(123, "gil612", "38 end=10:00")
    check("handle_edit_session: end=HH:MM with lat/lon -> calls fetch_utc_offset_seconds with the session's lat/lon", offset_calls == [(30.61, 34.80)], f"-> {offset_calls}")
    check("handle_edit_session: end=HH:MM with lat/lon -> exactly one update", len(updated) == 1, f"-> {updated}")
    if updated:
        _, _, patch_dict = updated[0]
        check("handle_edit_session: local 10:00 stored as UTC 07:00 (offset+3h)", "T07:00" in patch_dict["end_time"], f"-> {patch_dict.get('end_time')}")
        duration = (datetime.fromisoformat(patch_dict["end_time"]) - START).total_seconds() / 60
        check("handle_edit_session: exposure_score recomputed from the correct (offset-aware) duration", patch_dict.get("exposure_score") == bc.calculate_exposure_score(6.0, duration, 3, None), f"-> {patch_dict.get('exposure_score')} vs duration={duration}")

# ---------------------------------------------------------------------
# 5) session ישן בלי lat/lon בכלל (לפני ה-migration) -> נופל בחזרה ל-UTC
#    "כמו שהוא" (ההתנהגות הישנה) — fetch_utc_offset_seconds אפילו לא
#    נקראת, אין lat/lon לשלוח.
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_session(start=START, include_latlon=False), {"skin_type": 3})), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds):
    sent_messages.clear()
    updated.clear()
    offset_calls.clear()
    bc.handle_edit_session(123, "gil612", "38 end=10:00")
    check("handle_edit_session: no lat/lon on session -> fetch_utc_offset_seconds not called at all", len(offset_calls) == 0, f"-> {offset_calls}")
    if updated:
        _, _, patch_dict = updated[0]
        check("handle_edit_session: no lat/lon -> end=10:00 stored as literal UTC 10:00 (legacy behavior)", "T10:00" in patch_dict["end_time"], f"-> {patch_dict.get('end_time')}")

# ---------------------------------------------------------------------
# 6) חציית חצות *מקומית* (offset+3h): start=20:00 UTC (=23:00 מקומי)
#    end=01:00 מקומי -> "לפני" שעת ההתחלה המקומית -> יום נוסף, משך 2
#    שעות סה"כ (בדיוק כמו הבדיקה המקבילה ב-test_add_session_manual.py —
#    שם ה"מקומי" הוא מה שהמשתמש מקליד ישירות; כאן start_time כבר שמור
#    כ-UTC מראש, אז צריך להזין אותו כך ש-start+offset ייתן 23:00 מקומי).
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_session(start=START.replace(hour=20)), {"skin_type": 3})), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds):
    sent_messages.clear()
    updated.clear()
    bc.handle_edit_session(123, "gil612", "38 end=01:00")
    check("handle_edit_session: local midnight rollover -> exactly one update", len(updated) == 1, f"-> {updated}, messages={sent_messages}")
    if updated:
        _, _, patch_dict = updated[0]
        start_dt = START.replace(hour=20)
        end_dt = datetime.fromisoformat(patch_dict["end_time"])
        duration = (end_dt - start_dt).total_seconds() / 60
        check("handle_edit_session: local midnight rollover duration = 120min", duration == 120, f"-> {duration} (start={start_dt}, end={end_dt})")

# ---------------------------------------------------------------------
# 7) spf-only -> לא נוגע ב-end_time, לא קורא ל-fetch_utc_offset_seconds
#    בכלל (אין end בשדות).
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_session(), {"skin_type": 3})), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds):
    sent_messages.clear()
    updated.clear()
    offset_calls.clear()
    bc.handle_edit_session(123, "gil612", "38 spf=50")
    check("handle_edit_session: spf-only -> exactly one update, no end_time touched", len(updated) == 1 and "end_time" not in updated[0][2], f"-> {updated}")
    check("handle_edit_session: spf-only -> fetch_utc_offset_seconds not called", len(offset_calls) == 0, f"-> {offset_calls}")

# ---------------------------------------------------------------------
# 8) קלט זמן לא תקין -> הודעת שגיאה, בלי update
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_session(), {"skin_type": 3})), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds):
    sent_messages.clear()
    updated.clear()
    bc.handle_edit_session(123, "gil612", "38 end=25:99")
    check("handle_edit_session: invalid end time format -> error, no update", len(updated) == 0 and "פורמט שעה" in sent_messages[0], f"-> {sent_messages}")

# ---------------------------------------------------------------------
# 9) לא צוין end/spf בכלל -> הודעת שגיאה
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_session(), {"skin_type": 3})):
    sent_messages.clear()
    updated.clear()
    bc.handle_edit_session(123, "gil612", "38")
    check("handle_edit_session: no end/spf given -> usage-style error, no update", len(updated) == 0 and "לפחות" in sent_messages[0], f"-> {sent_messages}")

# ---------------------------------------------------------------------
# 10) COMMAND_HANDLERS: /edit_session מפנה לדשבורד
# ---------------------------------------------------------------------
# מ-2026-09-12 העריכה עברה לדשבורד: הפקודה עדיין מוכרת לבוט, אבל ממופה
# ל-handle_moved_to_dashboard (שמחזיר קישור לאזור האישי) במקום לערוך
# בעצמה. הלוגיקה של handle_edit_session עצמה עדיין נבדקת בכל שאר הקובץ —
# היא נשמרה כדי שהמעבר יהיה הפיך.
check(
    "COMMAND_HANDLERS: /edit_session now redirects to the dashboard",
    bc.COMMAND_HANDLERS.get("/edit_session") is bc.handle_moved_to_dashboard,
)


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
else:
    print("All checks passed.")
