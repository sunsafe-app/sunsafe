"""
SunSafe — מחרוזות דו-לשוניות (עברית / אנגלית)
------------------------------------------------
נוצר 2026-09-14. מה שהוביל לזה, מלוג פרודקשן: משתמש עם טלגרם באנגלית
שלח `/start`, קיבל מסך פתיחה בעברית בלבד, ושאל "English?" — והגייטקיפר
סיווג את זה כ-NOISE והשתיק את זה לגמרי.

בכוונה בלי ספריית i18n: מילון אחד ופונקציית t(). הפרויקט הזה מחזיק
מספר מצומצם של מחרוזות, והתלות הנוספת (gettext/קבצי .po) הייתה עולה
יותר ממה שהיא חוסכת.

כלל השפה: טלגרם שולח language_code בכל עדכון (למשל "he", "en-GB").
עברית -> עברית; *כל* שפה אחרת -> אנגלית, כי אנגלית היא שפת הגישור
הסבירה למי שלא קורא עברית. בלי language_code כלל -> עברית, שפת הבית
של הפרויקט.

הערה על ההיקף (שלב 1): כאן יושבות מחרוזות מסלול ה-onboarding בלבד —
המסלול שכל משתמש חדש עובר. שאר הפקודות, הדשבורד וה-Edge Functions
עדיין בעברית בלבד ויתורגמו בשלב נפרד. הסיבה שהגבול עובר כאן: כל
המסלול הזה מופעל מהודעה נכנסת, שנושאת language_code — בלי צורך
בעמודה חדשה ב-DB. הודעות שהבוט *יוזם* בעצמו (סגירת session אחרי
שקיעה) יצטרכו שפה שמורה, וזה חלק משלב 2.
"""

DEFAULT_LANGUAGE = "he"
SUPPORTED_LANGUAGES = ("he", "en")

# ---------------------------------------------------------------------
# מתג התמיכה באנגלית — כבוי (2026-09-14)
# ---------------------------------------------------------------------
# התמיכה באנגלית נבנתה, הופעלה, ונכבתה באותו יום לבקשת המשתמש ("נחזור
# לזה מאוחר יותר"). היא לא נמחקה: טבלת המחרוזות למטה מלאה ותקינה בשתי
# השפות, הבדיקות ממשיכות לאכוף את שלמותה, וכל נתיבי ה-lang בקוד
# נשארו במקומם.
#
# **להפעלה מחדש**: להחזיר כאן True, ולהחזיר ב-set_bot_profile.py את
# רישום הפרופיל והתפריט באנגלית (ראו ההערה שם — כרגע הוא לא רק לא
# רושם אנגלית, אלא גם *מוחק* רישום קודם).
#
# כל עוד זה False, resolve_language מחזירה עברית תמיד, ללא קשר למה
# שנכתב — כך שאין מצב ביניים שבו חלק מההודעות באנגלית וחלק בעברית.
ENGLISH_ENABLED = False


import re

# טווח היוניקוד של האלפבית העברי.
_HEBREW_RE = re.compile(r"[֐-׿]")
# פקודה בתחילת ההודעה ("/start", "/start_session") — תמיד לטינית, בכל
# שפה שהמשתמש מדבר, ולכן חסרת ערך כאות-שפה. מסירים אותה לפני הזיהוי.
_LEADING_COMMAND_RE = re.compile(r"^/\w+")


def detect_language_from_text(text: str | None) -> str | None:
    """
    מזהה שפה מתוך *מה שהמשתמש כתב*, או None אם אין בטקסט סימן מספיק.

    זיהוי לפי תווים ולא לפי מודל: זול, מיידי, דטרמיניסטי, ולא צורך
    quota. עברית מזוהה בוודאות מלאה מטווח היוניקוד; טקסט לטיני אמיתי
    (אחרי הסרת הפקודה) נחשב אנגלית.

    הפקודה עצמה מוסרת קודם, אחרת "/start" היה נספר כאנגלית ודובר
    עברית היה מקבל onboarding באנגלית רק בגלל שהפקודות לטיניות.
    """
    if not text:
        return None
    stripped = _LEADING_COMMAND_RE.sub("", text.strip(), count=1).strip()
    if _HEBREW_RE.search(stripped):
        return "he"
    if any(ch.isalpha() for ch in stripped):
        return "en"
    # בלי אותיות בכלל (מספרים, אמוג'י, "14:30") — אין כאן שום מידע על שפה.
    return None


def resolve_language(text: str | None = None) -> str:
    """
    קובעת באיזו שפה לענות: **עברית כברירת מחדל**, ואנגלית רק כשהמשתמש
    באמת כותב באנגלית.

    ה-language_code של לקוח הטלגרם *לא* משמש כאן בכוונה. זו הייתה
    הגרסה הראשונה (2026-09-14) והיא נכשלה מיידית בפרודקשן: משתמש עם
    טלגרם מוגדר-אנגלית כתב "עברית", ואז "שנה שפה לעברית" — וקיבל
    אנגלית בשתי הפעמים, כולל המשפט האבסורדי "I am already speaking
    Hebrew!". ההגדרה בלקוח שלו לא העידה כלום על השפה שבה הוא רוצה
    לדבר, והיא גם סתרה את ברירת המחדל הנכונה למוצר הזה — עברית.

    כרגע ENGLISH_ENABLED=False, ולכן הפונקציה מחזירה עברית תמיד —
    ראו ההערה על המתג למעלה. שאר הלוגיקה נשמרת כדי שההפעלה מחדש תהיה
    שינוי של שורה אחת.

    >>> resolve_language("עברית")            # 'he'
    >>> resolve_language("שנה שפה לעברית")   # 'he'
    >>> resolve_language("hello")            # 'he' כרגע; 'en' כשהמתג דלוק
    >>> resolve_language("/start")           # 'he' — פקודה אינה אות-שפה
    >>> resolve_language(None)               # 'he'
    """
    if not ENGLISH_ENABLED:
        return DEFAULT_LANGUAGE
    return detect_language_from_text(text) or DEFAULT_LANGUAGE


# ---------------------------------------------------------------------
# טבלת המחרוזות. כל מפתח חייב להכיל את *שתי* השפות — יש בדיקה שאוכפת
# את זה (tests/test_i18n.py), כדי שמחרוזת חדשה לא תישכח באנגלית ותיפול
# בשקט חזרה לעברית מול משתמש שלא קורא אותה.
# ---------------------------------------------------------------------
STRINGS: dict[str, dict[str, str]] = {
    # --- /start ---
    "welcome": {
        "he": (
            "☀️ ברוכים הבאים ל-SunSafe!\n\n"
            "אני עוקב אחרי החשיפה שלכם לשמש ומזכיר מתי להתגונן. "
            "נשאר רק לדעת מה סוג העור שלכם 👇"
        ),
        "en": (
            "☀️ Welcome to SunSafe!\n\n"
            "I track your sun exposure and remind you when to take cover. "
            "All I need first is your skin type 👇"
        ),
    },
    "skin_scale_caption": {
        "he": (
            "🎨 השוו לגוון העור הטבעי שלכם (לא משוזף) ולתגובה הרגילה שלו "
            "לשמש, ובחרו למטה:"
        ),
        "en": (
            "🎨 Compare with your natural, untanned skin tone and how it "
            "usually reacts to the sun, then pick below:"
        ),
    },
    "skin_question": {
        "he": "מה סוג העור שלכם?",
        "en": "What's your skin type?",
    },
    # --- תוויות סוג העור (הכפתורים) ---
    "skin_1": {"he": "1 · בהיר מאוד", "en": "1 · Very fair"},
    "skin_2": {"he": "2 · בהיר", "en": "2 · Fair"},
    "skin_3": {"he": "3 · בינוני", "en": "3 · Medium"},
    "skin_4": {"he": "4 · זיתי", "en": "4 · Olive"},
    "skin_5": {"he": "5 · חום", "en": "5 · Brown"},
    "skin_6": {"he": "6 · כהה מאוד", "en": "6 · Very dark"},
    "skin_photo_button": {
        "he": "📷 לא בטוחים? שלחו תמונה של היד",
        "en": "📷 Not sure? Send a photo of your hand",
    },
    "skin_photo_instructions": {
        "he": (
            "📷 צלמו את גב היד שלכם והעלו לכאן את התמונה.\n\n"
            "לתוצאה טובה: באור יום טבעי, לא בשמש ישירה ולא בתאורה צהובה, "
            "ועל העור הלא-משוזף (הצד הפנימי של הזרוע עובד טוב גם).\n\n"
            "אציע לכם סוג עור, ותאשרו בלחיצה."
        ),
        "en": (
            "📷 Take a photo of the back of your hand and send it here.\n\n"
            "For a good result: natural daylight, not direct sun and not "
            "yellow indoor lighting, on untanned skin (the inner forearm "
            "works well too).\n\n"
            "I'll suggest a skin type, and you confirm with one tap."
        ),
    },
    # --- אישור ושמירה ---
    "skin_saved": {
        "he": (
            "✅ נשמר: סוג עור {label}.\n\n"
            "זה הכל — אפשר להתחיל. לחצו למטה כשאתם יוצאים לשמש, "
            "ואני אחשב לכם את החשיפה."
        ),
        "en": (
            "✅ Saved: skin type {label}.\n\n"
            "That's it — you're set. Tap below when you head out into the "
            "sun, and I'll work out your exposure."
        ),
    },
    "share_location_button": {
        "he": "📍 שתפו מיקום והתחילו מעקב",
        "en": "📍 Share location and start tracking",
    },
    "skin_toast": {
        "he": "סוג עור {n}",
        "en": "Skin type {n}",
    },
    # --- הצעה מתמונה ---
    "photo_suggestion": {
        "he": (
            "לפי התמונה, נראה כמו סוג עור {skin_type} — {reasoning} "
            "(רמת ביטחון: {confidence}).\n\n"
            "שימו לב: זו הערכה חזותית משוערת בלבד, לא שאלון רשמי המבוסס "
            "על היסטוריית שרפות-שמש. מה נשמור?"
        ),
        "en": (
            "From the photo, this looks like skin type {skin_type} — "
            "{reasoning} (confidence: {confidence}).\n\n"
            "Note: this is a rough visual estimate only, not a proper "
            "assessment based on your sunburn history. What should I save?"
        ),
    },
    "photo_confirm_button": {
        "he": "✅ כן, {label}",
        "en": "✅ Yes, {label}",
    },
    "photo_choose_other_button": {
        "he": "בחירה אחרת",
        "en": "Pick a different one",
    },
    "photo_analysis_failed": {
        "he": "לא הצלחתי לנתח את התמונה כרגע. אפשר לנסות תמונה נוספת, או לבחור ידנית:",
        "en": "I couldn't analyse that photo right now. Try another one, or pick manually:",
    },
    "photo_rejected": {
        "he": "לא הצלחתי להעריך סוג עור מהתמונה הזו ({reason}). אפשר לשלוח תמונה ברורה יותר של העור, או לבחור ידנית:",
        "en": "I couldn't estimate a skin type from that photo ({reason}). Send a clearer photo of the skin, or pick manually:",
    },
    # --- שערים והודעות שירות ---
    "need_skin_type_first": {
        "he": "קודם צריך להגדיר סוג עור — בחרו למטה:",
        "en": "You'll need a skin type first — pick below:",
    },
    "need_username": {
        "he": "צריך שיהיה לך username מוגדר בהגדרות טלגרם כדי להשתמש בפקודות האלה.",
        "en": "You need a username set in your Telegram settings to use this bot.",
    },
    "storage_error": {
        "he": "משהו השתבש בשמירת הנתונים. נסו שוב בעוד רגע.",
        "en": "Something went wrong saving your data. Please try again in a moment.",
    },
    "agent_failed": {
        "he": "לא הצלחתי לענות על זה כרגע. נסו שוב, או הקלידו \"/\" לתפריט הפקודות.",
        "en": "I couldn't answer that right now. Try again, or type \"/\" for the command menu.",
    },
}


def t(key: str, lang: str = DEFAULT_LANGUAGE, **kwargs) -> str:
    """
    מחזירה את המחרוזת במפתח `key` בשפה `lang`, עם החלפת פרמטרים.

    שפה חסרה נופלת חזרה לעברית ולא מתפוצצת — הודעה בשפה הלא נכונה עדיפה
    על חריגה מול משתמש. הבדיקה ב-tests/test_i18n.py אמורה לתפוס את זה
    הרבה לפני פרודקשן.
    """
    entry = STRINGS[key]
    template = entry.get(lang) or entry[DEFAULT_LANGUAGE]
    return template.format(**kwargs) if kwargs else template


def skin_type_label(skin_type: int, lang: str = DEFAULT_LANGUAGE) -> str:
    """תווית סוג עור ("3 · בינוני" / "3 · Medium"), עם נפילה למספר גולמי."""
    key = f"skin_{skin_type}"
    return t(key, lang) if key in STRINGS else str(skin_type)
