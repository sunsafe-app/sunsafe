"""
SunSafe — Weather MCP Server
------------------------------
A standalone MCP server wrapping Open-Meteo (free, no API key) and exposing
weather tools as standard MCP Tools. The same server serves both the production
Agent Loop (via mcp_agent_loop.py) and Claude Desktop / Claude Code for manual
testing — without writing the weather logic twice.

Run standalone (for example, to connect from Claude Desktop):
    python mcp_weather_server.py

Usually you do not run this by hand — the MCP Client (e.g. mcp_agent_loop.py)
spawns this file as a subprocess automatically over stdio.
"""

import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from mcp.server.fastmcp import FastMCP

from supabase_client import insert_row
from geo_uv_core import (
    calculate_exposure_score as core_calculate_exposure_score,
    score_to_level,
    geocode_city as core_geocode_city,
    fetch_current_weather,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sunsafe.mcp_weather_server")

mcp = FastMCP("weather-mcp-server")

# geocoding/UV/exposure-score עברו ל-geo_uv_core.py (2026-09-09, ניקוי
# כפילות מול bot_commands.py — ראו שם). OPEN_METEO_URL נשאר כאן כי
# get_uv_forecast (למטה) עדיין משתמש בו ישירות ולא הועבר (לא כפול
# באף מקום אחר).
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# ארכיון UV (2026-09-15) -------------------------------------------------
# ה-forecast API שלמעלה מגיש גם עבר, דרך past_days — אבל הפרמטר חסום
# ב-92 יום. לשאלות ישנות יותר ("מה היה ה-UV במצפה רמון לפני שנה") צריך
# endpoint אחר: ה-Air Quality API של Open-Meteo, שמגיש uv_index כמשתנה
# שעתי מארכיון CAMS.
#
# **שני מודלים שונים.** אותה שעה באותו מקום יכולה לקבל ערך מעט שונה
# בין ה-forecast לארכיון. לכן get_historical_uv מעדיפה את ה-forecast
# API כל עוד התאריך בטווח 92 הימים — כך שתשובה על "אתמול" עקבית עם
# המספר ש-/end_session חישב באותו יום — ונופלת לארכיון רק כשאין ברירה.
# השדה "source" בתשובה אומר מאיפה הגיע הערך, כדי שאפשר יהיה להסביר
# הפרש אם מישהו ישווה.
OPEN_METEO_AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# past_days ב-forecast API מוגבל ל-0..92 (מגבלת Open-Meteo, לא שלנו).
FORECAST_PAST_DAYS_LIMIT = 92

# הכיסוי הגלובלי של ארכיון ה-UV מתחיל באוגוסט 2022. לפני זה יש דומיין
# אירופאי שמגיע רחוק יותר, אבל הגבול הדרומי שלו עובר בערך באזור הנגב
# ולא אימתנו אותו — עדיף להחזיר "אין נתונים" מאשר ערך שאולי המצאה.
UV_ARCHIVE_START = date(2022, 8, 1)


@mcp.tool()
async def geocode_city(city_name: str) -> dict:
    """
    מאתר קואורדינטות (lat, lon) עבור שם עיר, באמצעות שירות ה-Geocoding
    החינמי של Open-Meteo (ללא API key, כמו שאר הכלים בשרת הזה).

    יש לקרוא לכלי הזה תמיד *לפני* get_current_uv או get_uv_forecast,
    כדי לוודא שהעיר אכן קיימת ולקבל קואורדינטות מדויקות — ולא לנחש
    lat/lon עצמאית מתוך ידע כללי.

    מחזיר dict עם "found": bool.
    אם found=True: גם "name" (השם הרשמי/המתוקן), "country", "latitude",
    "longitude".
    אם found=False: לא נמצאה עיר מתאימה לשם שסופק — אין לנחש ערכים,
    יש לדווח על כך למשתמש.

    מ-2026-09-09: מיושם דרך geo_uv_core.geocode_city המשותף עם הבוט
    החי (bot_commands.py) — כולל את שכבת פיצול-עיר/מדינה ואת הגיבוי
    ל-Nominatim, לא רק ההתאמה הישירה כמו קודם. אותו client סינכרוני
    (httpx.Client, לא AsyncClient) בכוונה — שרת MCP מקומי, יוזר יחיד,
    בלי לחץ concurrency אמיתי שמצדיק async כאן.
    """
    logger.info("geocode_city(city_name=%s)", city_name)
    with httpx.Client() as client:
        result = core_geocode_city(client, city_name)

    if not result["found"]:
        logger.info("geocode_city(%s) -> not found", city_name)
        return {"found": False, "query": city_name}

    logger.info("geocode_city(%s) -> %s", city_name, result)
    return result


@mcp.tool()
async def get_current_uv(lat: float, lon: float) -> dict:
    """
    Return the current UV index, temperature, cloud cover and relative
    humidity for a given geographic location. Call this tool whenever
    up-to-date information about the UV radiation level or weather at a
    specific place is needed — including humidity questions.

    מ-2026-09-09: מיושם דרך geo_uv_core.fetch_current_weather המשותף.
    מ-2026-09-12: נוספה לחות יחסית (relative_humidity_2m).
    """
    logger.info("get_current_uv(lat=%s, lon=%s)", lat, lon)
    with httpx.Client() as client:
        return fetch_current_weather(client, lat, lon)


@mcp.tool()
def calculate_exposure_score(
    uv_index: float,
    duration_minutes: float,
    skin_type: int,
    spf: int | None = None,
) -> dict:
    """
    מחשב מדד חשיפה אישי (0-100+) לפי UV Index, משך חשיפה בדקות, סוג עור
    (סולם Fitzpatrick, 1-6), ו-SPF אופציונלי (None אם לא נעשה שימוש
    בקרם הגנה). פונקציה טהורה (סינכרונית, בלי גישה לרשת/DB) — זהה
    בדיוק לנוסחה ב-README ובדף ההדגמה, כדי שהציון יהיה עקבי בכל
    מקום בפרויקט.

    נוסחה: safe_minutes = (200/uv_index) × skin_factor × effective_spf,
    כאשר effective_spf = 1 + (spf-1)×0.4 (או 1 אם spf=None).
    exposure_score = round((duration_minutes / safe_minutes) × 100).

    מחזיר {"exposure_score": int, "level": "good"|"warning"|"serious"|"critical"}.

    מ-2026-09-09: מיושם דרך geo_uv_core.calculate_exposure_score/
    score_to_level המשותפים עם הבוט החי (bot_commands.py) — אותה נוסחה
    בדיוק, רק ממוקמת פעם אחת במקום פעמיים.
    """
    score = core_calculate_exposure_score(uv_index, duration_minutes, skin_type, spf)
    level = score_to_level(score)

    logger.info(
        "calculate_exposure_score(uv=%s, duration=%s, skin_type=%s, spf=%s) -> %s (%s)",
        uv_index, duration_minutes, skin_type, spf, score, level,
    )
    return {"exposure_score": score, "level": level}


@mcp.tool()
async def log_uv_reading(
    query_city: str,
    resolved_city: str,
    country: str | None,
    lat: float,
    lon: float,
    uv_index: float,
    temperature_2m: float | None = None,
    cloud_cover: int | None = None,
) -> dict:
    """
    שומר קריאת UV שבוצעה בפועל בטבלת uv_readings ב-Supabase, לצורך
    היסטוריה עתידית (Dashboard). יש לקרוא לכלי הזה תמיד אחרי
    get_current_uv כאשר יש תוצאה תקפה לדווח עליה.

    כשלון בשמירה (בעיית רשת/הרשאות מול Supabase) לא אמור לעצור את
    התשובה למשתמש — הכלי מחזיר {"logged": False, "error": ...}
    במקום לזרוק חריגה.
    """
    logger.info(
        "log_uv_reading(query_city=%s, resolved_city=%s, uv_index=%s)",
        query_city, resolved_city, uv_index,
    )
    row = {
        "query_city": query_city,
        "resolved_city": resolved_city,
        "country": country,
        "lat": lat,
        "lon": lon,
        "uv_index": uv_index,
        "temperature_2m": temperature_2m,
        "cloud_cover": cloud_cover,
    }
    try:
        inserted = insert_row("uv_readings", row)
        logger.info("log_uv_reading -> logged id=%s", inserted.get("id"))
        return {"logged": True, "id": inserted.get("id")}
    except Exception as e:
        logger.warning("log_uv_reading failed: %s", e)
        return {"logged": False, "error": str(e)}


@mcp.tool()
async def get_uv_forecast(lat: float, lon: float, days: int = 3) -> dict:
    """
    Return an hourly UV Index forecast for the next N days (default: 3) for a
    given geographic location. Useful for planning exposure ahead of time, not
    just for the current conditions.
    """
    logger.info("get_uv_forecast(lat=%s, lon=%s, days=%s)", lat, lon, days)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "uv_index",
                "forecast_days": days,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()["hourly"]


def _summarise_uv_day(hourly: dict, target: str) -> dict:
    """
    מצמצם תשובת hourly של Open-Meteo ליום אחד ולתקציר קריא.

    מחזיר את שעות האור בלבד (uv>0) ולא 24 מספרים שרובם אפסים — גם כי
    זה מה שמעניין, וגם כי כל מה שחוזר מכאן נכנס לחלון ההקשר של המודל.
    """
    times = hourly.get("time") or []
    values = hourly.get("uv_index") or []

    day = [
        (t, v) for t, v in zip(times, values)
        if isinstance(t, str) and t.startswith(target) and v is not None
    ]
    if not day:
        return {}

    daylight = [(t, v) for t, v in day if v > 0]
    peak_time, peak_value = max(day, key=lambda tv: tv[1])
    return {
        "uv_max": round(peak_value, 1),
        "peak_hour": peak_time[11:16],
        "hourly": {
            "time": [t[11:16] for t, _ in daylight],
            "uv_index": [round(v, 1) for _, v in daylight],
        },
    }


@mcp.tool()
async def get_historical_uv(
    lat: float,
    lon: float,
    date_iso: str | None = None,
    days_back: int | None = None,
) -> dict:
    """
    Return the UV index for a **past** date at a given location: the day's
    peak value, the hour of that peak, and the hourly series for daylight
    hours.

    Use this for any question about UV on a date that has already passed
    ("what was the UV in Mitzpe Ramon yesterday / last week / a year
    ago"). For today use get_current_uv; for future dates use
    get_uv_forecast. Always call geocode_city first — never guess lat/lon.

    Pass exactly one of:
      days_back — how many days ago (1 = yesterday, 7 = a week ago,
                  365 = a year ago). **Prefer this for anything phrased
                  relatively**: it needs no date arithmetic, so it cannot
                  land on the wrong day.
      date_iso  — an explicit "YYYY-MM-DD", only when the user named a
                  specific calendar date.

    Returns {"found": true, "date", "days_ago", "source", "uv_max",
    "peak_hour", "hourly"} — or {"found": false, "reason"} when the date
    is out of range or the provider has no data. **On found=false, say so
    plainly; never estimate a UV value.** Always report the returned
    "date" back to the user, so the day being answered about is visible.

    Note: this reports UV only. Temperature, cloud cover and humidity are
    available for *current* conditions (get_current_uv), not for history.
    """
    logger.info(
        "get_historical_uv(lat=%s, lon=%s, date_iso=%s, days_back=%s)",
        lat, lon, date_iso, days_back,
    )

    today = datetime.now(timezone.utc).date()

    # days_back קיים כדי שהמודל לא יחשב תאריכים בעצמו. באג אמיתי
    # (2026-09-15): על "אתמול" הוא שלח date_iso=2025-05-18, תאריך מתוך
    # ידיעת האימון שלו, וקיבל UV אמיתי ליום הלא נכון. עם days_back=1
    # החישוב קורה כאן, מול השעון האמיתי.
    if (date_iso is None) == (days_back is None):
        return {
            "found": False,
            "reason": "pass exactly one of days_back (1 = yesterday) or date_iso (YYYY-MM-DD)",
        }

    if days_back is not None:
        if days_back < 1:
            return {
                "found": False,
                "reason": f"days_back must be 1 or more (got {days_back}); "
                          "for today use get_current_uv",
            }
        target = today - timedelta(days=days_back)
        date_iso = target.isoformat()
    else:
        try:
            target = date.fromisoformat(date_iso)
        except (TypeError, ValueError):
            return {"found": False, "reason": f"date_iso must be YYYY-MM-DD, got {date_iso!r}"}

    age_days = (today - target).days

    if age_days < 0:
        return {
            "found": False,
            "reason": f"{date_iso} is in the future — use get_uv_forecast instead",
        }

    if age_days <= FORECAST_PAST_DAYS_LIMIT:
        url = OPEN_METEO_URL
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "uv_index",
            "past_days": age_days,
            "forecast_days": 1,
            "timezone": "auto",
        }
        source = "forecast"
    elif target >= UV_ARCHIVE_START:
        url = OPEN_METEO_AIR_QUALITY_URL
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "uv_index",
            "start_date": date_iso,
            "end_date": date_iso,
            "timezone": "auto",
        }
        source = "archive"
    else:
        return {
            "found": False,
            "reason": (
                f"no UV archive before {UV_ARCHIVE_START.isoformat()} "
                f"at this location ({date_iso} requested)"
            ),
        }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=15.0)
            response.raise_for_status()
            hourly = response.json().get("hourly") or {}
    except Exception as e:
        logger.warning("get_historical_uv(%s) failed against %s: %s", date_iso, source, e)
        return {"found": False, "reason": f"could not reach the UV provider: {e}"}

    summary = _summarise_uv_day(hourly, date_iso)
    if not summary:
        return {"found": False, "reason": f"the provider returned no UV data for {date_iso}"}

    logger.info(
        "get_historical_uv(%s) -> uv_max=%s at %s (source=%s)",
        date_iso, summary["uv_max"], summary["peak_hour"], source,
    )
    return {
        "found": True,
        "date": date_iso,
        "days_ago": age_days,
        "source": source,
        **summary,
    }


if __name__ == "__main__":
    mcp.run()  # stdio transport כברירת מחדל
