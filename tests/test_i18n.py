"""
בדיקה ידנית (לא pytest) לתמיכה הדו-לשונית (2026-09-14).

שתי תקלות פרודקשן הובילו לקובץ הזה, שתיהן באותו יום:
1. משתמש שאל "English?" — הגייטקיפר סיווג NOISE והשתיק אותו לגמרי.
2. הגרסה הראשונה של התיקון נשענה על language_code של לקוח הטלגרם,
   אז אותו משתמש (טלגרם מוגדר-אנגלית) כתב "עברית" ואז "שנה שפה
   לעברית" — וקיבל אנגלית בשתי הפעמים, כולל "I am already speaking
   Hebrew!" שנכתב באנגלית. ראו את שחזור השיחה בסעיף 7.

2026-10-08: אנגלית הודלקה מחדש, עם שפה שמורה לכל משתמש (users.language).
מאוחר יותר"). הקובץ הזה בודק עכשיו שני דברים במקביל: שהבוט אכן מדבר
עברית בלבד כרגע, ושהתשתית והתרגומים נשארו שלמים מתחת למתג — כך
שההפעלה מחדש תהיה שינוי של שורה אחת (i18n.ENGLISH_ENABLED).

מכוסה כאן:
1. resolve_language — עברית תמיד כל עוד המתג כבוי; הזיהוי שמתחתיו תקין.
2. שלמות טבלת המחרוזות — כל מפתח קיים בשתי השפות, ואותם פרמטרי {}.
3. onboarding בעברית בפועל, והתרגום האנגלי שמור.
4. ה-dispatch מעביר lang רק ל-handlers שמצהירים עליו.
5. שפת התשובה ב-prompt של ה-Agent Loop.
6. הגייטקיפר כבר לא מסווג שאלות על הבוט כ-NOISE.
7. שחזור השיחה מהפרודקשן — עכשיו כולה בעברית.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

# tests/ נמצא רמה אחת מתחת לשורש הריפו — מוסיפים את שורש הריפו ל-sys.path
# כדי ש-import bot_commands (ומודולים אחיים אחרים) ימשיך לעבוד גם כשמריצים
# מ-tests/ ולא משורש הריפו.
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import string
from unittest.mock import patch

import bot_commands as bc
import i18n
from message_gatekeeper import CLASSIFICATION_PROMPT

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1) קביעת השפה (2026-10-08: אנגלית דולקת, שפה שמורה לכל משתמש)
# ---------------------------------------------------------------------
check("English is switched on", i18n.ENGLISH_ENABLED is True)
check("Hebrew is the default", i18n.DEFAULT_LANGUAGE == "he")

# מתי הודעה *מחליפה* שפה. שמרני: פקודות אף פעם, שמות ערים לא.
for text, expected in {
    "עברית": "he",
    "English?": "en",
    "שנה שפה לאנגלית": "en",            # כתוב בעברית, מבקש אנגלית
    "Switch language to Hebrew": "he",  # כתוב באנגלית, מבקש עברית
    "I am already speaking Hebrew!": "he",
    "what is the UV in Haifa": "en",
    "מה ה-UV בחיפה": "he",
    "Tel Aviv": None,                   # שם עיר, לא שפה
    "UV Haifa": None,
    "/start_session Tel Aviv": None,    # פקודה — אף פעם
    "/language en": None,
    "14:30": None,
    "": None,
    None: None,
}.items():
    got = i18n.detect_language_switch(text)
    check(f"detect_language_switch({str(text)[:28]!r}) -> {expected}", got == expected, f"-> {got}")

# סדר העדיפויות: מה שנכתב > מה שנשמר > language_code (רק לחדש) > עברית
for (text, stored, client), expected in {
    (("/end_session 30", "en", None)): ("en", False),   # בלי אותיות -> השמורה
    (("/end_session 30", "he", "en")): ("he", False),    # הלקוח לא גובר על השמורה
    (("What's the UV in Haifa now?", "he", "he")): ("en", True),
    (("/start", None, "en")): ("en", True),              # חדש, לקוח באנגלית
    (("/start", None, "de")): ("en", True),              # חדש, לא דובר עברית
    (("/start", None, "he")): ("he", True),
    (("/start", None, None)): ("he", True),
    (("עברית", None, "en")): ("he", True),               # מה שנכתב גובר על הלקוח
}.items():
    got = i18n.resolve_user_language(text, stored, client)
    check(f"resolve_user_language({text[:16]!r}, stored={stored}, client={client}) -> {expected}",
          got == expected, f"-> {got}")

for text, expected in {"English?": True, "עברית בבקשה": True, "in Hebrew please": True,
                       "What's the UV in Tel Aviv in English?": False, "/language": False,
                       "Tel Aviv": False}.items():
    check(f"is_language_request({text!r}) -> {expected}", i18n.is_language_request(text) == expected)

# הזיהוי הישן לפי תווים נשאר (משמש ב-resolve_language)
for text, expected in {"עברית": "he", "hello": "en", "/start": None, "14:30": None, "": None}.items():
    got = i18n.detect_language_from_text(text)
    check(f"detect_language_from_text({str(text)[:20]!r}) -> {expected}", got == expected, f"-> {got}")

# L() ו-t() קוראות את שפת העדכון הנוכחי
i18n.set_lang("en")
check("L() follows the current language", i18n.L("שלום", "hello") == "hello")
check("t() follows the current language", i18n.t("skin_question") == "What's your skin type?")
i18n.set_lang("he")
check("...and back", i18n.L("שלום", "hello") == "שלום")
i18n.set_lang("fr")
check("an unsupported language falls back to Hebrew", i18n.get_lang() == "he")

# ---------------------------------------------------------------------
# 2) שלמות טבלת המחרוזות
# ---------------------------------------------------------------------
missing = [k for k, v in i18n.STRINGS.items() if set(v) != set(i18n.SUPPORTED_LANGUAGES)]
check("every string exists in both languages", not missing, f"-> missing: {missing}")


def placeholders(template):
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


mismatched = [
    key for key, entry in i18n.STRINGS.items()
    if placeholders(entry["he"]) != placeholders(entry["en"])
]
check("both languages use the same {placeholders}", not mismatched, f"-> {mismatched}")

untranslated = [
    key for key, entry in i18n.STRINGS.items()
    if entry["he"] == entry["en"] and not placeholders(entry["he"])
]
check("no string was left identical in both languages", not untranslated, f"-> {untranslated}")

# עברית אמיתית בצד העברי, בלי עברית בצד האנגלי
hebrew = re.compile(r"[֐-׿]")
leaked = [k for k, v in i18n.STRINGS.items() if hebrew.search(v["en"])]
check("no Hebrew characters leaked into the English strings", not leaked, f"-> {leaked}")

check("t() falls back to Hebrew for an unknown language",
      i18n.t("skin_question", "de") == i18n.t("skin_question", "he"))

# ---------------------------------------------------------------------
# 3) onboarding ופקודות בשתי השפות
# ---------------------------------------------------------------------
def run_update(text, stored, client_lang="en", username="gil612", extra_patches=()):
    """מריץ handle_update אמיתי עם שפה שמורה מדומה. מחזיר (הודעות, תמונות, שפות שנשמרו)."""
    msgs, pics, saved = [], [], []
    bc._user_language_cache.clear()
    with patch.object(bc, "send_message", lambda cid, text, reply_markup=None: msgs.append((text, reply_markup))), \
         patch.object(bc, "send_photo", lambda cid, img, caption=None, reply_markup=None: pics.append((caption, reply_markup))), \
         patch.object(bc, "_load_fitzpatrick_scale_image", lambda: b"png"), \
         patch.object(bc, "update_rows", lambda *a, **k: []), \
         patch.object(bc, "_stored_language", lambda u: stored), \
         patch.object(bc, "_remember_language", lambda u, l: saved.append(l)), \
         patch.object(bc, "_mirror_incoming_to_admin", lambda *a, **k: None):
        for p in extra_patches:
            p.start()
        try:
            bc._pending_skin_type_pick.clear()
            bc.handle_update({"message": {
                "chat": {"id": 123},
                "from": {"username": username, "language_code": client_lang},
                "text": text,
            }})
        finally:
            for p in extra_patches:
                p.stop()
    return msgs, pics, saved


# משתמש חדש עם טלגרם באנגלית -> פתיחה באנגלית, ועם כפתורי שפה לתיקון מהיר
msgs, pics, saved = run_update("/start", stored=None, client_lang="en")
check("a new English-client user gets the English welcome",
      msgs and msgs[0][0].startswith("☀️ Welcome to SunSafe"), f"-> {msgs[0][0][:30] if msgs else None}")
lang_buttons = [b["callback_data"] for row in (msgs[0][1] or {}).get("inline_keyboard", []) for b in row] if msgs else []
check("...with both language buttons on it", lang_buttons == ["lang:he:start", "lang:en:start"], f"-> {lang_buttons}")
labels = [b["text"] for row in pics[0][1]["inline_keyboard"] for b in row]
check("...and English skin-type buttons", "3 · Medium" in labels, f"-> {labels[:3]}")
check("...and the guess is remembered", saved == ["en"], f"-> {saved}")

# משתמש ותיק ששמור בעברית — לקוח באנגלית לא משנה כלום (התקלה מ-14.9)
msgs, pics, saved = run_update("/start", stored="he", client_lang="en")
check("a stored-Hebrew user with an English client still gets Hebrew",
      msgs and "ברוכים הבאים" in msgs[0][0])
check("...and nothing is re-saved", saved == [], f"-> {saved}")

# פקודה בלי אותיות בכלל — רק השפה השמורה יכולה לענות נכון
msgs, _, _ = run_update("/end_session abc", stored="en", client_lang="he")
check("a command with no language signal answers in the stored language (en)",
      msgs and msgs[0][0].startswith("Without sunscreen"), f"-> {msgs[0][0][:30] if msgs else None}")
msgs, _, _ = run_update("/end_session abc", stored="he", client_lang="en")
check("...and in Hebrew for a Hebrew user", msgs and msgs[0][0].startswith("בלי קרם הגנה"))

# בקשת שפה קצרה מקבלת אישור ישיר, לא את ה-Agent Loop
agent_calls = []
msgs, _, saved = run_update(
    "English?", stored="he",
    extra_patches=(patch.object(bc, "classify_message", lambda t: "VALID"),
                   patch.object(bc, "run_agent_via_mcp", lambda task: agent_calls.append(task) or "...")),
)
check("'English?' switches the language", saved and set(saved) == {"en"}, f"-> {saved}")
check("...confirms in English", msgs and "in English from now on" in msgs[0][0], f"-> {msgs[0][0][:40] if msgs else None}")
check("...without calling the agent", not agent_calls)

# לחיצה על כפתור שפה
msgs, saved = [], []
bc._user_language_cache.clear()
with patch.object(bc, "answer_callback_query", lambda qid, text=None: None), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: msgs.append(text)), \
     patch.object(bc, "_stored_language", lambda u: "en"), \
     patch.object(bc, "_remember_language", lambda u, l: saved.append(l)), \
     patch.object(bc, "select_rows", lambda *a, **k: []):
    bc.handle_callback_query({
        "id": "q1", "data": "lang:he",
        "from": {"username": "gil612", "language_code": "en"},
        "message": {"chat": {"id": 123}, "text": "☀️ Welcome to SunSafe!"},
    })
check("the 🇮🇱 button saves Hebrew", saved == ["he"], f"-> {saved}")
check("...confirms in Hebrew", msgs and "בעברית" in msgs[0], f"-> {msgs[:1]}")
check("...and re-asks the skin type in Hebrew for a user who has none yet",
      len(msgs) == 2 and msgs[1] == "מה סוג העור שלכם?", f"-> {msgs[1:]}")

# לחיצה על כפתור שפה *בהודעת הפתיחה* — ה-onboarding כולו נשלח מחדש בשפה
# החדשה, גם למשתמש שכבר יש לו סוג עור (תקלה 8.10: קיבל רק אישור, והסולם
# והכפתורים נשארו בעברית).
msgs, pics, saved = [], [], []
with patch.object(bc, "answer_callback_query", lambda qid, text=None: None), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: msgs.append((text, reply_markup))), \
     patch.object(bc, "send_photo", lambda cid, img, caption=None, reply_markup=None: pics.append((caption, reply_markup))), \
     patch.object(bc, "_load_fitzpatrick_scale_image", lambda: b"png"), \
     patch.object(bc, "_stored_language", lambda u: "he"), \
     patch.object(bc, "_remember_language", lambda u, l: saved.append(l)), \
     patch.object(bc, "select_rows", lambda *a, **k: [{"skin_type": 3}]):
    bc.handle_callback_query({
        "id": "q1", "data": "lang:en:start",
        "from": {"username": "gil612", "language_code": "he"},
        "message": {"chat": {"id": 123}, "text": "☀️ ברוכים הבאים ל-SunSafe!"},
    })
check("the welcome-message English button saves English", saved == ["en"], f"-> {saved}")
check("...and re-sends the welcome in English", msgs and msgs[0][0].startswith("☀️ Welcome to SunSafe"),
      f"-> {msgs[0][0][:30] if msgs else None}")
check("...and the skin-type picker in English",
      pics and "3 · Medium" in [b["text"] for row in pics[0][1]["inline_keyboard"] for b in row])
check("...with the language buttons still marked as coming from /start",
      msgs and msgs[0][1]["inline_keyboard"][0][1]["callback_data"] == "lang:en:start")

# לחיצה על כפתור סוג עור — בשפה השמורה, לא בשפת ההודעה שהכפתור יושב עליה
msgs = []
with patch.object(bc, "upsert_row", lambda *a, **k: None), \
     patch.object(bc, "answer_callback_query", lambda qid, text=None: None), \
     patch.object(bc, "_stored_language", lambda u: "he"), \
     patch.object(bc, "_remember_language", lambda u, l: None), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: msgs.append(text)):
    bc.handle_callback_query({
        "id": "q1", "data": "skin:4",
        "from": {"username": "gil612", "language_code": "en"},
        "message": {"chat": {"id": 123}, "caption": "🎨 Compare with your natural skin tone"},
    })
check("a skin button answers in the stored language",
      msgs and "נשמר: סוג עור 4 · זית" in msgs[0], f"-> {msgs[0][:45] if msgs else None}")

# מי שאין לו GPS חייב לקבל כאן דרך חלופית — בשתי השפות.
confirmation = msgs[0] if msgs else ""
check("the confirmation offers a no-GPS alternative", "אין GPS" in confirmation)
check("...naming the command, since a bare city name does NOT open a session",
      "/start_session חיפה" in confirmation, f"-> ...{confirmation[-70:]}")
check("...and showing the coordinates form too", "32.08, 34.78" in confirmation)
check("...and in English too", "/start_session Haifa" in i18n.t("skin_saved", "en", label="x"))

# הקישור לדשבורד ול-Mini App נושא את השפה
i18n.set_lang("en")
with patch.object(bc, "insert_row", lambda *a, **k: None):
    link = bc.create_magic_link("gil612")
check("the dashboard link carries lang=en", link.endswith("&lang=en"), f"-> {link[-20:]}")
check("format_duration in English", bc.format_duration_he(105) == "about 1 hour 45 minutes",
      f"-> {bc.format_duration_he(105)}")
i18n.set_lang("he")
check("...and in Hebrew", bc.format_duration_he(105) == "כשעה ו-45 דקות")

# ---------------------------------------------------------------------
# 4) ה-dispatch
# ---------------------------------------------------------------------
check("_dispatch detects a handler that wants lang", bc._handler_takes_lang(bc.handle_start))
check("_dispatch detects one that doesn't", not bc._handler_takes_lang(bc.handle_today))

called = {}


def _wants_lang(chat_id, username, args, lang="he"):
    called["lang"] = lang


def _plain(chat_id, username, args):
    called["plain"] = True


bc._dispatch(_wants_lang, 1, "u", "", "en")
check("a lang-aware handler receives it", called.get("lang") == "en", f"-> {called}")
called.clear()
bc._dispatch(_plain, 1, "u", "", "en")
check("a plain handler is still called with three arguments", called.get("plain") is True)

# ---------------------------------------------------------------------
# 5) שפת התשובה ב-Agent Loop
# ---------------------------------------------------------------------
check("the agent prompt asks for a Hebrew answer",
      "ענה בעברית" in bc._build_freeform_task("מה ה-UV?", "he"))
check("the agent prompt asks for an English answer for English users",
      "ענה באנגלית" in bc._build_freeform_task("what's the UV?", "en"))
check("the agent prompt now covers questions about the bot itself",
      "שאלה על הבוט עצמו" in bc._build_freeform_task("x", "he"))
check("the agent prompt points at the dashboard, not removed commands",
      "/dashboard" in bc._build_freeform_task("x", "he")
      and "/my_sessions" not in bc._build_freeform_task("x", "he"))

# ---------------------------------------------------------------------
# 6) הגייטקיפר
# ---------------------------------------------------------------------
check("the gatekeeper prompt no longer calls 'what can you do' noise",
      '"what can you do"' not in CLASSIFICATION_PROMPT)
check("it now treats questions about the bot as valid",
      "asks something about the bot itself" in CLASSIFICATION_PROMPT)
check("it mentions both languages", "Hebrew or in English" in CLASSIFICATION_PROMPT)
check("bare greetings are still noise", '"hi"' in CLASSIFICATION_PROMPT)
check("injection attempts are still noise", "prompt-injection attempt" in CLASSIFICATION_PROMPT)
check("the stale command list is gone from the prompt",
      "/add_session" not in CLASSIFICATION_PROMPT and "/delete_session" not in CLASSIFICATION_PROMPT)


# ---------------------------------------------------------------------
# 7) שחזור השיחה מהפרודקשן (14.9), עכשיו עם שפה שמורה
# ---------------------------------------------------------------------
# אותו משתמש, טלגרם מוגדר-אנגלית, שמור בעברית. "עברית" ו-"Switch
# language to Hebrew" הן בקשות שפה -> אישור בעברית. "שנה שפה לעברית" —
# גם. אף אחת לא אמורה להגיע ל-"I am already speaking Hebrew!".
for text in ("עברית", "שנה שפה לעברית", "Switch language to Hebrew"):
    msgs, _, saved = run_update(
        text, stored="he", username="Chatgil_0",
        extra_patches=(patch.object(bc, "classify_message", lambda t: "VALID"),
                       patch.object(bc, "run_agent_via_mcp", lambda task: "...")),
    )
    check(f"'{text}' -> confirmed in Hebrew", msgs and "בעברית" in msgs[0][0],
          f"-> {msgs[0][0][:30] if msgs else None}")

# שאלה חופשית באנגלית -> Agent Loop מתבקש לענות באנגלית
asked = {}
run_update(
    "What's the UV in Tel Aviv right now?", stored="he", username="Chatgil_0",
    extra_patches=(patch.object(bc, "classify_message", lambda t: "VALID"),
                   patch.object(bc, "run_agent_via_mcp", lambda task: asked.setdefault("task", task) or "...")),
)
check("an English question -> the agent answers in English", "באנגלית" in asked.get("task", ""))

task = bc._build_freeform_task("שנה שפה", "he")
check("the prompt states the truth: Hebrew and English", "עברית ואנגלית" in task)
check("the prompt points at /language", "/language" in task)
check("the prompt forbids inventing a settings screen", "אסור להמציא" in task)
check("the prompt forbids sending the user to the dashboard for language",
      "לדשבורד" in task and "בשביל שפה" in task)
check("the prompt forbids promising other languages", "אסור להבטיח שפות אחרות" in task)


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
print("All checks passed.")
