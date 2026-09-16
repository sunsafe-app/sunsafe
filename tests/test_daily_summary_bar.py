"""
בדיקה ידנית (לא pytest) לסיכום היומי ולסרגל הצבעים (2026-09-16).

**השינוי המושגי.** עד כאן "ציון של יום" היה **המקסימום** מבין ה-
sessions — _peak_exposure_session בבוט, dayScoreOf בדשבורד. זו הגדרה
שמקטינה סיכון: נזק UV מצטבר, ומי שיצא שלוש פעמים לחצי שעה חטף את
שלושתן. שלושה sessions של 40% הם 120% מהתקציב היומי, לא 40%.

**הצבע לא לבד, בכוונה.** ירוק-מול-אדום הוא הצמד שדויטרנופיה לא
מבדילה (~8% מהגברים), ובהודעת טלגרם אין כפתור נגישות להציע כמו
בדשבורד. לכן כל פלט נושא שלושה ערוצים: מיקום המילוי בסרגל, המספר,
ושם הרמה במילים. הבדיקות למטה נועלות את שלושתם.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geo_uv_core import (
    DAILY_BAR_CELLS,
    daily_exposure_score,
    daily_summary_he,
    exposure_bar,
    score_to_level,
)

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1. הצבירה — סכום, לא מקסימום. זה כל השינוי.
# ---------------------------------------------------------------------
check("שלושה sessions של 40% נותנים 120%, לא 40%",
      daily_exposure_score([40, 40, 40]) == 120)
check("session פתוח (None) לא נכלל", daily_exposure_score([40, None, 20]) == 60)
check("יום בלי sessions סגורים -> 0", daily_exposure_score([None, None]) == 0)
check("יום ריק -> 0", daily_exposure_score([]) == 0)
check("session בודד מחזיר את עצמו", daily_exposure_score([73]) == 73)

# ---------------------------------------------------------------------
# 2. הסרגל — אורך קבוע, ומילוי שגדל עם הציון
# ---------------------------------------------------------------------
import unicodedata


def cells(bar):
    """סופר ריבועים, לא תווים — כל ריבוע הוא נקודת קוד אחת אבל בטוח לסמוך על הפירוק."""
    return [c for c in bar if unicodedata.category(c) == "So"]


for score in (0, 1, 39, 40, 69, 70, 99, 100, 250):
    check(f"אורך הסרגל קבוע ב-{score}%", len(cells(exposure_bar(score))) == DAILY_BAR_CELLS,
          f"-> {len(cells(exposure_bar(score)))}")

filled = lambda sc: DAILY_BAR_CELLS - exposure_bar(sc).count("⬜")
check("המילוי מונוטוני עולה",
      all(filled(a) <= filled(b) for a, b in zip(range(0, 100, 7), range(7, 107, 7))))
check("0% -> סרגל ריק לגמרי", filled(0) == 0)
check("ציון חיובי קטן -> תא אחד לפחות, לא אפס", filled(3) >= 1)
check("99% -> לא מלא עדיין", filled(99) < DAILY_BAR_CELLS, f"-> {filled(99)}")

# ---------------------------------------------------------------------
# 3. הבאנדים — ירוק/צהוב/כתום מתאימים ל-score_to_level
# ---------------------------------------------------------------------
check("39% — ירוק בלבד", set(cells(exposure_bar(39))) <= {"🟩", "⬜"}, f"-> {exposure_bar(39)}")
check("ו-39% הוא אכן good", score_to_level(39) == "good")
check("55% — כבר יש צהוב", "🟨" in exposure_bar(55), f"-> {exposure_bar(55)}")
check("85% — כבר יש כתום", "🟧" in exposure_bar(85), f"-> {exposure_bar(85)}")
check("ואין אדום מתחת ל-100", "🟥" not in exposure_bar(99))

# ---------------------------------------------------------------------
# 4. 100% ומעלה — כולו אדום. "אדום זה חשיפה מלאה".
# ---------------------------------------------------------------------
check("100% -> כולו אדום", exposure_bar(100) == "🟥" * DAILY_BAR_CELLS)
check("145% -> גם כן כולו אדום", exposure_bar(145) == "🟥" * DAILY_BAR_CELLS)
check("ואין תא ריק כשעברת את התקציב", "⬜" not in exposure_bar(100))

# ---------------------------------------------------------------------
# 5. הטקסט — שלושה ערוצי מידע, לא רק צבע
# ---------------------------------------------------------------------
text = daily_summary_he(82, 3, 71.4, "חיפה", 47)
check("המספר מופיע כטקסט", "82%" in text)
check("שם הרמה מופיע במילים", "בטווח הגבוה" in text)
check("וגם כמה נשאר עד חשיפה מלאה", "עוד 18%" in text)
check("הדקות בשמש מופיעות", "71 דקות בשמש" in text, f"-> {text!r}")
# **הבדיקה שנועלת את מה שהוסר.** בבדיקה אמיתית יצא "16 sessions · 23
# דקות בשמש" — 1.4 דקות לכל session. המספר מודד כמה פעמים נלחץ
# /start_session, לא כמה שמש נספגה, והוא הוריד את העין מהדקות.
check("ומספר ה-sessions *לא* מופיע", "sessions" not in text and "session ·" not in text,
      f"-> {text!r}")
check("וה-session הגבוה", "הגבוה ביותר: חיפה, 47%" in text)

one = daily_summary_he(12, 1, 18)
check("session בודד -> רק דקות, בלי מספר", "היום: 18 דקות בשמש" in one, f"-> {one!r}")
check("ובלי שורת 'הגבוה ביותר' כשיש רק אחד", "הגבוה ביותר" not in one)

over = daily_summary_he(145, 2, 210, "אילת", 90)
check("מעל 100% -> נוסח של חשיפה מלאה", "חשיפה מלאה" in over)
check("ולא מבטיחים 'עוד X% עד'", "עד חשיפה מלאה" not in over, f"-> {over!r}")

check("כל רמה מקבלת נוסח משלה",
      len({daily_summary_he(s, 1, 10).split("\n")[1] for s in (10, 50, 80, 120)}) == 4)


# ---------------------------------------------------------------------
# 6. מקצה לקצה — הודעת /end_session מכילה את הבלוק היומי
# ---------------------------------------------------------------------
# והצבירה חייבת להיות זהות לזו של /today, שכבר סכם. שתי הדרכים
# משתמשות באותם _sessions_on_date ו-_peak_exposure_session בכוונה.
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bot_commands as bc

NOW = datetime.now(timezone.utc)


def _closed(id_, score, minutes, city):
    start = NOW - timedelta(minutes=minutes + 5)
    return {"id": id_, "telegram_username": "tester", "city": city, "country": "ישראל",
            "start_time": start.isoformat(), "end_time": (start + timedelta(minutes=minutes)).isoformat(),
            "uv_index": 7.0, "spf": None, "exposure_score": score, "lat": None, "lon": None}


def _open(id_, minutes, city):
    return {"id": id_, "telegram_username": "tester", "city": city, "country": "ישראל",
            "start_time": (NOW - timedelta(minutes=minutes)).isoformat(), "end_time": None,
            "uv_index": 7.0, "spf": None, "exposure_score": None, "lat": None, "lon": None}


def _end_session_text(day_rows):
    """מריץ handle_end_session ומחזיר את טקסט ההודעה שנשלחה."""
    sent = []
    open_row = _open(99, 30, "חיפה")

    def fake_select(table, params):
        if table == "users":
            return [{"telegram_username": "tester", "skin_type": 2}]
        if table == "exposure_log":
            if params.get("end_time") == "is.null":
                return [open_row]
            return day_rows + [dict(open_row, end_time=NOW.isoformat(), exposure_score=30)]
        raise AssertionError(table)

    with patch.object(bc, "select_rows", fake_select), \
         patch.object(bc, "update_rows", lambda *a, **k: [open_row]), \
         patch.object(bc, "send_message", lambda chat_id, text, **k: sent.append(text)), \
         patch.object(bc, "fetch_historical_uv", lambda *a, **k: None):
        bc.handle_end_session(1, "tester", "")
    return sent[0] if sent else ""


text = _end_session_text([_closed(1, 40, 25, "אילת"), _closed(2, 35, 20, "ירושלים")])
check("הודעת הסיום כוללת סרגל צבעים", any(c in text for c in "🟩🟨🟧🟥"), f"-> {text!r}")
check("והצבירה היא סכום: 40+35+30 = 105", "105%" in text, f"-> {text!r}")
check("ובלי מספר sessions", "sessions" not in text)
check("כלומר עברה את 100 -> כולה אדומה", "🟥" * 10 in text)
check("והנוסח הוא של חשיפה מלאה", "חשיפה מלאה" in text)
check("עדיין מופיעה התוצאה של ה-session עצמו", "דקות בחיפה" in text)
check("וגם ההפניה לדשבורד", "/dashboard" in text)

text = _end_session_text([])
check("יום עם session בודד -> סרגל לפי אותו ציון", "30%" in text, f"-> {text!r}")
check("ובלי שורת 'הגבוה ביותר'", "הגבוה ביותר" not in text)

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
