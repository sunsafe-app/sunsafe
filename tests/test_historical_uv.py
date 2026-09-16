"""
בדיקה ידנית (לא pytest) לכלי get_historical_uv ב-mcp_weather_server.py
(2026-09-15).

הרקע: משתמש שאל "מה היה מזג האוויר במצפה רמון אתמול?" והבוט ענה שהוא
"אינו מנהל היסטוריה של ימים קודמים" — טענה לא נכונה. הסיבה הייתה
של-Agent Loop לא היה כלי היסטורי בכלל, ושהדוגמה "מה היה אתמול"
ב-prompt נוסחה כך שבלעה גם שאלות על *מקום* ולא רק על נתוני המשתמש.

מה נבדק כאן:
1. ניתוב לפי גיל התאריך — עד 92 יום ל-forecast API (כדי שתשובה על
   "אתמול" תהיה עקבית עם מה ש-/end_session חישב), מעבר לזה לארכיון.
2. גבולות: תאריך עתידי, תאריך לפני תחילת הארכיון, פורמט לא תקין.
3. כשל רשת -> found=false, בלי חריגה שמפילה את ה-Agent Loop.
4. התקציר עצמו: שיא, שעת שיא, ושעות אור בלבד.

הקריאה האמיתית ל-Open-Meteo לא נבדקת כאן (אין גישה לרשת בסביבת
הבדיקה) — httpx.AsyncClient מוחלף בכפיל שמחזיר תגובה מוכנה, כך שגוף
הפונקציה כן רץ במלואו: בניית הפרמטרים, בחירת ה-endpoint והפענוח.
"""
import asyncio
import os
import sys
from datetime import date, timedelta
from unittest.mock import patch

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-key")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_weather_server as mws

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# --------------------------------------------------------------------
# כפיל ל-httpx.AsyncClient — לוכד את ה-URL והפרמטרים, מחזיר תגובה מוכנה
# --------------------------------------------------------------------
CALLS = []


def make_fake_client(payload=None, raise_on_get=None):
    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, timeout=None):
            CALLS.append({"url": url, "params": params})
            if raise_on_get:
                raise raise_on_get
            return FakeResponse(payload)

    return lambda *a, **k: FakeAsyncClient()


def uv_payload(day: str, values):
    """values: רשימה של 24 ערכים שעתיים."""
    return {
        "hourly": {
            "time": [f"{day}T{h:02d}:00" for h in range(24)],
            "uv_index": values,
        }
    }


# עקומה ריאליסטית: אפס בלילה, שיא 8.4 ב-12:00
DAY_CURVE = [0.0] * 6 + [0.3, 1.4, 3.2, 5.6, 7.4, 8.1, 8.4, 7.9, 6.3, 4.1, 2.0, 0.6] + [0.0] * 6

TODAY = date.today()


def run(lat, lon, date_iso=None, days_back=None):
    return asyncio.run(mws.get_historical_uv(lat, lon, date_iso, days_back))


# --------------------------------------------------------------------
# 1) אתמול -> forecast API עם past_days, לא הארכיון
# --------------------------------------------------------------------
yesterday = (TODAY - timedelta(days=1)).isoformat()
CALLS.clear()
with patch.object(mws.httpx, "AsyncClient", make_fake_client(uv_payload(yesterday, DAY_CURVE))):
    result = run(30.61, 34.80, yesterday)

check("yesterday -> found", result.get("found") is True, f"-> {result.get('reason', '')}")
check("yesterday -> uses the forecast endpoint (consistent with /end_session)",
      CALLS and CALLS[0]["url"] == mws.OPEN_METEO_URL, f"-> {CALLS[0]['url'] if CALLS else None}")
check("yesterday -> past_days=1", CALLS and CALLS[0]["params"].get("past_days") == 1,
      f"-> {CALLS[0]['params'].get('past_days') if CALLS else None}")
check("yesterday -> source reported as 'forecast'", result.get("source") == "forecast")
check("yesterday -> peak value and hour", result.get("uv_max") == 8.4 and result.get("peak_hour") == "12:00",
      f"-> {result.get('uv_max')} at {result.get('peak_hour')}")
check("yesterday -> only daylight hours are returned",
      len(result["hourly"]["uv_index"]) == 12 and all(v > 0 for v in result["hourly"]["uv_index"]),
      f"-> {len(result['hourly']['uv_index'])} hours")

# --------------------------------------------------------------------
# 2) לפני שנה -> הארכיון, עם start_date/end_date
# --------------------------------------------------------------------
last_year = (TODAY - timedelta(days=365)).isoformat()
CALLS.clear()
with patch.object(mws.httpx, "AsyncClient", make_fake_client(uv_payload(last_year, DAY_CURVE))):
    result = run(30.61, 34.80, last_year)

check("a year ago -> found", result.get("found") is True, f"-> {result.get('reason', '')}")
check("a year ago -> uses the air-quality archive endpoint",
      CALLS and CALLS[0]["url"] == mws.OPEN_METEO_AIR_QUALITY_URL,
      f"-> {CALLS[0]['url'] if CALLS else None}")
check("a year ago -> start_date/end_date, not past_days",
      CALLS and CALLS[0]["params"].get("start_date") == last_year
      and "past_days" not in CALLS[0]["params"],
      f"-> {CALLS[0]['params'] if CALLS else None}")
check("a year ago -> source reported as 'archive'", result.get("source") == "archive")

# --------------------------------------------------------------------
# 3) הגבול המדויק: 92 יום = עדיין forecast, 93 = ארכיון
# --------------------------------------------------------------------
for age, expected_url, label in (
    (92, mws.OPEN_METEO_URL, "forecast"),
    (93, mws.OPEN_METEO_AIR_QUALITY_URL, "archive"),
):
    d = (TODAY - timedelta(days=age)).isoformat()
    CALLS.clear()
    with patch.object(mws.httpx, "AsyncClient", make_fake_client(uv_payload(d, DAY_CURVE))):
        run(30.61, 34.80, d)
    check(f"{age} days back -> {label} endpoint",
          CALLS and CALLS[0]["url"] == expected_url, f"-> {CALLS[0]['url'] if CALLS else None}")

# --------------------------------------------------------------------
# 4) גבולות שחייבים להחזיר found=false בלי לפנות לרשת
# --------------------------------------------------------------------
tomorrow = (TODAY + timedelta(days=1)).isoformat()
CALLS.clear()
with patch.object(mws.httpx, "AsyncClient", make_fake_client(uv_payload(tomorrow, DAY_CURVE))):
    result = run(30.61, 34.80, tomorrow)
check("future date -> found=false and points at get_uv_forecast",
      result.get("found") is False and "get_uv_forecast" in result.get("reason", ""),
      f"-> {result}")
check("future date -> no network call at all", CALLS == [], f"-> {CALLS}")

CALLS.clear()
with patch.object(mws.httpx, "AsyncClient", make_fake_client(uv_payload("2019-06-01", DAY_CURVE))):
    result = run(30.61, 34.80, "2019-06-01")
check("before the archive starts -> found=false", result.get("found") is False, f"-> {result}")
check("before the archive starts -> no network call", CALLS == [], f"-> {CALLS}")

CALLS.clear()
result = run(30.61, 34.80, "14/09/2025")
check("malformed date -> found=false, no crash", result.get("found") is False, f"-> {result}")
check("malformed date -> no network call", CALLS == [], f"-> {CALLS}")

# --------------------------------------------------------------------
# 5) כשל רשת -> found=false, לא חריגה (ה-Agent Loop לא אמור ליפול)
# --------------------------------------------------------------------
CALLS.clear()
crashed = False
try:
    with patch.object(mws.httpx, "AsyncClient",
                      make_fake_client(raise_on_get=RuntimeError("simulated network failure"))):
        result = run(30.61, 34.80, yesterday)
except Exception as e:
    crashed = True
    result = {"error": str(e)}
check("provider failure -> does not raise", crashed is False, f"-> {result}")
check("provider failure -> found=false with a reason",
      crashed is False and result.get("found") is False and result.get("reason"),
      f"-> {result}")

# --------------------------------------------------------------------
# 6) היום בלי נתונים בתגובה -> found=false ולא שיא מומצא
# --------------------------------------------------------------------
CALLS.clear()
empty = {"hourly": {"time": [f"{yesterday}T{h:02d}:00" for h in range(24)], "uv_index": [None] * 24}}
with patch.object(mws.httpx, "AsyncClient", make_fake_client(empty)):
    result = run(30.61, 34.80, yesterday)
check("all-null response -> found=false", result.get("found") is False, f"-> {result}")

# --------------------------------------------------------------------
# 7) הכלי רשום כ-MCP tool ולא רק מוגדר בקובץ
# --------------------------------------------------------------------
tools = asyncio.run(mws.mcp.list_tools())
names = {t.name for t in tools}
check("get_historical_uv is registered as an MCP tool", "get_historical_uv" in names, f"-> {sorted(names)}")
check("the five original tools are still registered",
      {"geocode_city", "get_current_uv", "calculate_exposure_score",
       "log_uv_reading", "get_uv_forecast"} <= names,
      f"-> {sorted(names)}")

# --------------------------------------------------------------------
# 8) days_back — הפרמטר שנוסף כדי שהמודל לא יחשב תאריכים בעצמו
#
#    הבאג שהוליד אותו (2026-09-15): על "מה היה ה-UV במצפה רמון אתמול?"
#    המודל שלח date_iso=2025-05-18 — תאריך מתוך ידיעת האימון שלו —
#    וקיבל UV אמיתי ליום שגוי בשנה וארבעה חודשים. עם days_back=1
#    החישוב קורה בשרת מול השעון האמיתי ואין מה לטעות בו.
# --------------------------------------------------------------------
CALLS.clear()
with patch.object(mws.httpx, "AsyncClient", make_fake_client(uv_payload(yesterday, DAY_CURVE))):
    result = run(30.61, 34.80, days_back=1)
check("days_back=1 resolves to yesterday's real date",
      result.get("found") is True and result.get("date") == yesterday,
      f"-> {result.get('date')} (expected {yesterday})")
check("days_back=1 -> days_ago echoed back as 1", result.get("days_ago") == 1, f"-> {result.get('days_ago')}")
check("days_back=1 -> forecast endpoint with past_days=1",
      CALLS and CALLS[0]["params"].get("past_days") == 1, f"-> {CALLS[0]['params'] if CALLS else None}")

CALLS.clear()
year_back_date = (TODAY - timedelta(days=365)).isoformat()
with patch.object(mws.httpx, "AsyncClient", make_fake_client(uv_payload(year_back_date, DAY_CURVE))):
    result = run(30.61, 34.80, days_back=365)
check("days_back=365 -> archive endpoint and the right date",
      result.get("date") == year_back_date and result.get("source") == "archive",
      f"-> {result.get('date')} / {result.get('source')}")

# שני הפרמטרים יחד, או אף אחד מהם — שגיאה מפורשת, בלי פנייה לרשת
for kwargs, label in (
    ({"date_iso": yesterday, "days_back": 1}, "both date_iso and days_back"),
    ({}, "neither date_iso nor days_back"),
):
    CALLS.clear()
    result = run(30.61, 34.80, **kwargs)
    check(f"{label} -> found=false", result.get("found") is False, f"-> {result}")
    check(f"{label} -> no network call", CALLS == [], f"-> {CALLS}")

# days_back=0 הוא "היום", וזה לא תפקידו של הכלי הזה
CALLS.clear()
result = run(30.61, 34.80, days_back=0)
check("days_back=0 -> found=false and points at get_current_uv",
      result.get("found") is False and "get_current_uv" in result.get("reason", ""), f"-> {result}")
check("days_back=0 -> no network call", CALLS == [], f"-> {CALLS}")

# --------------------------------------------------------------------
# 9) ה-prompt של ה-Agent Loop חייב למסור את התאריך של היום.
#    זה שורש הבאג: בלי זה המודל נופל על תחושת ה"עכשיו" מהאימון שלו.
# --------------------------------------------------------------------
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
import bot_commands as bc

prompt = bc._build_freeform_task("מה היה ה-UV במצפה רמון אתמול?")
check("the freeform prompt states today's date", TODAY.isoformat() in prompt,
      f"-> today {TODAY.isoformat()} missing from the prompt")
check("the freeform prompt spells out yesterday's date too", yesterday in prompt,
      f"-> yesterday {yesterday} missing from the prompt")
check("the freeform prompt tells the model to prefer days_back", "days_back" in prompt)
check("the freeform prompt tells the model to report the date back", "date" in prompt)

# --------------------------------------------------------------------
# 10) לוח העוגנים — הגרסה הראשונה של התיקון נקבה רק ב"אתמול", כך
#     ש"שלשום" או "בשבת" עדיין היו תלויים בחישוב של המודל. הלוח מוסר
#     את שבעת הימים האחרונים עם שם היום, כדי שהמודל רק יבחר שורה.
#
#     שמות הימים נבדקים מול חישוב עצמאי (isoweekday) ולא מול הטבלה
#     שבקוד — טבלת שמות ימים בעברית מול weekday() של פייתון היא מקום
#     קלאסי לשגיאת off-by-one, ובדיקה שמשתמשת באותה טבלה לא תתפוס אותה.
# --------------------------------------------------------------------
BY_ISOWEEKDAY = {1: "שני", 2: "שלישי", 3: "רביעי", 4: "חמישי", 5: "שישי", 6: "שבת", 7: "ראשון"}

for back in range(0, 8):
    d = TODAY - timedelta(days=back)
    expected_name = BY_ISOWEEKDAY[d.isoweekday()]
    # רק שורות הלוח — שורת "השעה עכשיו" מכילה גם היא את תאריך היום
    # ואם לא מסננים אותה היא נבחרת ל-back=0 במקום שורת הלוח.
    line = next(
        (l for l in prompt.split("\n") if d.isoformat() in l and "days_back=" in l),
        None,
    )
    check(f"anchor table: {back} day(s) back is listed",
          line is not None, f"-> {d.isoformat()} not in the table")
    if line:
        check(f"anchor table: {d.isoformat()} carries days_back={back}",
              f"days_back={back}" in line, f"-> {line.strip()}")
        check(f"anchor table: {d.isoformat()} named as יום {expected_name}",
              expected_name in line, f"-> {line.strip()} (expected {expected_name})")

check("anchor table: 'שלשום' is spelled out with its date",
      any("שלשום" in l and (TODAY - timedelta(days=2)).isoformat() in l for l in prompt.split("\n")),
      "-> day-before-yesterday is not anchored")
check("anchor table: a month and a year back are anchored too",
      (TODAY - timedelta(days=30)).isoformat() in prompt
      and (TODAY - timedelta(days=365)).isoformat() in prompt)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    raise SystemExit(1)
print("All checks passed.")
