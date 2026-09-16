"""
בדיקה ידנית (לא pytest) ל-/add_session — מריצים ישירות עם python, לא
נוגעים ברשת/DB אמיתיים: מדמים httpx.Client + select_rows/insert_row/
send_message. בודקים parsing, חציית חצות, גלגול שנה ל-date=, שליפת UV
היסטורי, ותרחיש קצה-לקצה מלא.
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


# ---------------------------------------------------------------------
# 1) _parse_kv_fields
# ---------------------------------------------------------------------
city_tokens, fields = bc._parse_kv_fields(
    "תל אביב start=14:00 end=16:30 spf=30".split(), {"start", "end", "spf", "date", "uv"}
)
check("parse: city", " ".join(city_tokens) == "תל אביב", f"-> {city_tokens}")
check("parse: fields", fields == {"start": "14:00", "end": "16:30", "spf": "30"}, f"-> {fields}")

city_tokens2, fields2 = bc._parse_kv_fields("New York start=09:00 end=11:00".split(), {"start", "end", "spf", "date", "uv"})
check("parse: multi-word city", " ".join(city_tokens2) == "New York", f"-> {city_tokens2}")

# ---------------------------------------------------------------------
# 2) weighted_average_uv + fetch_historical_uv (מדמים תגובת Open-Meteo) —
# תיקון "דגימת UV בודדת" מ-2026-09-08 (ראו bot_commands.py לרציונל המלא
# ולתקלה האמיתית ממצפה רמון): fetch_historical_uv עבר מהתאמה לשעה
# בודדת לממוצע-משוקלל-משך על פני כל טווח [start_time, end_time).
# ---------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.last_params = params
        return FakeResponse(self._payload)


# --- weighted_average_uv (פונקציה טהורה) ---
day = datetime(2026, 8, 26, tzinfo=timezone.utc)
hourly_times = [f"{day.date().isoformat()}T{h:02d}:00" for h in range(24)]
hourly_uvs = [0.0] * 24
hourly_uvs[14] = 6.4  # שיא בודד ב-14:00, כמו הבדיקה הישנה לשעה מדויקת

# session שלם בתוך שעה אחת -> מחזיר את ה-UV של אותה שעה בדיוק
uv_single_hour = bc.weighted_average_uv(
    hourly_times, hourly_uvs,
    day.replace(hour=14, minute=10), day.replace(hour=14, minute=40),
)
check("weighted_average_uv: session within a single hour -> exact match", uv_single_hour == 6.4, f"-> {uv_single_hour}")

# session שחוצה שתי שעות בחלקים שווים -> ממוצע פשוט
uv_two_hours = bc.weighted_average_uv(
    [f"{day.date().isoformat()}T10:00", f"{day.date().isoformat()}T11:00"], [2.0, 8.0],
    day.replace(hour=10, minute=45), day.replace(hour=11, minute=15),
)
check("weighted_average_uv: 15min in each of two buckets -> (2+8)/2=5.0", uv_two_hours == 5.0, f"-> {uv_two_hours}")

# תרחיש "מצפה רמון": session ארוך (11 שעות, מיושר לשעות עגולות) -> ממוצע
# פשוט על פני 11 הדליים בפועל, בלי קשר לדגימה הבודדת שהייתה נלקחת קודם
# (05:00, uv=0) שהחמיצה לגמרי את השיא בצהריים.
mitzpe_times = [f"{day.date().isoformat()}T{h:02d}:00" for h in range(4, 16)]
mitzpe_uvs = [9.9, 0, 0, 1, 3, 6, 8, 7, 5, 2, 0, 0]  # 04:00 מחוץ ל-session, לא אמור להיספר
expected_mitzpe_avg = sum(mitzpe_uvs[1:]) / len(mitzpe_uvs[1:])
uv_mitzpe = bc.weighted_average_uv(mitzpe_times, mitzpe_uvs, day.replace(hour=5), day.replace(hour=16))
check(
    "weighted_average_uv: 11h session averages the whole span, not the old single (near-zero) snapshot",
    uv_mitzpe is not None and abs(uv_mitzpe - expected_mitzpe_avg) < 1e-9 and uv_mitzpe > 2.9,
    f"-> {uv_mitzpe} vs expected {expected_mitzpe_avg}",
)

# bucket עם uv=None מדולג, לא נספר כאפס
uv_with_none = bc.weighted_average_uv(
    [f"{day.date().isoformat()}T09:00", f"{day.date().isoformat()}T10:00"], [None, 5.0],
    day.replace(hour=9, minute=30), day.replace(hour=10, minute=30),
)
check("weighted_average_uv: None bucket skipped, not treated as 0", uv_with_none == 5.0, f"-> {uv_with_none}")

# בלי שום חפיפה בין ה-session לבין ה-buckets -> None
uv_no_overlap = bc.weighted_average_uv(
    [f"{day.date().isoformat()}T09:00"], [5.0],
    day.replace(day=27, hour=9), day.replace(day=27, hour=10),
)
check("weighted_average_uv: no overlap at all -> None", uv_no_overlap is None, f"-> {uv_no_overlap}")

# --- fetch_historical_uv (עטיפת ה-HTTP + past_days) ---
today = datetime.now(timezone.utc).date()
target_dt = datetime(today.year, today.month, today.day, 14, 0, tzinfo=timezone.utc) - timedelta(days=2)
target_dt = target_dt.replace(hour=14)
hourly_payload = {
    "hourly": {
        "time": [f"{target_dt.date().isoformat()}T{h:02d}:00" for h in range(24)],
        "uv_index": [0.0] * 14 + [6.4] + [0.0] * 9,
    }
}
fake_client = FakeClient(hourly_payload)
uv = bc.fetch_historical_uv(fake_client, 32.08, 34.78, target_dt, target_dt + timedelta(minutes=30))
check("fetch_historical_uv: short session within the peak hour -> exact match", uv == 6.4, f"-> {uv}")
check("fetch_historical_uv: past_days computed from start_time", fake_client.last_params["past_days"] == 2, f"-> {fake_client.last_params}")

# session ארוך יותר שנכנס גם לשעה שקטה -> ממוצע-משוקלל, לא 6.4 מדויק
uv_long = bc.fetch_historical_uv(FakeClient(hourly_payload), 32.08, 34.78, target_dt, target_dt + timedelta(hours=2))
expected_long = (6.4 * 60 + 0.0 * 60) / 120  # שעה אחת ב-14:00 (6.4) + שעה אחת ב-15:00 (0.0)
check("fetch_historical_uv: multi-hour session -> weighted average, not just the start hour", uv_long is not None and abs(uv_long - expected_long) < 1e-9, f"-> {uv_long} vs {expected_long}")

# תאריך מעבר ל-92 יום -> None בלי לקרוא לרשת בכלל
far_dt = datetime(today.year, today.month, today.day, 14, 0, tzinfo=timezone.utc) - timedelta(days=200)
uv_far = bc.fetch_historical_uv(FakeClient(hourly_payload), 32.08, 34.78, far_dt, far_dt + timedelta(hours=1))
check("fetch_historical_uv: too far in past -> None", uv_far is None, f"-> {uv_far}")

# בלי שום חפיפה בין ה-session לנתונים שחזרו (למשל modeled data חסר לגמרי) -> None
missing_hour_payload = {"hourly": {"time": [f"{today.isoformat()}T{h:02d}:00" for h in range(24) if h != 14], "uv_index": [1.0] * 23}}
target_today_14 = datetime(today.year, today.month, today.day, 14, 0, tzinfo=timezone.utc)
uv_missing = bc.fetch_historical_uv(FakeClient(missing_hour_payload), 32.08, 34.78, target_today_14, target_today_14 + timedelta(minutes=1))
check("fetch_historical_uv: no matching bucket at all -> None", uv_missing is None, f"-> {uv_missing}")


# ---------------------------------------------------------------------
# 3) handle_add_session — תרחיש מלא, קלטים תקינים, uv= override
# ---------------------------------------------------------------------
sent_messages = []
inserted_rows = []


def fake_send_message(chat_id, text, reply_markup=None):
    sent_messages.append(text)


def fake_select_rows(table, params):
    assert table == "users"
    return [{"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}]


def fake_insert_row(table, row):
    inserted_rows.append((table, row))
    return row


def fake_geocode_city(client, city_name):
    return {"found": True, "name": "תל אביב", "country": "IL", "latitude": 32.08, "longitude": 34.78}


# offset=0 (UTC) לכל התרחישים הקיימים למטה -> ההתנהגות/הציפיות שלהם לא
# משתנות עם התיקון של 2026-09-08 (start=/end=HH:MM כזמן מקומי, ראו
# fetch_utc_offset_seconds ב-bot_commands.py) — תרחיש ה-offset הלא-אפס
# נבדק בנפרד למטה, section 4.
def fake_fetch_utc_offset_seconds_zero(client, lat, lon):
    return 0


# date= קבוע כמה ימים אחורה, משותף לכל התרחישים ש"בעבר" (לא היום) —
# כדי שהבדיקה לא תהיה תלויה בשעה בפועל שבה מריצים אותה (start=02:00
# למשל היה "בעתיד" אם מריצים את הבדיקה ב-00:30 UTC). ראו גם התיקון
# הזהה בבדיקת חציית-החצות למטה.
safe_past = datetime.now(timezone.utc) - timedelta(days=3)
safe_past_date_field = f"{safe_past.day}.{safe_past.month}"

with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", fake_select_rows), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "geocode_city", fake_geocode_city), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds_zero):

    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_add_session(123, "gil612", f"תל אביב date={safe_past_date_field} start=02:00 end=04:30 spf=30 uv=7")

    check("add_session: no usage error with uv= override", len(sent_messages) == 1 and "שימוש" not in sent_messages[0], f"-> {sent_messages}")
    check("add_session: inserted exactly one row", len(inserted_rows) == 1, f"-> {inserted_rows}")
    if inserted_rows:
        _, row = inserted_rows[0]
        check("add_session: uv_index from override", row["uv_index"] == 7.0, f"-> {row.get('uv_index')}")
        check("add_session: spf stored", row["spf"] == 30, f"-> {row.get('spf')}")
        expected_score = bc.calculate_exposure_score(7.0, 150, 3, 30)
        check("add_session: exposure_score matches formula", row["exposure_score"] == expected_score, f"-> {row.get('exposure_score')} vs {expected_score}")
        check("add_session: start_time hour", "T02:00" in row["start_time"], f"-> {row['start_time']}")
        check("add_session: end_time hour", "T04:30" in row["end_time"], f"-> {row['end_time']}")

    # חציית חצות: start=23:30 end=00:15 -> end ביום הבא. אותו safe_past_date_field
    # (כמה ימים אחורה) כדי שגם אחרי גלגול היום קדימה end_time עדיין בעבר.
    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_add_session(123, "gil612", f"תל אביב date={safe_past_date_field} start=23:30 end=00:15 uv=2")
    if inserted_rows:
        _, row = inserted_rows[0]
        start_dt = datetime.fromisoformat(row["start_time"])
        end_dt = datetime.fromisoformat(row["end_time"])
        check("add_session: midnight rollover -> end is next day", end_dt.date() == start_dt.date() + timedelta(days=1), f"-> start={start_dt} end={end_dt}")
        check("add_session: midnight rollover duration = 45min", (end_dt - start_dt).total_seconds() / 60 == 45, f"-> {(end_dt - start_dt).total_seconds() / 60}")

    # end בעתיד -> נדחה, בלי insert.
    #
    # ה"עכשיו" מוקפא כאן בכוונה (2026-09-12): הגרסה הקודמת בנתה את
    # השעות מ-datetime.now() + 5/6 שעות והעבירה רק %H:%M בלי date=,
    # כלומר אחרי ~18:00 UTC ה"עתיד" חצה חצות והתפרש כשעה *מוקדמת היום*
    # — שהיא בעבר. הבדיקה נכשלה בערבים והצליחה בבקרים, בלי שום קשר
    # לקוד. date= לא יכול לפתור את זה (הוא מגלגל שנה אחורה לתאריך
    # "עתידי", בכוונה), אז מקפיאים את השעון במקום.
    sent_messages.clear()
    inserted_rows.clear()

    class _FixedNow(datetime):
        """datetime אמיתי לכל דבר, רק עם now() קבוע — כדי ש-fromisoformat
        וחשבון התאריכים בתוך handle_add_session ימשיכו לעבוד כרגיל."""

        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 12, 8, 0, tzinfo=tz or timezone.utc)

    with patch.object(bc, "datetime", _FixedNow):
        bc.handle_add_session(123, "gil612", "תל אביב start=13:00 end=14:00 uv=3")
    check(
        "add_session: future end_time rejected",
        len(inserted_rows) == 0 and any("בעתיד" in m for m in sent_messages),
        f"-> {sent_messages}",
    )

    # חסר start/end -> הודעת שימוש, בלי insert
    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_add_session(123, "gil612", "תל אביב spf=30")
    check("add_session: missing start/end -> usage message", len(inserted_rows) == 0 and "שימוש" in sent_messages[0], f"-> {sent_messages}")

    # uv לא תקין -> הודעת שגיאה, בלי insert
    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_add_session(123, "gil612", f"תל אביב date={safe_past_date_field} start=02:00 end=03:00 uv=abc")
    check("add_session: invalid uv -> error, no insert", len(inserted_rows) == 0 and "uv חייב" in sent_messages[0], f"-> {sent_messages}")

    # date= עם גלגול שנה: date בעתיד ביחס להיום -> משנה קודמת
    sent_messages.clear()
    inserted_rows.clear()
    future_date_str = (datetime.now(timezone.utc) + timedelta(days=10))
    date_field = f"{future_date_str.day}.{future_date_str.month}"
    bc.handle_add_session(123, "gil612", f"תל אביב date={date_field} start=10:00 end=11:00 uv=4")
    if inserted_rows:
        _, row = inserted_rows[0]
        start_dt = datetime.fromisoformat(row["start_time"])
        check("add_session: date= year-rollback when date is in the future", start_dt.year == datetime.now(timezone.utc).year - 1, f"-> {start_dt}")
    else:
        check("add_session: date= year-rollback when date is in the future", False, f"-> no row inserted, messages={sent_messages}")

    # מ-2026-09-12 ההוספה עברה לדשבורד: הפקודה עדיין מוכרת לבוט, אבל
    # ממופה ל-handle_moved_to_dashboard (שמחזיר קישור לאזור האישי) במקום
    # לרשום session בעצמה. הלוגיקה של handle_add_session עצמה עדיין
    # נבדקת בכל שאר הקובץ — היא נשמרה כדי שהמעבר יהיה הפיך.
    check(
        "COMMAND_HANDLERS: /add_session now redirects to the dashboard",
        bc.COMMAND_HANDLERS.get("/add_session") is bc.handle_moved_to_dashboard,
    )


# ---------------------------------------------------------------------
# 4) start=/end=HH:MM כזמן *מקומי*, לא UTC — התיקון מ-2026-09-08 (ראו
# fetch_utc_offset_seconds ב-bot_commands.py לרציונל המלא/לתקלה
# האמיתית: "שעת הסיום לא יכולה להיות בעתיד" על end=11:00 שהתכוון
# ל-11:00 שעון ישראל, לא UTC). offset=10800 (UTC+3, כמו ישראל/IDT).
# ---------------------------------------------------------------------
OFFSET_IL = 10800  # +3h


def fake_fetch_utc_offset_seconds_il(client, lat, lon):
    return OFFSET_IL


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", fake_select_rows), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "geocode_city", fake_geocode_city), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds_il):

    # start=05:00/end=10:00 מקומי (ישראל) -> מאוחסן כ-UTC אמיתי: 02:00/07:00
    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_add_session(123, "gil612", f"תל אביב date={safe_past_date_field} start=05:00 end=10:00 uv=6")
    check("add_session (offset+3h): inserted exactly one row", len(inserted_rows) == 1, f"-> {inserted_rows}")
    if inserted_rows:
        _, row = inserted_rows[0]
        check("add_session (offset+3h): local 05:00 stored as UTC 02:00", "T02:00" in row["start_time"], f"-> {row['start_time']}")
        check("add_session (offset+3h): local 10:00 stored as UTC 07:00", "T07:00" in row["end_time"], f"-> {row['end_time']}")

    # חציית חצות *מקומית*: start=23:00 end=01:00 (ישראל) -> משך 2 שעות,
    # גם אם חציית חצות ה-UTC המתאימה נופלת ביום UTC שונה מחציית החצות
    # המקומית (20:00/22:00 UTC, אותו יום UTC בפועל -- ראו ניתוח בהודעת
    # התיקון: המרה עקבית לא תלויה באיזה "יום" חוצים, רק ביחס בין
    # start/end עצמם).
    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_add_session(123, "gil612", f"תל אביב date={safe_past_date_field} start=23:00 end=01:00 uv=2")
    check("add_session (offset+3h): midnight rollover -> inserted", len(inserted_rows) == 1, f"-> {inserted_rows}, messages={sent_messages}")
    if inserted_rows:
        _, row = inserted_rows[0]
        start_dt = datetime.fromisoformat(row["start_time"])
        end_dt = datetime.fromisoformat(row["end_time"])
        duration = (end_dt - start_dt).total_seconds() / 60
        check("add_session (offset+3h): local midnight rollover duration = 120min", duration == 120, f"-> {duration} (start={start_dt}, end={end_dt})")

    # רגרסיה ישירה לתקלה שדווחה בפועל: זמן מקומי שעבר לפני כמה דקות לא
    # אמור להידחות כ"עתידי" (התקלה הישנה השוותה UTC אמיתי מול המספרים
    # הגולמיים כאילו הם UTC). מדלגים קרוב מדי לחצות מקומית כדי לא להיתקל
    # בגלגול-תאריך לא-קשור בבדיקה עצמה.
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc + timedelta(seconds=OFFSET_IL)
    if 2 <= local_now.hour <= 21:
        end_local = (local_now - timedelta(minutes=5)).strftime("%H:%M")
        start_local = (local_now - timedelta(hours=1, minutes=5)).strftime("%H:%M")
        sent_messages.clear()
        inserted_rows.clear()
        bc.handle_add_session(123, "gil612", f"תל אביב start={start_local} end={end_local} uv=5")
        check(
            "add_session (offset+3h): local time 5min ago accepted, not misread as UTC-future",
            len(inserted_rows) == 1 and not any("בעתיד" in m for m in sent_messages),
            f"-> inserted={inserted_rows}, messages={sent_messages}",
        )
    else:
        check("add_session (offset+3h): local time 5min ago accepted, not misread as UTC-future", True, "skipped: too close to local midnight for a flake-free check")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
else:
    print("All checks passed.")
