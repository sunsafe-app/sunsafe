"""
בדיקה ידנית (לא pytest) ל-/start_session עם קואורדינטות (2026-09-16).

**הבאג שהוביל לזה, מלוגים של פרודקשן.** משתמש שלח
"/start_session 28.84 42.9" וקיבל שתיקה. בלוג:

    File "/app/bot_commands.py", line 1644, in handle_start_session
        geo["latitude"],
    KeyError: 'latitude'

reverse_geocode_location מחזירה **שלושה מפתחות בלבד** —
{"found", "name", "country"} — ואין בהם קואורדינטות. זה הגיוני: העברנו
לה lat/lon, אין לה מה להחזיר אותם. אבל הקוד ניגש ל-geo["latitude"]
כאילו זו התשובה של geocode_city (שכן מחזירה קואורדינטות, כי היא
מקבלת שם עיר). שני נתיבים, שתי צורות החזרה, ואחד מהם השתמש בצורה
של השני.

**ולמה זה הגיע לפרודקשן:** אף בדיקה לא כיסתה את נתיב הקואורדינטות.
חיפוש של _COORDINATE_ARGS_RE בכל tests/ החזיר אפס. הנתיב הזה נוסף
ב-15.9 ("אפשר גם לשלוח קואורדינטות ישירות") ומעולם לא הורץ בבדיקה.

הבדיקה המרכזית כאן היא #3: **geo שאין בו קואורדינטות כלל לא שובר
כלום, וה-lat/lon שמגיעים ל-DB הם אלה שהמשתמש הקליד.**

לא נוגעים ברשת.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import httpx

import bot_commands as bc

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1. הביטוי הרגולרי — שלושת הפורמטים שהוצהרו, ולא יותר
# ---------------------------------------------------------------------
for text, expected in [
    ("32.08,34.78", ("32.08", "34.78")),
    ("32.08, 34.78", ("32.08", "34.78")),
    ("32.08 34.78", ("32.08", "34.78")),
    ("-33.87, 151.21", ("-33.87", "151.21")),
    ("28.84 42.9", ("28.84", "42.9")),
]:
    m = bc._COORDINATE_ARGS_RE.match(text)
    check(f"נקלט: {text!r}", m is not None and m.groups() == expected,
          f"-> {m.groups() if m else None}")

for text in ["תל אביב", "באר שבע 7", "Rome", "32.08", "", "32.08, 34.78, 5"]:
    check(f"לא נקלט כקואורדינטה: {text!r}", bc._COORDINATE_ARGS_RE.match(text) is None)


# ---------------------------------------------------------------------
# 2-3. הנתיב המלא, עם geo שאין בו קואורדינטות — הצורה האמיתית
# ---------------------------------------------------------------------
def _run(location_text, geo_result, uv=6.5):
    """מריץ handle_start_session ומחזיר (הודעות, הארגומנטים ל-_begin_session)."""
    sent, begun = [], []

    class _Client:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    with patch.object(bc.httpx, "Client", _Client), \
         patch.object(bc, "_can_start_session", lambda *a, **k: True), \
         patch.object(bc, "reverse_geocode_location", lambda c, la, lo: geo_result), \
         patch.object(bc, "get_current_uv", lambda c, la, lo: uv), \
         patch.object(bc, "send_message", lambda chat_id, text, **k: sent.append(text)), \
         patch.object(bc, "_begin_session", lambda *a, **k: begun.append(a)):
        bc.handle_start_session(1, "tester", location_text)
    return sent, begun


# זו הצורה האמיתית של reverse_geocode_location — בלי latitude/longitude.
GEO_FOUND = {"found": True, "name": "ג'רבלוס", "country": "סוריה"}

sent, begun = _run("28.84 42.9", GEO_FOUND)
check("geo בלי קואורדינטות לא מפיל את הנתיב", len(begun) == 1, f"-> sent={sent}")
if begun:
    chat_id, username, city, country, uv_index, lat, lon = begun[0]
    check("ה-lat שנכתב הוא זה שהמשתמש הקליד", lat == 28.84, f"-> {lat}")
    check("וה-lon כנ\"ל", lon == 42.9, f"-> {lon}")
    check("שם התצוגה מגיע מה-reverse geocoding", city == "ג'רבלוס", f"-> {city}")
    check("וגם המדינה", country == "סוריה", f"-> {country}")
    check("וה-UV לא None", uv_index == 6.5, f"-> {uv_index}")

# ---------------------------------------------------------------------
# 4. reverse geocoding שנכשל — ה-session עדיין נפתח
# ---------------------------------------------------------------------
sent, begun = _run("28.84 42.9", {"found": False})
check("לא נמצאה עיר -> ה-session עדיין נפתח", len(begun) == 1, f"-> sent={sent}")
if begun:
    city, lat, lon = begun[0][2], begun[0][5], begun[0][6]
    check("ושם התצוגה הוא הקואורדינטות עצמן", "28.84" in city and "42.9" in city, f"-> {city}")
    check("והקואורדינטות נשמרות במדויק", (lat, lon) == (28.84, 42.9), f"-> {lat},{lon}")

# ---------------------------------------------------------------------
# 5. קואורדינטות מחוץ לטווח — הודעה, בלי session
# ---------------------------------------------------------------------
for bad in ["91.0, 34.78", "32.08, 181.0", "-91, 0", "0, -181"]:
    sent, begun = _run(bad, GEO_FOUND)
    check(f"נדחה: {bad!r}", begun == [] and len(sent) == 1 and "לא תקינות" in sent[0],
          f"-> begun={len(begun)} sent={sent}")

# הגבולות עצמם כן חוקיים
sent, begun = _run("90 180", GEO_FOUND)
check("הגבולות 90/180 חוקיים", len(begun) == 1, f"-> sent={sent}")

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
