"""
SunSafe — אבחון: האם טלגרם בכלל שולח לבוט callback_query?
------------------------------------------------------------------
סקריפט קריאה-בלבד (לא משנה שום דבר, לא שולח הודעות) שנועד לענות על
שאלה אחת: כשלוחצים על כפתור inline ושום דבר לא קורה — האם העדכון בכלל
מגיע לבוט?

למה זה בכלל שאלה: ל-getUpdates יש פרמטר allowed_updates, ולטלגרם יש
זיכרון לגביו — "If not specified, the previous setting will be used".
כלומר אם *פעם אחת* בעבר מישהו קרא ל-getUpdates או ל-setWebhook עם
רשימה מוגבלת שלא כללה callback_query, ההגדרה הזו נשארת בצד של טלגרם
גם אחרי שהקוד שלנו השתנה — ולחיצות על כפתורים פשוט לא יגיעו, בלי
שום שגיאה ובלי שום שורה בלוג. בדיוק "שום דבר לא קורה".

    python check_bot_updates.py

מריצים מהתיקייה של הפרויקט (צריך BOT_TOKEN ב-.env).
"""

import json
import logging
import os

import httpx
from dotenv import load_dotenv

# אותה זהירות כמו בשאר הפרויקט: httpx רושם את ה-URL המלא ברמת INFO,
# וה-URL כולל את ה-BOT_TOKEN עצמו.
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ALL_WANTED = ["message", "edited_message", "callback_query"]


def main() -> None:
    with httpx.Client() as client:
        info = client.get(f"{TELEGRAM_API}/getWebhookInfo", timeout=10.0).json()

    if not info.get("ok"):
        print(f"getWebhookInfo נכשל: {info}")
        return

    result = info["result"]
    url = result.get("url") or ""
    allowed = result.get("allowed_updates")
    pending = result.get("pending_update_count", 0)

    print("=" * 60)
    print(f"webhook url:            {url or '(ריק — polling, כמצופה)'}")
    print(f"pending_update_count:   {pending}")
    print(f"allowed_updates:        {allowed if allowed is not None else '(לא הוגדר — ברירת מחדל)'}")
    print("=" * 60)
    print()

    if url:
        print(
            "⚠️  יש webhook מוגדר! כל עוד הוא קיים, getUpdates לא יקבל כלום\n"
            "    (טלגרם מחזיר 409 Conflict). זה היה מסביר שהבוט לא מגיב בכלל."
        )
        return

    if allowed is None:
        print(
            "✅ allowed_updates לא מוגדר, כלומר ברירת המחדל של טלגרם — והיא\n"
            "   *כוללת* callback_query. אם לחיצה על כפתור לא עושה כלום, הבעיה\n"
            "   לא כאן: או שה-Space לא רץ/ישן באותו רגע, או שהקוד שנפרס שם\n"
            "   עדיין לא כולל את handle_callback_query."
        )
    elif "callback_query" not in allowed:
        print(
            "❌ מצאנו את הבעיה: callback_query *לא* ברשימת allowed_updates,\n"
            f"   אז טלגרם בכלל לא שולח לחיצות על כפתורים (הרשימה: {allowed}).\n"
            "   זו הגדרה ששמורה בצד של טלגרם, לא בקוד שלנו.\n\n"
            "   לתיקון, הריצו את הסקריפט הזה עם --fix (הוא רק מרחיב את\n"
            "   הרשימה, לא נוגע בשום דבר אחר):\n"
            "       python check_bot_updates.py --fix"
        )
    else:
        print(
            f"✅ callback_query כן ברשימה ({allowed}) — לחיצות אמורות להגיע.\n"
            "   אם הן לא מטופלות, הבעיה בקוד שרץ ב-Space או שה-Space לא ער."
        )

    if pending:
        print(
            f"\nℹ️  יש {pending} עדכונים ממתינים בתור של טלגרם — סימן שהבוט לא\n"
            "    שולף אותם כרגע (Space ישן/מושבת). הם יטופלו כשהוא יתעורר."
        )


def fix() -> None:
    """
    קובע במפורש allowed_updates שכולל callback_query. עושה זאת דרך
    getUpdates עם limit=1 — קריאה זולה שכל תפקידה כאן הוא *לעדכן את
    ההגדרה* בצד של טלגרם (זו הדרך היחידה לשנות אותה בלי webhook).

    שימו לב: הקריאה הזו לא מוחקת עדכונים ולא מזיזה את ה-offset (אין
    offset בבקשה), אז שום הודעה ממתינה לא תיעלם.
    """
    with httpx.Client() as client:
        response = client.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"limit": 1, "timeout": 0, "allowed_updates": json.dumps(ALL_WANTED)},
            timeout=15.0,
        )
        data = response.json()

    if not data.get("ok"):
        print(f"הקריאה נכשלה: {data}")
        return

    with httpx.Client() as client:
        info = client.get(f"{TELEGRAM_API}/getWebhookInfo", timeout=10.0).json()
    allowed = info.get("result", {}).get("allowed_updates")
    print(f"עודכן. allowed_updates עכשיו: {allowed if allowed is not None else '(ברירת מחדל)'}")
    print("אם callback_query מופיע שם (או שההגדרה חזרה לברירת מחדל) — נסו שוב ללחוץ על כפתור.")


if __name__ == "__main__":
    import sys

    if "--fix" in sys.argv:
        fix()
    else:
        main()
