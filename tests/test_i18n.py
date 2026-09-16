"""
בדיקה ידנית (לא pytest) לתמיכה הדו-לשונית (2026-09-14).

שתי תקלות פרודקשן הובילו לקובץ הזה, שתיהן באותו יום:
1. משתמש שאל "English?" — הגייטקיפר סיווג NOISE והשתיק אותו לגמרי.
2. הגרסה הראשונה של התיקון נשענה על language_code של לקוח הטלגרם,
   אז אותו משתמש (טלגרם מוגדר-אנגלית) כתב "עברית" ואז "שנה שפה
   לעברית" — וקיבל אנגלית בשתי הפעמים, כולל "I am already speaking
   Hebrew!" שנכתב באנגלית. ראו את שחזור השיחה בסעיף 7.

ובסוף אותו יום התמיכה באנגלית **כובתה** לבקשת המשתמש ("נחזור לזה
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
# 1) resolve_language — כרגע עברית תמיד (ENGLISH_ENABLED=False)
# ---------------------------------------------------------------------
# התמיכה באנגלית נבנתה ונכבתה באותו יום (2026-09-14). המתג ב-i18n.py
# מבטיח שאין מצב ביניים: כל עוד הוא כבוי, כל הודעה נענית בעברית ללא
# קשר לשפה שנכתבה בה או להגדרת הלקוח.
cases = {
    "עברית": "he",
    "hello": "he",                    # אנגלית כבויה -> עברית
    "Switch language to Hebrew": "he",
    "/start": "he",
    "/start_session Tel Aviv": "he",
    "14:30": "he",
    "": "he",
    None: "he",
}
for text, expected in cases.items():
    got = i18n.resolve_language(text)
    check(f"resolve_language({str(text)[:26]!r}) -> {expected}", got == expected, f"-> {got}")

check("English is switched off", i18n.ENGLISH_ENABLED is False)
check("Hebrew is the default", i18n.DEFAULT_LANGUAGE == "he")

# הזיהוי עצמו נשאר תקין *מתחת* למתג, כדי שההפעלה מחדש תהיה שינוי של
# שורה אחת ולא בנייה מחדש.
for text, expected in {"עברית": "he", "hello": "en", "/start": None, "14:30": None, "": None}.items():
    got = i18n.detect_language_from_text(text)
    check(f"detect_language_from_text({str(text)[:20]!r}) -> {expected}", got == expected, f"-> {got}")

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
# 3) onboarding — עברית בפועל, והתרגום האנגלי נשאר שמור לעתיד
# ---------------------------------------------------------------------
messages, photos = [], []
with patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append((text, reply_markup))), \
     patch.object(bc, "send_photo", lambda cid, img, caption=None, reply_markup=None: photos.append((caption, reply_markup))), \
     patch.object(bc, "_load_fitzpatrick_scale_image", lambda: b"png"), \
     patch.object(bc, "update_rows"), \
     patch.object(bc, "_mirror_incoming_to_admin", lambda *a, **k: None):
    bc._pending_skin_type_pick.clear()
    bc.handle_update({"message": {
        "chat": {"id": 123},
        "from": {"username": "gil612", "language_code": "en"},   # לקוח מוגדר-אנגלית
        "text": "/start",
    }})

check("an English-locale client still gets the Hebrew welcome",
      messages and "ברוכים הבאים" in messages[0][0], f"-> {messages[0][0][:40] if messages else None}")
labels = [b["text"] for row in photos[0][1]["inline_keyboard"] for b in row]
check("...and Hebrew buttons", "3 · בינוני" in labels, f"-> {labels[:3]}")
check("...with no English leaking through",
      not any("Medium" in l or "Fair" in l for l in labels), f"-> {labels}")

# לחיצה על כפתור — עברית גם אם הכפתור ישב על הודעה אנגלית מלפני הכיבוי.
messages, answered = [], []
with patch.object(bc, "upsert_row", lambda *a, **k: None), \
     patch.object(bc, "answer_callback_query", lambda qid, text=None: answered.append(text)), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append(text)):
    bc.handle_callback_query({
        "id": "q1", "data": "skin:4",
        "from": {"username": "gil612", "language_code": "en"},
        "message": {"chat": {"id": 123}, "caption": "🎨 Compare with your natural skin tone"},
    })
check("a button tap answers in Hebrew while English is off",
      messages and "נשמר: סוג עור 4 · זית" in messages[0], f"-> {messages[0][:45] if messages else None}")

# מי שאין לו GPS חייב לקבל כאן דרך חלופית: זה האישור היחיד בסוף
# ה-onboarding, וטלגרם לא מודיע לבוט כשמשתמש מסרב להרשאת מיקום —
# כלומר אין שום נקודה מאוחרת יותר שבה אפשר להציע לו משהו.
confirmation = messages[0] if messages else ""
check("the confirmation offers a no-GPS alternative", "אין GPS" in confirmation)
check("...naming the command, since a bare city name does NOT open a session",
      "/start_session חיפה" in confirmation, f"-> ...{confirmation[-70:]}")
check("...and showing the coordinates form too", "32.08, 34.78" in confirmation)

# התרגומים עצמם שמורים ותקינים — זה מה שיאפשר להדליק את המתג בחזרה.
check("the English translations are still intact underneath",
      i18n.t("welcome", "en").startswith("☀️ Welcome to SunSafe"))
check("...including the button labels", i18n.skin_type_label(3, "en") == "3 · Medium")

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
# המנגנון עצמו עדיין יודע אנגלית — רק אף אחד לא מבקש ממנו כרגע.
check("the machinery still supports English for when the switch returns",
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
# 7) שחזור השיחה מהפרודקשן, הודעה־הודעה
# ---------------------------------------------------------------------
# בדיוק שלוש ההודעות מצילום המסך, מאותו משתמש עם language_code="en".
# כל אחת מהן עוברת דרך handle_update האמיתי, ואנחנו בודקים באיזו שפה
# ה-Agent Loop התבקש לענות. בגרסה השבורה כל השלוש היו "ענה באנגלית";
# עכשיו, עם אנגלית כבויה, כולן בעברית.
conversation = [
    ("עברית", "he"),
    ("שנה שפה לעברית", "he"),
    ("Switch language to Hebrew", "he"),   # אנגלית כבויה -> עברית
]
for text, expected_lang in conversation:
    asked = {}

    def fake_agent(task):
        asked["task"] = task
        return "..."

    with patch.object(bc, "classify_message", lambda t: "VALID"), \
         patch.object(bc, "update_rows"), \
         patch.object(bc, "send_message", lambda *a, **k: None), \
         patch.object(bc, "_mirror_incoming_to_admin", lambda *a, **k: None), \
         patch.object(bc, "run_agent_via_mcp", fake_agent):
        bc.handle_update({
            "message": {
                "chat": {"id": 123},
                "from": {"username": "Chatgil_0", "language_code": "en"},
                "text": text,
            }
        })

    wanted = "ענה בעברית" if expected_lang == "he" else "ענה באנגלית"
    check(
        f"'{text}' -> the agent is asked to answer in {expected_lang}",
        wanted in asked.get("task", ""),
        f"-> {'ענה בעברית' if 'ענה בעברית' in asked.get('task','') else 'ענה באנגלית'}",
    )

# והמקור להזיה: ה-prompt חייב לאסור במפורש להמציא מתג שפה, אחרי
# ש-Gemini שלח משתמש "to the dashboard" כדי להחליף שפה — מסך שלא קיים.
task = bc._build_freeform_task("שנה שפה", "he")
check("the prompt states the truth: Hebrew only for now",
      "עובד בעברית בלבד" in task)
check("the prompt forbids inventing a language setting",
      "אין** הגדרת" in task and "אסור להמציא" in task)
check("the prompt forbids sending the user to the dashboard for language",
      "לדשבורד בשביל שפה" in task)
check("the prompt forbids promising languages the bot doesn't speak",
      "אסור להבטיח שפות" in task)


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
print("All checks passed.")
