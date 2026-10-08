"""
SunSafe — מחרוזות דו-לשוניות (עברית / אנגלית)
------------------------------------------------
נוצר 2026-09-14. מה שהוביל לזה, מלוג פרודקשן: משתמש עם טלגרם באנגלית
שלח `/start`, קיבל מסך פתיחה בעברית בלבד, ושאל "English?" — והגייטקיפר
סיווג את זה כ-NOISE והשתיק את זה לגמרי.

בכוונה בלי ספריית i18n: מילון אחד ופונקציית t(). הפרויקט הזה מחזיק
מספר מצומצם של מחרוזות, והתלות הנוספת (gettext/קבצי .po) הייתה עולה
יותר ממה שהיא חוסכת.

**מצב נוכחי (2026-10-08): אנגלית דולקת.** לכל משתמש שפה שמורה
(users.language), והבוט עונה בה בכל הודעה — גם בפקודות בלי אותיות
כמו "/end_session 30", שאין בהן שום סימן שפה.

איך נקבעת השפה (resolve_language):
1. בחירה מפורשת — כפתור 🇬🇧/🇮🇱 בהודעת הפתיחה, או /language.
2. החלפה אוטומטית כשהמשתמש *כותב טקסט* בשפה השנייה (לא פקודה —
   "/start_session Haifa" הוא שם עיר, לא בחירת שפה). ראו
   detect_language_switch.
3. אחרת — השפה השמורה.
4. משתמש חדש בלי שפה שמורה: language_code של לקוח הטלגרם, *רק* כנקודת
   פתיחה (he -> עברית, כל השאר -> אנגלית). זה המקום היחיד שבו הוא
   משמש, והוא לא גובר על שום דבר שהמשתמש כתב או בחר — ראו התקלה
   מ-14.9 ב-resolve_language.

השפה של העדכון הנוכחי נשמרת ב-contextvar (set_lang / get_lang), כך
ש-t() ו-L() יודעות אותה בלי להעביר lang דרך כל פונקציה.

מחרוזות: STRINGS למטה למסלול ה-onboarding (המקור, עם בדיקת שלמות),
ו-L(he, en) לכל השאר — שתי השפות זו לצד זו בנקודת השימוש. ~100
מחרוזות חד-פעמיות לא מצדיקות מפתח בטבלה מרוחקת כל אחת.
"""

DEFAULT_LANGUAGE = "he"
SUPPORTED_LANGUAGES = ("he", "en")

# ---------------------------------------------------------------------
# מתג התמיכה באנגלית
# ---------------------------------------------------------------------
# נבנתה ונכבתה ב-2026-09-14, הודלקה מחדש ב-2026-10-08 יחד עם שפה
# שמורה לכל משתמש. כיבוי (False) מחזיר עברית תמיד, ללא קשר לשום דבר.
ENGLISH_ENABLED = True


import contextvars
import re

_current_lang: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sunsafe_lang", default=DEFAULT_LANGUAGE
)


def normalize_language(lang: str | None) -> str | None:
    """'en', 'EN', 'en-US' -> 'en'. כל דבר שלא נתמך -> None."""
    if not lang:
        return None
    code = str(lang).strip().lower()[:2]
    return code if code in SUPPORTED_LANGUAGES else None


def set_lang(lang: str | None) -> None:
    """קובעת את שפת העדכון הנוכחי (ל-thread/context הנוכחי)."""
    _current_lang.set(
        (normalize_language(lang) or DEFAULT_LANGUAGE) if ENGLISH_ENABLED else DEFAULT_LANGUAGE
    )


def get_lang() -> str:
    return _current_lang.get()


def L(he: str, en: str) -> str:
    """המחרוזת בשפה של העדכון הנוכחי."""
    return en if get_lang() == "en" else he

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


# מילים שמבקשות שפה במפורש — מספיקות לבדן, גם בהודעה של מילה אחת.
_EXPLICIT_EN = re.compile(r"\b(english|in english|inglish)\b|אנגלית", re.I)
_EXPLICIT_HE = re.compile(r"\bhebrew\b|עברית|\bivrit\b", re.I)
# כמה מילים לטיניות צריך כדי להחליף לאנגלית. שם עיר ("Tel Aviv",
# "San Jose") הוא עד שתי מילים ולא אומר כלום על השפה; משפט — כן.
_MIN_ENGLISH_WORDS = 3


def detect_language_switch(text: str | None) -> str | None:
    """
    האם ההודעה הזו היא סיבה *להחליף* את השפה השמורה. שמרנית בכוונה:
    החלפה שגויה עולה יותר מהחמצה (המשתמש יכול תמיד /language).

    - פקודות לא מחליפות שפה בכלל — הארגומנטים שלהן הם שמות ערים ומספרים.
    - בקשה מפורשת ("English?", "עברית", "in Hebrew please") -> השפה המבוקשת.
      נבדקת *לפני* הכתב: "שנה שפה לאנגלית" כתוב בעברית אבל מבקש אנגלית.
    - כל אות עברית -> עברית (דובר עברית לא כותב עברית בטעות).
    - לפחות שלוש מילים לטיניות -> אנגלית.
    """
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("/"):
        return None
    if _EXPLICIT_EN.search(stripped) and not _EXPLICIT_HE.search(stripped):
        return "en"
    if _EXPLICIT_HE.search(stripped) and not _EXPLICIT_EN.search(stripped):
        return "he"
    if _HEBREW_RE.search(stripped):
        return "he"
    latin_words = re.findall(r"[A-Za-z]{2,}", stripped)
    if len(latin_words) >= _MIN_ENGLISH_WORDS:
        return "en"
    return None


def is_language_request(text: str | None) -> bool:
    """הודעה קצרה (עד 4 מילים) שכל עניינה בקשת שפה: "English?", "עברית בבקשה"."""
    if not text or text.strip().startswith("/"):
        return False
    if len(text.split()) > 4:
        return False
    return bool(_EXPLICIT_EN.search(text) or _EXPLICIT_HE.search(text))


def resolve_user_language(
    text: str | None,
    stored: str | None,
    client_language_code: str | None = None,
) -> tuple[str, bool]:
    """
    השפה לעדכון הזה, ו-True אם היא שונה מהשמורה (כלומר צריך לשמור).

    סדר עדיפויות: מה שנכתב עכשיו > מה שנשמר > language_code של הלקוח
    (רק למשתמש חדש) > עברית.
    """
    if not ENGLISH_ENABLED:
        return DEFAULT_LANGUAGE, False
    stored = normalize_language(stored)
    switched = detect_language_switch(text)
    if switched:
        return switched, switched != stored
    if stored:
        return stored, False
    client = (client_language_code or "").lower()
    guess = "he" if client.startswith("he") or client.startswith("iw") else ("en" if client else DEFAULT_LANGUAGE)
    return guess, True


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
    # נוסף 16.9.2026. עד אז "/help" לא היה ב-COMMAND_HANDLERS בכלל, ולכן
    # עבר דרך הגייטקיפר ונותב ל-Agent Loop — כלומר Gemini המציא את טקסט
    # העזרה בזמן אמת. נצפה בפועל: התשובה הייתה סבירה אבל השמיטה את
    # /today, /my_sessions, /offline_session ו-/diagnose_skin, והיא
    # שונה בכל הרצה. טקסט עזרה שמשתנה בכל פעם, שעלול להמציא פקודה
    # שלא קיימת, ושעולה שלוש קריאות Gemini מתוך 15 לדקה — בשביל
    # מחרוזת שלא משתנה.
    #
    # למה זה לא סותר את ההחלטה מ-12.9 להסיר את רשימת הפקודות מ-/start:
    # שם היא הוצגה למי שלא ביקש אותה ולפני שעשה משהו. כאן המשתמש
    # ביקש במפורש.
    "help": {
        "he": (
            "☀️ SunSafe — מעקב חשיפה לשמש\n\n"
            "היומיום:\n"
            "/start_session <עיר> — פותח מעקב. אפשר גם לשתף מיקום.\n"
            "/end_session [SPF] — סוגר אותו ומחשב את החשיפה.\n"
            "/dashboard — האזור האישי: היסטוריה, גרפים ועריכה.\n\n"
            "עוד:\n"
            "/today — סיכום היום, כולל השוואה מול SPF קבוע.\n"
            "/my_sessions — ה-sessions האחרונים שלכם.\n"
            "/offline_session — לתעד חשיפה בדיעבד, בלי קליטה.\n"
            "/diagnose_skin — בדיקת סימני כוויה מתצלום.\n"
            "/set_skin_type <1-6> — לעדכן סוג עור.\n"
            "/start — להתחיל מחדש ולבחור סוג עור.\n"
            "/language — English / עברית\n\n"
            "ואפשר גם פשוט לשאול: \"מה ה-UV בתל אביב?\", "
            "\"מה היה ה-UV במצפה רמון אתמול?\"\n\n"
            "לבחירת קרם הגנה ספציפי — במיוחד אם יש רגישות או מצב עור — "
            "כדאי לשאול רוקח או רופא עור. אני נותן SPF מומלץ, לא מוצר."
        ),
        "en": (
            "☀️ SunSafe — sun exposure tracking\n\n"
            "Every day:\n"
            "/start_session <city> — start tracking. You can share a location instead.\n"
            "/end_session [SPF] — close it and get your exposure.\n"
            "/dashboard — your history, charts and edits.\n\n"
            "More:\n"
            "/today — today's summary.\n"
            "/my_sessions — your recent sessions.\n"
            "/offline_session — log exposure after the fact, with no signal.\n"
            "/diagnose_skin — check a photo for burn signs.\n"
            "/set_skin_type <1-6> — update your skin type.\n"
            "/start — start over and pick a skin type.\n"
            "/language — switch language\n\n"
            "You can also just ask: \"What's the UV in Tel Aviv?\"\n\n"
            "For choosing a specific sunscreen — especially with a skin condition "
            "or sensitivity — ask a pharmacist or a dermatologist. I give a "
            "recommended SPF, not a product."
        ),
    },
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
    "skin_4": {"he": "4 · זית", "en": "4 · Olive"},
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
    # השורה על "אין GPS" נוספה 2026-09-14: האישור הזה הציע רק את כפתור
    # שיתוף המיקום, ומי שאין לו GPS או שסירב להרשאה נשאר בלי שום דרך
    # להתחיל. חשוב לדייק — שם יישוב *בלי* הפקודה לא פותח session אלא
    # מגיע ל-Agent Loop כשאלת UV, ולכן הדוגמאות כוללות את /start_session.
    #
    # כל פקודה בשורה משלה, ולא משורשרות בתוך המשפט: פקודה לטינית באמצע
    # טקסט עברי נשברת ויזואלית ע"י אלגוריתם ה-bidi והסדר יוצא מבלבל.
    # בשורה נפרדת היא גם נקראת נכון וגם קלה יותר ללחיצה (טלגרם מרנדר
    # /command כקישור לחיץ).
    "skin_saved": {
        "he": (
            "✅ נשמר: סוג עור {label}.\n\n"
            "זה הכל — אפשר להתחיל. לחצו למטה כשאתם יוצאים לשמש, "
            "ואני אחשב לכם את החשיפה.\n\n"
            "אין GPS? שלחו\n"
            "/start_session חיפה\n\n"
            "— או עם קואורדינטות:\n"
            "/start_session 32.08, 34.78"
        ),
        "en": (
            "✅ Saved: skin type {label}.\n\n"
            "That's it — you're set. Tap below when you head out into the "
            "sun, and I'll work out your exposure.\n\n"
            "No GPS? Send\n"
            "/start_session Haifa\n\n"
            "— or with coordinates:\n"
            "/start_session 32.08, 34.78"
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


def t(key: str, lang: str | None = None, **kwargs) -> str:
    """
    מחזירה את המחרוזת במפתח `key` בשפה `lang`, עם החלפת פרמטרים.

    שפה חסרה נופלת חזרה לעברית ולא מתפוצצת — הודעה בשפה הלא נכונה עדיפה
    על חריגה מול משתמש. הבדיקה ב-tests/test_i18n.py אמורה לתפוס את זה
    הרבה לפני פרודקשן.
    """
    entry = STRINGS[key]
    template = entry.get(lang or get_lang()) or entry[DEFAULT_LANGUAGE]
    return template.format(**kwargs) if kwargs else template


def skin_type_label(skin_type: int, lang: str | None = None) -> str:
    """תווית סוג עור ("3 · בינוני" / "3 · Medium"), עם נפילה למספר גולמי."""
    key = f"skin_{skin_type}"
    return t(key, lang) if key in STRINGS else str(skin_type)
