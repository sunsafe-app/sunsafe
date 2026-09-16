"""
בדיקה ידנית (לא pytest) ל-/end_session — במיוחד לתיקון "דגימת UV
בודדת" מ-2026-09-08 (ראו weighted_average_uv / fetch_historical_uv /
handle_end_session ב-bot_commands.py לרציונל המלא, כולל התקלה האמיתית
ממצפה רמון: session ארוך שהוצג עם UV=0.0 בדשבורד כי הדגימה הבודדת
נפלה על שעת לילה). מדמים send_message/select_rows/update_rows/
fetch_historical_uv — לא נוגעים ברשת/DB אמיתיים.

תרחישי המפתח: session עם lat/lon שמור -> מנסים לרענן ל-UV משוקלל;
כשל ברענון (None או exception) -> נופלים בחזרה בבטחה לדגימה המקורית;
session ישן בלי lat/lon (לפני ה-migration) -> אפילו לא מנסים לרענן.
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
updated = []  # [(table, params, patch)]


def fake_send_message(chat_id, text, reply_markup=None):
    sent_messages.append(text)


def fake_update_rows(table, params, patch_dict):
    updated.append((table, params, patch_dict))
    return [patch_dict]


def make_fake_select_rows(open_session, users_row):
    def fake_select_rows(table, params):
        if table == "exposure_log":
            return [open_session] if open_session else []
        if table == "users":
            return [users_row] if users_row else []
        raise AssertionError(f"unexpected table in test: {table}")
    return fake_select_rows


START = datetime(2026, 9, 2, 5, 0, tzinfo=timezone.utc)  # מיושר לשעה עגולה, נוח לבדיקות


def _open_session(id_=1, uv_index=0.0, lat=30.61, lon=34.80, include_latlon=True):
    row = {
        "id": id_,
        "telegram_username": "gil612",
        "city": "מצפה רמון",
        "country": "IL",
        "start_time": START.isoformat(),
        "end_time": None,
        "uv_index": uv_index,  # הדגימה המקורית (הישנה) שנלקחה ב-_begin_session
        "spf": None,
        "exposure_score": None,
    }
    if include_latlon:
        row["lat"] = lat
        row["lon"] = lon
    return row


# ---------------------------------------------------------------------
# 1) אין session פתוח בכלל -> הודעה ידידותית, בלי update_rows
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(None, {"skin_type": 3})):
    sent_messages.clear()
    updated.clear()
    bc.handle_end_session(123, "gil612", "")
    check("handle_end_session: no open session -> friendly message, no update", len(updated) == 0 and "אין לך" in sent_messages[0], f"-> {sent_messages}")

# ---------------------------------------------------------------------
# 2) session עם lat/lon -> fetch_historical_uv מצליח -> uv_index מתעדכן
#    לערך המרוענן (לא נשאר על הדגימה הבודדת המקורית), וגם exposure_score
#    מחושב לפי הערך המרוענן.
# ---------------------------------------------------------------------
REFRESHED_UV = 6.5  # מדמה שיא אמיתי בצהריים, בניגוד לדגימה המקורית uv_index=0.0


def fake_fetch_historical_uv_success(client, lat, lon, start_time, end_time):
    return REFRESHED_UV


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_open_session(uv_index=0.0), {"skin_type": 3})), \
     patch.object(bc, "fetch_historical_uv", fake_fetch_historical_uv_success):
    sent_messages.clear()
    updated.clear()
    bc.handle_end_session(123, "gil612", "")
    check("handle_end_session: exactly one update_rows call", len(updated) == 1, f"-> {updated}")
    # הודעת הסיום היא הנקודה היחידה שבה המשתמש רואה את מדד החשיפה שלו,
    # ולכן גם הרגע הטבעי להפנות אותו לאזור האישי (2026-09-14). הפקודה
    # חייבת לשבת בשורה משלה — בטלגרם היא לינק לחיץ, וטקסט שנדבק אליה
    # באותה שורה נבלע בתוכו.
    completion = sent_messages[0] if sent_messages else ""
    check(
        "handle_end_session: completion message points the user to /dashboard, on its own line",
        "/dashboard" in [line.strip() for line in completion.split("\n")],
        f"-> {sent_messages}",
    )
    if updated:
        _, _, patch_dict = updated[0]
        check("handle_end_session: uv_index replaced with refreshed weighted-average value", patch_dict.get("uv_index") == REFRESHED_UV, f"-> {patch_dict}")
        check("handle_end_session: exposure_score computed from the refreshed UV, not the original 0.0", patch_dict.get("exposure_score", 0) > 0, f"-> {patch_dict}")

# ---------------------------------------------------------------------
# 3) session עם lat/lon -> fetch_historical_uv מחזיר None (לדוגמה: מעבר
#    ל-92 יום, או שאין שום bucket חופף) -> נופל בחזרה לדגימה המקורית,
#    לא קורס ולא משאיר uv_index=None בטעות.
# ---------------------------------------------------------------------
def fake_fetch_historical_uv_none(client, lat, lon, start_time, end_time):
    return None


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_open_session(uv_index=4.2), {"skin_type": 3})), \
     patch.object(bc, "fetch_historical_uv", fake_fetch_historical_uv_none):
    sent_messages.clear()
    updated.clear()
    bc.handle_end_session(123, "gil612", "")
    if updated:
        _, _, patch_dict = updated[0]
        check("handle_end_session: refresh returns None -> falls back to original snapshot (4.2)", patch_dict.get("uv_index") == 4.2, f"-> {patch_dict}")
    else:
        check("handle_end_session: refresh returns None -> falls back to original snapshot (4.2)", False, "no update_rows call at all")

# ---------------------------------------------------------------------
# 4) session עם lat/lon -> fetch_historical_uv זורק חריגה (רשת נפלה)
#    -> לא קורס, נופל בחזרה לדגימה המקורית, ועדיין שולח הודעת סיום
#    תקינה למשתמש.
# ---------------------------------------------------------------------
def fake_fetch_historical_uv_raises(client, lat, lon, start_time, end_time):
    raise RuntimeError("simulated network failure")


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_open_session(uv_index=3.3), {"skin_type": 3})), \
     patch.object(bc, "fetch_historical_uv", fake_fetch_historical_uv_raises):
    sent_messages.clear()
    updated.clear()
    try:
        bc.handle_end_session(123, "gil612", "")
        crashed = False
    except Exception as e:
        crashed = True
        crash_detail = str(e)
    check("handle_end_session: refresh raises -> does not crash /end_session", crashed is False, "" if not crashed else crash_detail)
    if not crashed:
        # לא בודקים מילה ספציפית בנוסח (הוא נערך ביד ב-2026-09-14) אלא את
        # מה שההודעה חייבת לשאת: העיר ומדד החשיפה.
        msg = sent_messages[0] if sent_messages else ""
        check(
            "handle_end_session: refresh raises -> still sends a completion message",
            len(sent_messages) == 1 and "מצפה רמון" in msg and "מדד חשיפה" in msg,
            f"-> {sent_messages}",
        )
        # הרענון נכשל, אז ה-UV שמוצג הוא הדגימה המקורית ולא ממוצע —
        # ההודעה חייבת לומר "UV" ולא "UV ממוצע", אחרת היא משקרת על
        # מקור המספר (2026-09-15).
        check(
            "handle_end_session: refresh failed -> the UV is not labelled as an average",
            "UV ממוצע" not in msg and "UV 3.3" in msg,
            f"-> {msg}",
        )
        if updated:
            _, _, patch_dict = updated[0]
            check("handle_end_session: refresh raises -> falls back to original snapshot (3.3)", patch_dict.get("uv_index") == 3.3, f"-> {patch_dict}")

# ---------------------------------------------------------------------
# 5) session ישן בלי lat/lon בכלל (לפני ה-migration) -> אפילו לא מנסים
#    לקרוא ל-fetch_historical_uv, משתמשים ישירות בדגימה המקורית.
# ---------------------------------------------------------------------
fetch_calls = []


def fake_fetch_historical_uv_tracking(client, lat, lon, start_time, end_time):
    fetch_calls.append((lat, lon, start_time, end_time))
    return 9.9  # אם בכלל נקרא, זה היה משנה את uv_index -> הבדיקה הייתה נכשלת למטה


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_open_session(uv_index=5.5, include_latlon=False), {"skin_type": 3})), \
     patch.object(bc, "fetch_historical_uv", fake_fetch_historical_uv_tracking):
    sent_messages.clear()
    updated.clear()
    fetch_calls.clear()
    bc.handle_end_session(123, "gil612", "")
    check("handle_end_session: no lat/lon on session -> fetch_historical_uv not called at all", len(fetch_calls) == 0, f"-> {fetch_calls}")
    if updated:
        _, _, patch_dict = updated[0]
        check("handle_end_session: no lat/lon -> keeps the original snapshot (5.5)", patch_dict.get("uv_index") == 5.5, f"-> {patch_dict}")

# ---------------------------------------------------------------------
# 6) SPF לא-מספרי -> הודעת שימוש, בלי update_rows
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "select_rows", make_fake_select_rows(_open_session(), {"skin_type": 3})):
    sent_messages.clear()
    updated.clear()
    bc.handle_end_session(123, "gil612", "abc")
    # ההודעה לא חייבת להכיל את המילה "שימוש" — היא חייבת להראות למשתמש
    # את שתי הצורות התקינות, כל אחת בשורה משלה (כמו שאר הודעות הבוט
    # שמציגות פקודה). "/end_session <מספר>" הוא בדיוק מה שהמשתמש פספס.
    usage = sent_messages[0] if sent_messages else ""
    usage_lines = [line.strip() for line in usage.split("\n")]
    check(
        "handle_end_session: non-numeric SPF -> usage message, no update",
        len(updated) == 0
        and "/end_session" in usage_lines
        and any(line.startswith("/end_session ") and line.split()[-1].isdigit() for line in usage_lines),
        f"-> {sent_messages}",
    )

# ---------------------------------------------------------------------
# 7) COMMAND_HANDLERS: /end_session רשום נכון
# ---------------------------------------------------------------------
check("COMMAND_HANDLERS: /end_session registered", bc.COMMAND_HANDLERS.get("/end_session") is bc.handle_end_session)


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
else:
    print("All checks passed.")
