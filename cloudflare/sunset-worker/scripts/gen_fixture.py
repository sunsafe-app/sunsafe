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

from geo_uv_core import (  # noqa: E402
    calculate_exposure_score,
    daily_summary_he,
    exposure_bar,
    safe_exposure_minutes,
)
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

# הסרגל והבלוק היומי (נוסף 16.9.2026). בלעדיהם ה-Worker היה שולח
# הודעה בלי הסיכום היומי, ומי שה-session שלו נסגר בשקיעה היה מקבל
# משהו אחר ממי שסגר ידנית.
BARS = [{"score": sc, "bar": exposure_bar(sc)} for sc in (
    0, 1, 3, 12, 25, 39, 40, 41, 55, 69, 70, 71, 85, 99, 100, 101, 145, 400,
)]

DAILY_BLOCKS = []
for day_score, count, minutes, peak_city, peak_score in (
    (12, 1, 18, "חיפה", 12),
    (82, 3, 71.4, "חיפה", 47),
    (100, 2, 120, "אילת", 60),
    (145, 2, 210.5, "אילת", 90),
    (0, 1, 0, "טוקיו", 0),
    (47, 2, 55, None, None),
):
    DAILY_BLOCKS.append({
        "dayScore": day_score, "sessionCount": count, "totalMinutes": minutes,
        "peakCity": peak_city, "peakScore": peak_score,
        "text": daily_summary_he(day_score, count, minutes, peak_city, peak_score),
    })

# הודעות סיום מלאות — הנוסח המדויק שהבוט שולח.
MESSAGES = []
for city, duration, uv, skin, spf, is_avg, daily in (
    ("ירוחם", 45, 6.3, 3, None, True, None),
    ("לפקדה", 0, 1.5, 2, None, True, None),
    ("קריית ים", 150, 7.4, 2, 30, False, (105, 3, 75, "אילת", 40)),
    ("אילת", 20, 0, 4, None, True, (33, 2, 40, "אילת", 20)),
):
    score = calculate_exposure_score(uv, duration, skin, spf)
    budget = safe_exposure_minutes(uv, skin, spf)
    uv_label = "UV ממוצע" if is_avg else "UV"
    budget_part = (
        f"\nמדד חשיפה: {score}% — {round(duration)} מתוך "
        f"{round(budget)} הדקות המותרות לכם ב-{uv_label} {uv:.1f}."
        if budget else f"\nמדד חשיפה: {score}%."
    )
    daily_part = "\n\n" + daily_summary_he(*daily) if daily else ""
    MESSAGES.append({
        "city": city, "durationMinutes": duration, "uvIndex": uv,
        "skinType": skin, "spf": spf, "uvIsAverage": is_avg,
        "daily": None if daily is None else {
            "score": daily[0], "sessionCount": daily[1], "totalMinutes": daily[2],
            "peakCity": daily[3], "peakScore": daily[4],
        },
        "text": f"{round(duration)} דקות ב{city}.{budget_part}{daily_part}\n\n"
                "כדי לראות את הנתונים באזור האישי — לחצו\n/dashboard",
    })

out = pathlib.Path(__file__).resolve().parent.parent / "src" / "fixture.json"
out.write_text(json.dumps(
    {"scores": CASES, "durations": DURATIONS, "bars": BARS,
     "dailyBlocks": DAILY_BLOCKS, "messages": MESSAGES},
    ensure_ascii=False, indent=2,
) + "\n", encoding="utf-8")
print(f"wrote {out} — {len(CASES)} score cases, {len(DURATIONS)} durations, "
      f"{len(BARS)} bars, {len(DAILY_BLOCKS)} daily blocks, {len(MESSAGES)} messages")
