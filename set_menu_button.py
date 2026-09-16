"""
SunSafe — הגדרת Menu Button של הבוט (setChatMenuButton)
--------------------------------------------------------
הכפתור בפינת הצ'אט, ליד תיבת ההקלדה, יכול להיות אחד משניים — לא שניהם:

  commands  (ברירת המחדל) — כפתור "/" שפותח את רשימת הפקודות הרשומות
            ב-setMyCommands. זה מה שאנחנו רוצים כברירת מחדל: ארבע
            הפקודות של set_bot_profile.py.
  web_app   — כפתור שפותח את ה-Mini App האופליין (docs/session/index.html).
            **הוא מחליף את תפריט הפקודות לגמרי.** המשתמש כבר לא רואה
            את "/" בפינה.

זו הגדרה ברמת הבוט (לא per-message), ו-setChatMenuButton הוא "set" ולא
"add" — כל הרצה דורסת את הקודמת.

2026-09-14: הסקריפט הזה הורץ בטעות על הבוט החדש (@SunSafeAppBot) במסגרת
ההעברה מ-@gil612Bot, והחליף את תפריט ארבע הפקודות בכפתור האופליין. מכאן
ההתנהגות הפוכה: **בלי ארגומנטים הוא מחזיר את תפריט הפקודות**, ומי שבאמת
רוצה את כפתור ה-Mini App צריך לבקש אותו במפורש.

הרצה:
    python set_menu_button.py            # תפריט הפקודות (ברירת מחדל)
    python set_menu_button.py --web-app  # כפתור ה-Mini App האופליין
    python set_menu_button.py --show     # רק מראה מה מוגדר כרגע, בלי לשנות

ראה docs/2026-08-29-offline-session-miniapp-design.md סעיף 10.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_MINIAPP_URL = os.environ.get("SESSION_MINIAPP_URL", "http://localhost:8080/session")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _call(method: str, payload: dict | None = None) -> dict:
    response = httpx.post(f"{TELEGRAM_API}/{method}", json=payload or {}, timeout=10.0)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"{method} נכשל: {result}")
    return result


def show() -> None:
    """מדפיס את ההגדרה הנוכחית — שימושי כדי לוודא שהחזרה באמת תפסה."""
    button = _call("getChatMenuButton")["result"]
    kind = button.get("type")
    if kind == "web_app":
        url = (button.get("web_app") or {}).get("url")
        print(f"כרגע מוגדר: כפתור Mini App -> {url}")
        print("תפריט הפקודות (\"/\") *לא* מוצג למשתמש.")
    elif kind == "commands":
        print("כרגע מוגדר: תפריט הפקודות (\"/\"). זו ברירת המחדל הרצויה.")
    else:
        print(f"כרגע מוגדר: {kind} — טלגרם מציג את תפריט הפקודות כברירת מחדל.")


def set_commands() -> None:
    _call("setChatMenuButton", {"menu_button": {"type": "commands"}})
    print("הוחזר: כפתור התפריט מציג שוב את רשימת הפקודות.")
    print("אם ארבע הפקודות לא מופיעות, הריצו קודם: python set_bot_profile.py")


def set_web_app() -> None:
    if not SESSION_MINIAPP_URL.startswith("https://"):
        print(
            f"SESSION_MINIAPP_URL אינו HTTPS ({SESSION_MINIAPP_URL!r}). טלגרם ידחה "
            "כתובת כזו, והכפתור בטלגרם ייפתח לשום מקום. הגדירו אותו ב-.env "
            "לכתובת ה-GitHub Pages האמיתית ונסו שוב."
        )
        return

    print(
        "שימו לב: זה מחליף את תפריט הפקודות (\"/\") בכפתור ה-Mini App. "
        "המשתמש לא יראה יותר את רשימת הפקודות בפינת הצ'אט."
    )
    _call("setChatMenuButton", {
        "menu_button": {
            "type": "web_app",
            "text": "SunSafe אופליין",
            "web_app": {"url": SESSION_MINIAPP_URL},
        }
    })
    print(f"הוגדר. Menu Button יפתח: {SESSION_MINIAPP_URL}")


def main() -> None:
    args = set(sys.argv[1:])
    unknown = args - {"--web-app", "--show"}
    if unknown:
        print(f"ארגומנט לא מוכר: {' '.join(sorted(unknown))}")
        print(__doc__)
        raise SystemExit(2)

    if "--show" in args:
        show()
        return

    if "--web-app" in args:
        set_web_app()
    else:
        set_commands()

    print()
    show()


if __name__ == "__main__":
    main()
