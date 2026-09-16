"""
SunSafe — הצגת המשתמשים הרשומים
---------------------------------
כלי אבחון בלבד — read-only, לא נוגע ב-DB ולא שולח שום הודעה.

מציג את טבלת users: מי עבר onboarding (skin_type הוא NOT NULL, אז שורה
קיימת = המשתמש הגדיר סוג עור), האם יש לו chat_id שמור (בלי זה הבוט לא
יכול ליזום אליו הודעה — למשל סגירה אוטומטית בשקיעה), וכמה sessions הוא
רשם. שימושי כשבודקים אימוץ אחרי שמזמינים צוות לבחון את הבוט.

ה-chat_id מוצג כי הוא נדרש לשני דברים מעשיים: ADMIN_CHAT_ID ב-.env,
ו-set_menu_button.py --chat <id> לבדיקת override של כפתור התפריט.

הרצה:
    python list_users.py
    python list_users.py <telegram_username>   # רק משתמש אחד
"""

import sys
from collections import Counter

from supabase_client import SupabaseError, select_rows


def main() -> None:
    wanted = sys.argv[1].lstrip("@") if len(sys.argv) > 1 else None

    params = {"order": "telegram_username.asc"}
    if wanted:
        params["telegram_username"] = f"eq.{wanted}"

    try:
        users = select_rows("users", params)
        sessions = select_rows("exposure_log", {"select": "telegram_username,end_time"})
    except SupabaseError as e:
        print(f"שגיאה בשליפה מ-Supabase: {e}")
        return

    if not users:
        print(f"לא נמצא משתמש {wanted!r}." if wanted else "טבלת users ריקה.")
        return

    total = Counter(s.get("telegram_username") for s in sessions)
    open_now = Counter(s.get("telegram_username") for s in sessions if not s.get("end_time"))

    print(f"{len(users)} משתמש(ים):\n")
    for u in users:
        name = u.get("telegram_username")
        chat_id = u.get("chat_id")
        # chat_id חסר קורה בשורות מלפני ה-migration שהוסיף אותו: המשתמש
        # הגדיר סוג עור פעם, ומאז לא נגע בבוט. אין לבוט דרך ליזום אליו.
        chat_part = str(chat_id) if chat_id else "— אין! הבוט לא יכול ליזום אליו הודעה"
        line = (
            f"- @{name}  סוג עור={u.get('skin_type')}  chat_id={chat_part}  "
            f"sessions={total.get(name, 0)}"
        )
        if open_now.get(name):
            line += f"  (מהם {open_now[name]} פתוחים כרגע)"
        print(line)

    if not wanted:
        registered = {u.get("telegram_username") for u in users}
        orphans = sorted(set(total) - registered - {None})
        if orphans:
            # sessions בלי שורת users: אפשרי אם שורת המשתמש נמחקה ידנית.
            print(f"\nיש sessions למשתמשים שאין להם שורה ב-users: {', '.join(orphans)}")


if __name__ == "__main__":
    main()
