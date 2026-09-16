"""
SunSafe — הצגת כל ה-sessions הפתוחים כרגע (end_time = NULL)
---------------------------------------------------------------
כלי אבחון בלבד — read-only, לא נוגע ב-DB ולא שולח שום הודעה.

שימושי כשרואים ב-Dashboard/בלוגים ש"יש session שרץ X שעות" אבל לא
יודעים איזה משתמש זה — מציג את *כל* השורות הפתוחות ב-exposure_log
(לכל המשתמשים), כולל כמה זמן כל אחת כבר פתוחה, כדי לזהות מי צריך
טיפול (למשל עם end_session_for_user.py).

הרצה: python list_open_sessions.py
"""

from datetime import datetime, timezone

from supabase_client import SupabaseError, select_rows


def main() -> None:
    try:
        open_sessions = select_rows("exposure_log", {"end_time": "is.null", "order": "start_time.asc"})
    except SupabaseError as e:
        print(f"שגיאה בשליפת sessions פתוחים: {e}")
        return

    if not open_sessions:
        print("אין כרגע אף session פתוח (end_time=NULL) בטבלת exposure_log.")
        return

    now = datetime.now(timezone.utc)
    print(f"נמצאו {len(open_sessions)} session(s) פתוחים:\n")

    for s in open_sessions:
        start_time = datetime.fromisoformat(s["start_time"])
        duration_minutes = round((now - start_time).total_seconds() / 60)
        hours = duration_minutes // 60
        minutes = duration_minutes % 60
        lat = s.get("lat")
        lon = s.get("lon")
        print(
            f"- id={s.get('id')}  משתמש={s.get('telegram_username')!r}  "
            f"עיר={s.get('city')!r}  התחיל={s.get('start_time')}  "
            f"פתוח כבר: {hours} שעות ו-{minutes} דקות  "
            f"(lat/lon: {lat}, {lon})"
        )

    print(
        "\nלסגירה מנהלתית של session ספציפי (כמו /end_session אמיתי, בלי לזייף נתונים):\n"
        "    python end_session_for_user.py <telegram_username>              # dry run\n"
        "    python end_session_for_user.py <telegram_username> --confirm    # סוגר בפועל + שולח הודעה אמיתית"
    )


if __name__ == "__main__":
    main()
