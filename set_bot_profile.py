"""
SunSafe — עדכון פרופיל הבוט בטלגרם (Name / About / Description / Commands)
--------------------------------------------------------------------------
כרגע הבוט מוצג עם Name="Gil_token" (שם dev פנימי, לא ידידותי למשתמש)
ובלי Commands רשומים בכלל ("no commands yet" ב-/mybots) — כלומר מי
שמקליד "/" בצ'אט לא מקבל תפריט הצעות פקודות. הסקריפט הזה מתקן את שני
אלה + מוסיף About/Description קריאים, דרך ה-Bot API (לא צריך BotFather
ידני בכלל לחלקים האלה):
    setMyName, setMyShortDescription, setMyDescription, setMyCommands

**מה שהסקריפט הזה לא יכול לעשות** (אין להם endpoint ב-Bot API בכלל,
רק דרך BotFather ידנית): תמונת פרופיל (Botpic), תמונת Description,
Privacy Policy. ראו את ההודעה שהסקריפט מדפיס בסוף להוראות ידניות.

הרצה: python set_bot_profile.py   (חד-פעמי; אפשר להריץ שוב בבטחה בכל
עדכון עתידי לתוכן/לרשימת הפקודות — כל הקריאות הן "set", לא "add").
"""

import logging
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("sunsafe.set_bot_profile")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# httpx רושם ללוג את ה-URL המלא של כל בקשה ברמת INFO כברירת מחדל, וה-URL
# למטה כולל את ה-BOT_TOKEN עצמו — בדיוק מה שקרה בהרצה הקודמת (הטוקן
# המלא הופיע במסוף, בטקסט גלוי). בלי השורה הזו זה יקרה בכל הרצה.
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ["BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# --- תוכן, בעברית (כמו שאר הבוט) -------------------------------------
NAME = "SunSafe – עוזר הגנה מהשמש ☀️"

SHORT_DESCRIPTION = (
    "🌞 עוקב אחרי חשיפה שלכם ל-UV לאורך היום ומזכיר מתי להתגונן. "
    "שלחו /start כדי להתחיל."
)

DESCRIPTION = (
    "☀️ SunSafe עוזר לכם לעקוב אחרי חשיפה יומית לקרינת UV ולהימנע "
    "משיזוף-יתר וכוויות שמש.\n\n"
    "איך מתחילים: /start — היכרות קצרה + הגדרת סוג עור (סולם Fitzpatrick), "
    "לפני כל שאר הפקודות.\n\n"
    "מה עוד הבוט עושה:\n"
    "• /start_session — מתחיל מעקב (שיתוף מיקום או שם עיר), כולל תחזית UV\n"
    "• /end_session — מסיים ומחשב מדד חשיפה אישי (לפי סוג העור וקרם הגנה)\n"
    "• /dashboard — אזור אישי: גרפים, היסטוריה, והוספה/עריכה של sessions"
)

# סדר = סדר ההופעה בתפריט "/" בטלגרם. שם פקודה חייב: אותיות קטנות/
# ספרות/קו תחתון בלבד, 1-32 תווים (מגבלת Bot API) — תואם ל-COMMAND_HANDLERS
# הקיים ב-bot_commands.py.
#
# 2026-09-12: add_session / edit_session / delete_session ירדו מהתפריט —
# ההוספה, העריכה והמחיקה עברו לדשבורד (/dashboard).
#
# 2026-09-13: התפריט צומצם לארבע פקודות בלבד — מסלול החיים של המשתמש
# ותו לא: התחלה, פתיחת מעקב, סגירתו, והאזור האישי. רשימה של תשע
# פקודות היא בדיוק מה שמרתיע משתמש לא טכני, וזה הכיוון שאליו הולך כל
# הממשק ממילא (כפתורים במקום הקלדה).
#
# חשוב: הסרה מהתפריט היא *לא* הסרה מהבוט. /today, /my_sessions,
# /diagnose_skin, /offline_session ו-/set_skin_type ממשיכים לעבוד
# במלואם למי שמקליד אותם — הם פשוט לא מוצעים יותר. את היכולות שלהם
# אפשר להשיג גם דרך הדשבורד (היסטוריה, ניתוח יומי/חודשי) ודרך
# הכפתורים בבוט (בחירת סוג עור, צילום היד).
COMMANDS = [
    ("start", "ברוכים הבאים + הגדרת סוג עור"),
    ("start_session", "התחלת מעקב חשיפה לשמש"),
    ("end_session", "סיום המעקב וחישוב מדד חשיפה"),
    ("dashboard", "האזור האישי — גרפים, היסטוריה ועריכה"),
]

# --- אנגלית — רדום (2026-09-14) -----------------------------------------
# טלגרם תומך בפרופיל לפי שפה דרך פרמטר language_code ב-setMyCommands/
# setMyDescription/setMyShortDescription, ובוחר אוטומטית לפי שפת הלקוח.
#
# התמיכה באנגלית כובתה באותו יום שבו נבנתה ("נחזור לזה מאוחר יותר"),
# והטקסטים כאן נשמרים מוכנים להפעלה מחדש. כרגע main() לא רק *לא* רושם
# אותם — הוא גם **מוחק** רישום קודם (ראו _clear_english שם), אחרת
# משתמש עם טלגרם באנגלית היה ממשיך לראות תפריט ותיאור באנגלית בזמן
# שהבוט עצמו עונה רק בעברית.
#
# להפעלה מחדש: להחזיר את הרישום ב-main(), ולהחזיר ENGLISH_ENABLED=True
# ב-i18n.py.
NAME_EN = "SunSafe – sun protection helper ☀️"

SHORT_DESCRIPTION_EN = (
    "🌞 Tracks your UV exposure through the day and tells you when to "
    "cover up. Send /start to begin."
)

DESCRIPTION_EN = (
    "☀️ SunSafe helps you track your daily UV exposure and avoid "
    "overexposure and sunburn.\n\n"
    "Getting started: /start — a quick intro and your skin type "
    "(Fitzpatrick scale), which everything else builds on.\n\n"
    "What else it does:\n"
    "• /start_session — starts tracking (share a location or type a city), "
    "with a UV forecast\n"
    "• /end_session — ends it and works out your personal exposure score "
    "(from your skin type and sunscreen)\n"
    "• /dashboard — your personal area: charts, history, and adding or "
    "editing sessions"
)

COMMANDS_EN = [
    ("start", "Welcome + set your skin type"),
    ("start_session", "Start tracking sun exposure"),
    ("end_session", "End tracking and get your exposure score"),
    ("dashboard", "Your personal area — charts, history, editing"),
]


def _assert_within_limits() -> None:
    """בדיקת מגבלות Bot API לפני שליחה — עדיף כשל ברור פה מ-400 סתום מטלגרם."""
    for label, name, short, desc, commands in (
        ("he", NAME, SHORT_DESCRIPTION, DESCRIPTION, COMMANDS),
        ("en", NAME_EN, SHORT_DESCRIPTION_EN, DESCRIPTION_EN, COMMANDS_EN),
    ):
        assert len(name) <= 64, f"[{label}] NAME too long: {len(name)}/64"
        assert len(short) <= 120, f"[{label}] SHORT_DESCRIPTION too long: {len(short)}/120"
        assert len(desc) <= 512, f"[{label}] DESCRIPTION too long: {len(desc)}/512"
        assert len(commands) <= 100, f"[{label}] too many commands: {len(commands)}/100"
        for cmd, cmd_desc in commands:
            assert 1 <= len(cmd) <= 32, f"[{label}] command name length invalid: {cmd!r}"
            assert cmd.replace("_", "").isalnum() and cmd == cmd.lower(), \
                f"[{label}] invalid command name: {cmd!r}"
            assert 1 <= len(cmd_desc) <= 256, \
                f"[{label}] command description length invalid for {cmd!r}: {len(cmd_desc)}"

    # שתי הרשימות חייבות לתאר את *אותן* פקודות — אחרת דובר אנגלית יראה
    # תפריט אחר מדובר עברית, וזה באג שקט שקשה לשים לב אליו.
    assert [c for c, _ in COMMANDS] == [c for c, _ in COMMANDS_EN], \
        "COMMANDS and COMMANDS_EN list different commands"


def _call(client: httpx.Client, method: str, payload: dict) -> None:
    response = client.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=10.0)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"{method} failed: {result}")
    logger.info("%s -> ok", method)


def _clear_english(client: httpx.Client) -> None:
    """
    מוחק את גרסאות ה-"en" של הפרופיל והתפריט מטלגרם.

    נדרש כי הרישום כבר בוצע פעם אחת (2026-09-14) לפני שהתמיכה באנגלית
    כובתה. בלי המחיקה, מי שהלקוח שלו באנגלית היה ממשיך לראות תפריט
    ותיאור באנגלית — בזמן שהבוט עצמו עונה רק בעברית. מחרוזת ריקה
    מוחקת גרסה ספציפית-לשפה ומחזירה את ברירת המחדל (העברית).

    בטוח להרצה גם אם מעולם לא נרשמה אנגלית — טלגרם מחזיר ok גם אז.
    """
    _call(client, "deleteMyCommands", {"language_code": "en"})
    _call(client, "setMyDescription", {"description": "", "language_code": "en"})
    _call(client, "setMyShortDescription", {"short_description": "", "language_code": "en"})
    _call(client, "setMyName", {"name": "", "language_code": "en"})


def main() -> None:
    _assert_within_limits()
    with httpx.Client() as client:
        # ברירת המחדל (בלי language_code) — עברית. זה מה שיראה כל מי
        # שהלקוח שלו לא באנגלית ולא בעברית.
        _call(client, "setMyName", {"name": NAME})
        _call(client, "setMyShortDescription", {"short_description": SHORT_DESCRIPTION})
        _call(client, "setMyDescription", {"description": DESCRIPTION})
        _call(client, "setMyCommands", {
            "commands": [{"command": cmd, "description": desc} for cmd, desc in COMMANDS]
        })

        # אנגלית כבויה כרגע — ומנקים רישום קודם, אם היה.
        _clear_english(client)

    print(
        "\nעודכן: Name / About / Description / Commands.\n"
        "מה שנשאר לעשות ידנית ב-BotFather (/mybots -> Edit @gil612Bot info) —\n"
        "אין להם API בכלל:\n"
        "  • Botpic — Edit Botpic, להעלות תמונת פרופיל לבוט\n"
        "  • Description picture — Edit Description Picture (אופציונלי)\n"
        "  • Privacy Policy — Edit Privacy Policy (קישור למדיניות פרטיות, אם רוצים)\n"
    )


if __name__ == "__main__":
    main()
