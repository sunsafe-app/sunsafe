"""
SunSafe — סגירת session פתוח עבור משתמש, "מהצד שלו" (מנהל/מפתח, לא המשתמש עצמו)
--------------------------------------------------------------------------------
מיועד למצב שבו משתמש התחיל session (/start_session) אבל לא שלח
/end_session בעצמו (שכח, נתקע, וכו'). הסקריפט הזה סוגר את ה-session
הפתוח שלו *באותו נתיב קוד בדיוק* כמו /end_session רגיל — קורא ישירות
ל-handle_end_session() ב-bot_commands.py. הוא לא מדמה טקסט נכנס ולא
מזייף שום נתון: משך הזמן, ה-UV (כולל רענון לממוצע-משוקלל אם יש lat/lon
שמורים) והציון מחושבים מהנתונים האמיתיים שכבר שמורים ב-exposure_log —
בדיוק כמו שהיה קורה אילו המשתמש עצמו היה שולח /end_session ברגע הזה.

**חשוב — זה שולח הודעת טלגרם אמיתית**: הרצה עם --confirm מעדכנת את
ה-DB *וגם* שולחת למשתמש הודעה אמיתית בטלגרם (אותו טקסט בדיוק שהיה
נשלח אילו הוא סגר את זה בעצמו), למשל:
    "session הסתיים — 21 דקות בקריית ים. מדד חשיפה: 104%."
בלי --confirm — dry run בלבד: מראה איזה session פתוח נמצא ואת המשך
הגס עד עכשיו, בלי לגעת ב-DB ובלי לשלוח שום דבר. ודאו שאתם רוצים
שהמשתמש יקבל את ההודעה הזו *עכשיו* (עם משך הזמן שנצבר עד רגע ההרצה)
לפני שמריצים עם --confirm.

    python end_session_for_user.py HRazHad              # dry run
    python end_session_for_user.py HRazHad --confirm    # סוגר בפועל + שולח הודעה אמיתית למשתמש
    python end_session_for_user.py HRazHad --confirm --spf 30   # עם SPF, בדיוק כמו /end_session 30
"""

import argparse
import sys
from datetime import datetime, timezone

import bot_commands as bc
from supabase_client import SupabaseError, select_rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("username", help="telegram_username לסגירה (עם או בלי @ בהתחלה)")
    parser.add_argument("--spf", default="", help="SPF כמספר, אופציונלי — כמו הארגומנט ל-/end_session <SPF>")
    parser.add_argument("--confirm", action="store_true", help="לסגור בפועל ולשלוח הודעה אמיתית (בלי זה — dry run בלבד)")
    args = parser.parse_args()

    username = args.username.lstrip("@").strip()
    if args.spf and not args.spf.isdigit():
        print("--spf חייב להיות מספר (כמו הארגומנט ל-/end_session).", file=sys.stderr)
        sys.exit(1)

    try:
        users = select_rows("users", {"telegram_username": f"eq.{username}"})
    except SupabaseError as e:
        print(f"שגיאה בשליפת המשתמש: {e}", file=sys.stderr)
        sys.exit(1)

    if not users:
        print(f"לא נמצא משתמש {username!r} בטבלת users.", file=sys.stderr)
        sys.exit(1)

    chat_id = users[0].get("chat_id")
    print(f"נמצא משתמש {username!r} עם chat_id={chat_id}")
    if not chat_id:
        print(
            f"למשתמש {username!r} אין chat_id שמור (עוד לא שלח אף הודעה לבוט) — "
            f"אי אפשר לשלוח לו הודעה.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        open_sessions = select_rows(
            "exposure_log", {"telegram_username": f"eq.{username}", "end_time": "is.null"}
        )
    except SupabaseError as e:
        print(f"שגיאה בשליפת ה-session: {e}", file=sys.stderr)
        sys.exit(1)

    if not open_sessions:
        print(f"אין ל-{username!r} session פתוח כרגע — אין מה לסגור.")
        return

    session = open_sessions[0]
    start_time = datetime.fromisoformat(session["start_time"])
    now = datetime.now(timezone.utc)
    duration_minutes = round((now - start_time).total_seconds() / 60)

    print(f"session פתוח נמצא: id={session['id']}, עיר={session['city']}, התחיל ב-{session['start_time']}")
    print(f"משך נוכחי (אם ייסגר עכשיו): כ-{duration_minutes} דקות")
    print(f"chat_id שמור למשתמש: {chat_id}")

    if not args.confirm:
        print(
            "\nDRY RUN — שום דבר לא נשמר ולא נשלח. הציון המדויק (כולל רענון UV משוקלל, "
            "אם יש lat/lon שמורים) מחושב בפועל רק בתוך handle_end_session.\n"
            f"להרצה אמיתית (סוגר את ה-session + שולח הודעת טלגרם אמיתית למשתמש): "
            f"python end_session_for_user.py {username} --confirm"
        )
        return

    print(f"\n--confirm הועבר — סוגר את ה-session ושולח הודעה אמיתית ל-chat_id={chat_id}...\n")
    bc.handle_end_session(chat_id, username, args.spf)
    print("בוצע — ה-session נסגר וההודעה נשלחה (ראו לוג/קונסולה של handle_end_session למעלה).")


if __name__ == "__main__":
    main()
