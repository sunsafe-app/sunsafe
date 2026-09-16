"""
בדיקה ידנית (לא pytest) להצגת "כמה זמן אפשר להיות בשמש" (2026-09-15).

הרקע — משוב מבדיקת הצוות: משתמש פתח session בירוחם ב-UV 6.3 עם סוג עור
3, וכתב "אני לא רואה שום דבר חשוב מעבר ל'תמרח 50', יכולתי להבין את זה
לבד". הוא צדק: הנוסחה חישבה באותו רגע שמדובר ב-32 דקות *בשבילו*,
והמספר נזרק מיד. המשתמש קיבל רק אחוז מתקציב שלא הוצג לו מעולם.

מה נבדק כאן:
1. הרפקטור לא שינה ולו ציון אחד — safe_exposure_minutes הופרד מתוך
   calculate_exposure_score, וזו הבדיקה שמוכיחה שהפיצול נאמן למקור.
2. המספר עצמו נכון, כולל המקרה האמיתי של אותו משתמש.
3. **תקרת השעתיים** — הסעיף הבטיחותי. לפי הנוסחה הנוכחית SPF 50 על
   סוג עור 6 נותן 34 שעות, ולהבטיח דבר כזה למשתמש זה נזק. שום פלט
   לא אמור לנקוב בזמן ארוך יותר ממרווח המריחה החוזרת.
4. הניסוח בעברית לא מייצר "כ-1 שעות" ודומיו.
5. ההודעות עצמן, מקצה לקצה, באמת מכילות את השורה.
"""
import os
import sys
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_commands as bc
from geo_uv_core import SKIN_TYPE_FACTOR, effective_spf, safe_exposure_minutes

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1) הרפקטור נאמן למקור — הנוסחה המקורית, כתובה כאן במפורש
# ---------------------------------------------------------------------
def original_score(uv_index, duration_minutes, skin_type, spf):
    """הנוסחה כפי שהייתה לפני הפיצול, שוכפלה בכוונה כדי להשוות מולה."""
    if uv_index <= 0:
        return 0
    factor = SKIN_TYPE_FACTOR.get(skin_type, 1.0)
    protection = effective_spf(spf)
    safe = (200 / uv_index) * factor * protection
    return round((duration_minutes / safe) * 100)


mismatches = []
for uv in (0, 0.4, 1.0, 3.3, 6.3, 7.6, 8.0, 11.5):
    for st in range(1, 7):
        for spf in (None, 15, 30, 50):
            for duration in (0, 1, 17, 45, 120, 480):
                got = bc.calculate_exposure_score(uv, duration, st, spf)
                want = original_score(uv, duration, st, spf)
                if got != want:
                    mismatches.append((uv, st, spf, duration, got, want))

check("the refactor changes no score at all (1152 combinations)",
      not mismatches, f"-> {mismatches[:3]}")

# סוג עור לא מוכר נופל ל-1.0 בשתי הגרסאות
check("unknown skin type behaves as before",
      bc.calculate_exposure_score(7.0, 60, 99, None) == original_score(7.0, 60, 99, None))

# ---------------------------------------------------------------------
# 2) המספר עצמו
# ---------------------------------------------------------------------
check("the real reported case: Yeruham, UV 6.3, skin type 3 -> 32 minutes",
      round(safe_exposure_minutes(6.3, 3)) == 32,
      f"-> {safe_exposure_minutes(6.3, 3)}")
check("fairer skin gets less time", safe_exposure_minutes(6.3, 1) < safe_exposure_minutes(6.3, 3))
check("darker skin gets more time", safe_exposure_minutes(6.3, 6) > safe_exposure_minutes(6.3, 3))
check("higher UV shortens the time", safe_exposure_minutes(10.0, 3) < safe_exposure_minutes(6.3, 3))
check("UV 0 -> None, not infinity", safe_exposure_minutes(0, 3) is None)

# ---------------------------------------------------------------------
# 3) תקרת השעתיים — הסעיף הבטיחותי
# ---------------------------------------------------------------------
over_promises = []
for uv in (0.5, 1, 2, 3, 4, 5, 6.3, 7.6, 9, 11, 13):
    for st in range(1, 7):
        line = bc.safe_exposure_line(uv, st)
        if not line:
            continue
        # כל אזכור של קרם הגנה בשורה חייב או לא לנקוב בזמן, או לנקוב
        # בזמן שאינו עולה על מרווח המריחה החוזרת.
        spf_minutes = safe_exposure_minutes(uv, st, 30)
        if spf_minutes > bc.SUNSCREEN_REAPPLY_MINUTES:
            # מעל תקרת המריחה החוזרת אסור לנקוב במספר כלל.
            if "מאריך את הזמן הזה —" not in line:
                over_promises.append((uv, st, line))
check("no output ever promises more sunscreen time than the reapply interval",
      not over_promises, f"-> {over_promises[:2]}")

check("every line carries the two-hour reapply interval",
      all("כל שעתיים" in (bc.safe_exposure_line(uv, st) or "כל שעתיים")
          for uv in (1, 6.3, 11) for st in range(1, 7)))

# הניסוח נכתב מחדש ב-2026-09-15 (ראו ההודעה הראשונה שנשלחה בפועל).
# הבדיקה נועלת את החלק שאסור להתרכך בעריכות עתידיות: מרווח מפורש,
# ולא "בהתאם לצורך".
vague = [bc.safe_exposure_line(uv, st) for uv in (1, 6.3, 11) for st in range(1, 7)
         if "בהתאם לצורך" in (bc.safe_exposure_line(uv, st) or "")]
check("the reapply interval is never softened to 'as needed'", not vague, f"-> {vague[:1]}")

# ---------------------------------------------------------------------
# 4) ניסוח עברי
# ---------------------------------------------------------------------
cases = {
    12: "כ-12 דקות", 32: "כ-32 דקות", 59: "כ-59 דקות",
    60: "כשעה", 65: "כשעה ו-5 דקות", 120: "כשעתיים",
    125: "כשעתיים ו-5 דקות", 200: "כ-3 שעות ו-20 דקות",
}
for minutes, expected in cases.items():
    check(f"format_duration_he({minutes})", bc.format_duration_he(minutes) == expected,
          f"-> {bc.format_duration_he(minutes)}")

bad_phrasing = [m for m in range(1, 600)
                if "כ-1 שעות" in bc.format_duration_he(m) or "כ-2 שעות" in bc.format_duration_he(m)]
check("never produces 'כ-1 שעות' or 'כ-2 שעות'", not bad_phrasing, f"-> {bad_phrasing[:3]}")

check("no line ends up empty when there is no skin type",
      bc.safe_exposure_line(6.3, None) is None and bc.safe_exposure_line(6.3, 0) is None)

# ---------------------------------------------------------------------
# 5) ההודעות עצמן
# ---------------------------------------------------------------------
sent = []


def fake_send_message(chat_id, text, **kwargs):
    sent.append(text)


# /start_session
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "insert_row", lambda *a, **k: {"id": 1}), \
     patch.object(bc, "select_rows", lambda *a, **k: [{"telegram_username": "omri", "skin_type": 3}]), \
     patch.object(bc, "send_uv_forecast_chart", lambda *a, **k: None):
    sent.clear()
    bc._begin_session(670212669, "omri", "ירוחם", "Israel", 6.3, 30.99, 34.93)

check("/start_session message includes the time-in-sun line",
      sent and "32 דקות בשמש ישירה" in sent[0], f"-> {sent}")
check("/start_session shows the UV on its own line",
      sent and "UV נוכחי: 6.3" in sent[0], f"-> {sent}")
check("/start_session still tells the user how to end the session",
      sent and "/end_session" in sent[0])

# משתמש בלי סוג עור שמור — ההודעה עדיין נשלחת, רק בלי השורה
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "insert_row", lambda *a, **k: {"id": 1}), \
     patch.object(bc, "select_rows", lambda *a, **k: []), \
     patch.object(bc, "send_uv_forecast_chart", lambda *a, **k: None):
    sent.clear()
    bc._begin_session(1, "nobody", "ירוחם", None, 6.3, 30.99, 34.93)
check("no stored skin type -> confirmation still sent, just without the line",
      sent and "התחלת session" in sent[0] and "בשמש ישירה" not in sent[0], f"-> {sent}")

# כשל ב-DB בשליפת סוג העור לא מפיל את האישור
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "insert_row", lambda *a, **k: {"id": 1}), \
     patch.object(bc, "select_rows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))), \
     patch.object(bc, "send_uv_forecast_chart", lambda *a, **k: None):
    sent.clear()
    crashed = False
    try:
        bc._begin_session(1, "omri", "ירוחם", None, 6.3, 30.99, 34.93)
    except Exception:
        crashed = True
check("a DB failure while fetching skin type does not break /start_session",
      not crashed and sent and "התחלת session" in sent[0], f"-> crashed={crashed}, {sent}")

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    raise SystemExit(1)
print("All checks passed.")
