"""
בדיקה ידנית (לא pytest) לברירת המחדל של סוג העור (2026-09-16).

**מה שונה ולמה.** ברירת המחדל כשאין סוג עור שמור הייתה 3, עם ההערה
"ברירת מחדל זהירה". היא לא הייתה זהירה: 3 הוא *אמצע* סולם Fitzpatrick
(factor 1.0), כלומר הוא מניח עור שסובל חשיפה בינונית. עכשיו היא 1
(factor 0.5) — הסוג שנשרף הכי מהר, ולכן זה שמקבל את תקציב הזמן הקצר
ביותר ואת הציון הגבוה ביותר על אותה חשיפה בפועל.

הכיוון הזה הוא העיקר: באפליקציה שמטרתה למנוע כוויה, טעות לכיוון
"תמרח קרם" עדיפה על טעות לכיוון "אתה בסדר". ברירת מחדל באמצע הסולם
מבטיחה למשתמש בהיר-עור בערך כפול מהזמן שבאמת בטוח לו.

מה שלא שונה, בכוונה: SKIN_TYPE_FACTOR.get(skin_type, 1.0). הנפילה
*הזו* חלה רק על ערך מחוץ לטווח 1–6, ועל זה יש check constraint ב-DB
בשתי הטבלאות — כלומר היא רשת ביטחון שלא נדרכת בפועל. שינוי שלה היה
נוגע בחמישה מימושים של הנוסחה ובכל ה-fixture, בלי להשפיע על אף משתמש.

לא נוגעים ברשת.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bot_commands as bc
from geo_uv_core import SKIN_TYPE_FACTOR, safe_exposure_minutes

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1. הכיוון: 1 אכן מחמיר יותר מ-3
# ---------------------------------------------------------------------
check("factor של סוג 1 קטן מזה של סוג 3",
      SKIN_TYPE_FACTOR[1] < SKIN_TYPE_FACTOR[3],
      f"({SKIN_TYPE_FACTOR[1]} < {SKIN_TYPE_FACTOR[3]})")

t1, t3 = safe_exposure_minutes(7.0, 1), safe_exposure_minutes(7.0, 3)
check("ולכן תקציב הזמן קצר יותר", t1 < t3, f"({t1:.0f} דק' מול {t3:.0f} דק' ב-UV 7)")
check("והוא בדיוק חצי — מי שקיבל 3 בטעות קיבל כפול מהזמן",
      abs(t1 * 2 - t3) < 1e-9)

# ---------------------------------------------------------------------
# 2. handle_end_session בלי שורת users -> מניח 1
# ---------------------------------------------------------------------
START = datetime.now(timezone.utc) - timedelta(minutes=60)
SESSION = {
    "id": 1, "telegram_username": "nobody", "city": "חיפה", "country": "ישראל",
    "start_time": START.isoformat(), "end_time": None,
    "uv_index": 7.0, "spf": None, "exposure_score": None, "lat": None, "lon": None,
}


def _score_with(users_row):
    """מריץ /end_session ומחזיר את ה-exposure_score שנכתב ל-DB."""
    written = {}

    def fake_select(table, params):
        if table == "exposure_log":
            return [SESSION]
        if table == "users":
            return [users_row] if users_row else []
        raise AssertionError(table)

    with patch.object(bc, "select_rows", fake_select), \
         patch.object(bc, "update_rows", lambda t, p, patch_, **k: written.update(patch_) or [SESSION]), \
         patch.object(bc, "send_message", lambda *a, **k: None), \
         patch.object(bc, "fetch_historical_uv", lambda *a, **k: None):
        bc.handle_end_session(1, "nobody", "")
    return written.get("exposure_score")


no_user = _score_with(None)
as_three = _score_with({"telegram_username": "nobody", "skin_type": 3})
as_one = _score_with({"telegram_username": "nobody", "skin_type": 1})

check("בלי שורת users הציון שווה לזה של סוג עור 1", no_user == as_one,
      f"(בלי users: {no_user}, כסוג 1: {as_one})")
check("ו*לא* לזה של סוג עור 3", no_user != as_three, f"(כסוג 3 היה יוצא {as_three})")
check("כלומר ברירת המחדל מחמירה, לא מקלה", no_user > as_three,
      f"({no_user}% מול {as_three}%)")

# ---------------------------------------------------------------------
# 3. סוג עור שמור תמיד גובר על ברירת המחדל
# ---------------------------------------------------------------------
check("סוג עור 6 שמור לא נדרס ע\"י ברירת המחדל",
      _score_with({"telegram_username": "nobody", "skin_type": 6}) < as_one)

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
