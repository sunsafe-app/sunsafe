"""
מחלץ מהפייתון את הפלט האמיתי ומייצר fixture ל-logic.test.ts.

הרעיון: לא לכתוב ביד את הציפיות בבדיקות ה-TypeScript אלא לגזור אותן
מהמימוש בפייתון, שהוא מקור האמת. אם מישהו ישנה את הנוסחה או את נוסח
ההודעה בצד אחד, הבדיקה של ה-Worker תיפול.

הרצה (משורש הריפו):
    python cloudflare/sunset-worker/scripts/gen_fixture.py
"""
import json
import os
import pathlib
import sys

os.environ.setdefault("BOT_TOKEN", "FIXTURE")
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from geo_uv_core import calculate_exposure_score, safe_exposure_minutes  # noqa: E402
import bot_commands as bc  # noqa: E402

CASES = []
for uv in (0, 0.4, 1.0, 3.3, 6.3, 6.9, 7.6, 11.5):
    for skin in range(1, 7):
        for spf in (None, 30, 50):
            for duration in (0, 1, 17, 45, 150):
                CASES.append({
                    "uv": uv, "skin": skin, "spf": spf, "duration": duration,
                    "score": calculate_exposure_score(uv, duration, skin, spf),
                    "safeMinutes": safe_exposure_minutes(uv, skin, spf),
                })

DURATIONS = [
    {"minutes": m, "text": bc.format_duration_he(m)}
    for m in (0, 1, 12, 32, 59, 60, 61, 65, 90, 119, 120, 125, 180, 200, 400, 1439)
]

# הודעות סיום מלאות — הנוסח המדויק שהבוט שולח.
MESSAGES = []
for city, duration, uv, skin, spf, is_avg in (
    ("ירוחם", 45, 6.3, 3, None, True),
    ("לפקדה", 0, 1.5, 2, None, True),
    ("קריית ים", 150, 7.4, 2, 30, False),
    ("אילת", 20, 0, 4, None, True),
):
    score = calculate_exposure_score(uv, duration, skin, spf)
    budget = safe_exposure_minutes(uv, skin, spf)
    uv_label = "UV ממוצע" if is_avg else "UV"
    budget_part = (
        f"\nמדד חשיפה: {score}% — {round(duration)} מתוך "
        f"{round(budget)} הדקות המותרות לכם ב-{uv_label} {uv:.1f}."
        if budget else f"\nמדד חשיפה: {score}%."
    )
    MESSAGES.append({
        "city": city, "durationMinutes": duration, "uvIndex": uv,
        "skinType": skin, "spf": spf, "uvIsAverage": is_avg,
        "text": f"{round(duration)} דקות ב{city}.{budget_part}\n\n"
                "כדי לראות את הנתונים באזור האישי — לחצו\n/dashboard",
    })

out = pathlib.Path(__file__).resolve().parent.parent / "src" / "fixture.json"
out.write_text(json.dumps(
    {"scores": CASES, "durations": DURATIONS, "messages": MESSAGES},
    ensure_ascii=False, indent=2,
) + "\n", encoding="utf-8")
print(f"wrote {out} — {len(CASES)} score cases, {len(DURATIONS)} durations, {len(MESSAGES)} messages")
