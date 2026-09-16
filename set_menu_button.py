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

setChatMenuButton הוא "set" ולא "add" — כל הרצה דורסת את הקודמת.

**שתי רמות, וזו הייתה הבעיה.** ל-setChatMenuButton ול-getChatMenuButton
יש פרמטר chat_id אופציונלי:

  בלי chat_id  — ברירת המחדל של הבוט, לכל הצ'אטים.
  עם chat_id   — override לצ'אט פרטי בודד, ש*דורס* את ברירת המחדל שם.

ולכן הרצה בלי chat_id יכולה לדווח "הוחזר בהצלחה", ו-getChatMenuButton
בלי chat_id יאשר שברירת המחדל היא commands — ובצ'אט שלכם הכפתור יישאר
ה-Mini App, כי ה-override הפרטי לא נגע. זה מה שקרה ב-14.9.2026: החזרה
נראתה מוצלחת ו"כלום לא השתנה".

2026-09-14: הסקריפט הזה הורץ בטעות על הבוט החדש (@SunSafeAppBot) במסגרת
ההעברה מ-@gil612Bot, והחליף את תפריט ארבע הפקודות בכפתור האופליין. מכאן
ההתנהגות הפוכה: **בלי ארגומנטים הוא מחזיר את תפריט הפקודות**, ומי שבאמת
רוצה את כפתור ה-Mini App צריך לבקש אותו במפורש.

הרצה:
    python set_menu_button.py --show            # מה מוגדר כרגע, בלי לשנות
    python set_menu_button.py                   # תפריט הפקודות (ברירת מחדל)
    python set_menu_button.py --chat 12345      # ניקוי ה-override של צ'אט בודד
    python set_menu_button.py --web-app         # כפתור ה-Mini App האופליין

--show מדפיס גם את שם הבוט מ-getMe, כדי שלא נגלה בדיעבד ששינינו את
הבוט הלא נכון: ה-BOT_TOKEN נלקח מ-.env המקומי, לא מסודות ה-Space.
ואם ADMIN_CHAT_ID מוגדר, הוא נבדק אוטומטית גם ברמת הצ'אט.

ראה docs/2026-08-29-offline-session-miniapp-design.md סעיף 10.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_MINIAPP_URL = os.environ.get("SESSION_MINIAPP_URL", "http://localhost:8080/session")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _call(method: str, payload: dict | None = None) -> dict:
    response = httpx.post(f"{TELEGRAM_API}/{method}", json=payload or {}, timeout=10.0)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"{method} נכשל: {result}")
    return result


def _describe(button: dict) -> str:
    kind = button.get("type")
    if kind == "web_app":
        url = (button.get("web_app") or {}).get("url")
        return f'כפתור Mini App -> {url}  (תפריט הפקודות "/" לא מוצג)'
    if kind == "commands":
        return 'תפריט הפקודות ("/") — זו ברירת המחדל הרצויה'
    if kind == "default":
        return "default — יורש את ברירת המחדל של הבוט"
    return f"{kind}"


def show(chat_id: str | None = None) -> None:
    """מדפיס את ההגדרה הנוכחית — שימושי כדי לוודא שהחזרה באמת תפסה."""
    me = _call("getMe")["result"]
    print(f'הבוט: @{me.get("username")} ({me.get("first_name")})')

    default = _call("getChatMenuButton")["result"]
    print(f"ברירת המחדל של הבוט: {_describe(default)}")

    target = chat_id or ADMIN_CHAT_ID
    if not target:
        print()
        print("לא נבדק שום צ'אט ספציפית. אם הכפתור עדיין לא מה שאתם רואים,")
        print("הריצו --show עם --chat <id> — ייתכן שיש override על הצ'אט שלכם.")
        return

    source = "מ--chat" if chat_id else "מ-ADMIN_CHAT_ID"
    per_chat = _call("getChatMenuButton", {"chat_id": int(target)})["result"]
    print(f"בצ'אט {target} ({source}): {_describe(per_chat)}")

    # type="default" פירושו "לא הוגדר ערך ספציפי לצ'אט הזה" — כלומר *אין*
    # override, והצ'אט יורש את ברירת המחדל. רק ערך אחר הוא override אמיתי.
    # (ההשוואה הראשונה כאן הייתה per_chat != default, וזה דיווח על override
    # דווקא במצב הנקי. 16.9.2026.)
    if per_chat.get("type") in (None, "default"):
        print("כלומר: אין override. מה שמוצג בצ'אט הזה הוא ברירת המחדל שלמעלה.")
        if default.get("type") == "commands":
            print()
            print("אם למרות זאת אתם רואים את כפתור ה-Mini App בפינת הצ'אט —")
            print("זה מטמון של לקוח טלגרם, לא הגדרה. סגרו והפעילו מחדש את")
            print("האפליקציה עצמה (לא רק את הצ'אט).")
    else:
        print()
        print("** יש override על הצ'אט הזה, והוא דורס את ברירת המחדל. **")
        print(f"כדי לנקות אותו:  python set_menu_button.py --chat {target}")


def set_commands() -> None:
    _call("setChatMenuButton", {"menu_button": {"type": "commands"}})
    print("הוחזר: ברירת המחדל של הבוט מציגה שוב את רשימת הפקודות.")
    print("אם ארבע הפקודות לא מופיעות, הריצו קודם: python set_bot_profile.py")


def clear_chat_override(chat_id: str) -> None:
    """
    מנקה override של צ'אט בודד. type=default פירושו "לא הוגדר ערך ספציפי",
    כלומר הצ'אט חוזר לירוש את ברירת המחדל של הבוט — זה *לא* אותו דבר
    כמו להגדיר commands על הצ'אט, שהיה משאיר override נוסף במקום.
    """
    _call("setChatMenuButton", {"chat_id": int(chat_id), "menu_button": {"type": "default"}})
    print(f"ה-override של צ'אט {chat_id} נוקה — הוא יורש עכשיו את ברירת המחדל של הבוט.")
    print("הערה: לקוחות טלגרם שומרים את הכפתור במטמון. אם הוא לא מתחלף")
    print("מיד, סגרו ופתחו את הצ'אט, או הפעילו מחדש את האפליקציה.")


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


def _parse_args(argv: list[str]) -> tuple[set[str], str | None]:
    flags: set[str] = set()
    chat_id: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--chat":
            if i + 1 >= len(argv):
                print("--chat דורש מספר צ'אט אחריו.")
                raise SystemExit(2)
            chat_id = argv[i + 1]
            if not chat_id.lstrip("-").isdigit():
                print(f"--chat מצפה למספר, לא {chat_id!r}.")
                raise SystemExit(2)
            i += 2
            continue
        if arg in {"--web-app", "--show"}:
            flags.add(arg)
            i += 1
            continue
        print(f"ארגומנט לא מוכר: {arg}")
        print(__doc__)
        raise SystemExit(2)
    return flags, chat_id


def main() -> None:
    flags, chat_id = _parse_args(sys.argv[1:])

    if "--show" in flags:
        show(chat_id)
        return

    if "--web-app" in flags:
        set_web_app()
    elif chat_id:
        clear_chat_override(chat_id)
    else:
        set_commands()

    print()
    show(chat_id)


if __name__ == "__main__":
    main()
