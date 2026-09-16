"""
SunSafe — שליחת הודעת ברכה חד-פעמית למשתמש בודד (למשל: ברכת ראש השנה)
------------------------------------------------------------------------
לא חלק מהבוט הרגיל (אין handler כמו "/greet") — סקריפט admin עצמאי
לשליחת הודעת טקסט חד-פעמית וחופשית למשתמש ספציפי לפי telegram_username.
שולח דרך אותה send_message() בדיוק שהבוט עצמו משתמש בה
(bot_commands.py) — טקסט רגיל (בלי Markdown), אין escaping נדרש.

ברירת מחדל = dry run: מדפיס למי תישלח ההודעה ומה הטקסט המדויק, בלי
לגעת בטלגרם בכלל. שליחה אמיתית רק עם --confirm מפורש.

    python send_greeting.py Chatgil_0              # dry run
    python send_greeting.py Chatgil_0 --confirm    # שולח בפועל הודעת טלגרם אמיתית
"""

import argparse
import sys

import bot_commands as bc
from supabase_client import SupabaseError, select_rows

GREETING_TEXT = (
    "שנה טובה ומתוקה מ-SunSafe! 🍯🍎\n\n"
    "שתהיה שנה מלאה בימי שמש נעימים — בלי כוויות ובלי דאגות. "
    "אנחנו כאן כל השנה שתמיד תדעו מתי להתגונן. ☀️"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("username", help="telegram_username לשליחה (עם או בלי @ בהתחלה)")
    parser.add_argument("--confirm", action="store_true", help="לשלוח בפועל (בלי זה — dry run בלבד)")
    args = parser.parse_args()

    username = args.username.lstrip("@").strip()

    try:
        users = select_rows("users", {"telegram_username": f"eq.{username}"})
    except SupabaseError as e:
        print(f"שגיאה בשליפת המשתמש: {e}", file=sys.stderr)
        sys.exit(1)

    if not users:
        print(f"לא נמצא משתמש {username!r} בטבלת users.", file=sys.stderr)
        sys.exit(1)

    chat_id = users[0].get("chat_id")
    if not chat_id:
        print(f"למשתמש {username!r} אין chat_id שמור — אי אפשר לשלוח לו הודעה.", file=sys.stderr)
        sys.exit(1)

    print(f"נמען: {username!r} (chat_id={chat_id})\n")
    print("הטקסט שיישלח:")
    print("-" * 40)
    print(GREETING_TEXT)
    print("-" * 40)

    if not args.confirm:
        print(f"\nDRY RUN — לא נשלח כלום. לשליחה בפועל: python send_greeting.py {username} --confirm")
        return

    bc.send_message(chat_id, GREETING_TEXT)
    print(f"\nנשלח בהצלחה ל-{username!r}.")


if __name__ == "__main__":
    main()
