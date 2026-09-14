"""
SunSafe — קריאת הפרופיל הנוכחי של הבוט *ישירות מהשרת* של טלגרם
------------------------------------------------------------------
כלי אבחון: אחרי שהרצת set_bot_profile.py בהצלחה (כל הקריאות "ok"),
אבל באפליקציית טלגרם (Bot Info) עדיין רואים את הטקסט הישן — הסקריפט
הזה עוקף לגמרי את אפליקציית טלגרם וקורא את הערכים ישירות מה-Bot API
(getMyName / getMyShortDescription / getMyDescription), כדי להבדיל
בין שתי אפשרויות:
  1. העדכון באמת לא נקלט בשרת (למשל בגלל language_code סותר) — אז
     גם כאן יופיע הטקסט הישן.
  2. העדכון כן נקלט בשרת (כאן יופיע הטקסט החדש), וזה רק קאש בצד
     הלקוח של אפליקציית טלגרם — ידוע ש-Bot Info לפעמים לא מתרענן
     מיד; עוזר לסגור לגמרי את האפליקציה (לא רק למזער) ולפתוח מחדש,
     או לבדוק דרך Telegram Web/Desktop.

לא נוגע בכלום — רק GET, read-only, בטוח להרצה בכל שלב.

הרצה: python get_bot_profile.py
"""

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("sunsafe.get_bot_profile")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # לא לחשוף את ה-BOT_TOKEN בלוג ה-URL

BOT_TOKEN = os.environ["BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _get(client: httpx.Client, method: str) -> dict:
    response = client.get(f"{TELEGRAM_API}/{method}", timeout=10.0)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"{method} failed: {result}")
    return result["result"]


def main() -> None:
    with httpx.Client() as client:
        name = _get(client, "getMyName")
        short_desc = _get(client, "getMyShortDescription")
        desc = _get(client, "getMyDescription")

    print("מה שהשרת של טלגרם מחזיק כרגע בפועל (default, בלי language_code):\n")
    print(f"Name:\n  {name.get('name')!r}\n")
    print(f"Short description (זה שדה ה-'Bio' שרואים ב-Bot Info):\n  {short_desc.get('short_description')!r}\n")
    print(f"Description (העמוד המלא/About):\n  {desc.get('description')!r}\n")


if __name__ == "__main__":
    main()
