"""
SunSafe — geocoding + UV + exposure-score core logic
------------------------------------------------------
לוגיקה טהורה (בלי גישה ל-DB, בלי קריאת משתני-סביבה בזמן import) שמשמשת
גם את bot_commands.py (הבוט האמיתי) וגם את mcp_weather_server.py (שרת
ה-MCP, שמשמש רק את send_uv_report.py — ראו sunsafe_uml_hf_space.html
להיקף המדויק של מה שרץ בפועל על ה-HF Space).

רקע (2026-09-09): עד לרפקטור הזה היו לפרויקט שני מימושים נפרדים,
בלי קשר ביניהם, לאותה לוגיקה בדיוק — אחד ב-bot_commands.py ואחד עטוף
ב-@mcp.tool() ב-mcp_weather_server.py. הם התפצלו כי הכלים העטופים
ב-mcp.tool לא נוחים לייבוא ישיר ממודול חיצוני (ראו הערה ישנה שהייתה
ב-bot_commands.py). המחיר של הפיצול היה אמיתי: כשהתגלה ותוקן הבאג של
"סן חוזה קוסטה ריקה" (שכבת פיצול-מדינה + נפילה ל-Nominatim, גם ב-
2026-09-08), התיקון הלך רק לגרסה של bot_commands.py — כך שאותה בדיוק
תקלה נשארה קיימת ב-Agent Loop (send_uv_report.py) בלי שאף אחד שם לב.

המודול הזה לא תלוי ב-BOT_TOKEN/סודות אחרים בזמן import (בכוונה) — כדי
ש-mcp_weather_server.py ימשיך לעבוד כשמריצים אותו standalone (מ-Claude
Desktop, לבדיקה ידנית) בלי צורך בכל הסודות שהבוט המלא דורש.
"""

import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("sunsafe.geo_uv_core")

# --- Open-Meteo (geocoding + UV, בלי API key) ---------------------------
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# --- Nominatim (OpenStreetMap) — שכבת גיבוי ל-geocoding בלבד. reverse
# geocoding (lat/lon -> עיר) לא חלק מהמודול הזה — הוא נשאר מקומי
# ב-bot_commands.py (reverse_geocode_location), כי הוא לא היה כפול
# באף מקום אחר. ---------------------------------------------------------
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "SunSafe-Bot/1.0 (student course project)"

# זהה לנוסחה ב-README/דף ההדגמה/dashboard/submit-offline-session — כל
# שינוי כאן צריך להסתנכרן גם בשני המקומות ב-TypeScript (לא ניתן לייבא
# מודול Python משם, אז שם זו עדיין כפילות בלתי נמנעת).
SKIN_TYPE_FACTOR = {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.5, 5: 2.5, 6: 4.0}


# ---------------------------------------------------------------------
# Exposure score
# ---------------------------------------------------------------------
def effective_spf(labeled_spf: int | None) -> float:
    if not labeled_spf:
        return 1.0
    return 1 + (labeled_spf - 1) * 0.4


def safe_exposure_minutes(uv_index: float, skin_type: int, spf: int | None = None) -> float | None:
    """
    כמה דקות בשמש ישירה עד סיכון לכוויה, עבור סוג עור ו-UV נתונים.

    זה החצי הראשון של calculate_exposure_score, שהופרד ב-2026-09-15 כדי
    שאפשר יהיה **להציג** אותו למשתמש. עד אז המספר הזה היה מחושב בכל
    session ונזרק מיד: המשתמש קיבל רק "מדד חשיפה: 55%" — אחוז מתקציב
    שהוא לא רואה.

    מבדיקת הצוות: משתמש פתח session בירוחם ב-UV 6.3 עם סוג עור 3, ואמר
    "אני לא רואה שום דבר מעבר ל'תמרח 50', יכולתי להבין את זה לבד". הוא
    צדק — הבוט ידע באותו רגע שמדובר ב-32 דקות בשבילו, ולא אמר.

    מחזירה None כשאין משמעות למספר (UV אפס — למשל session בלילה), כדי
    שנקודת הקריאה תדלג על השורה במקום להציג "אינסוף דקות".
    """
    if uv_index <= 0:
        return None
    factor = SKIN_TYPE_FACTOR.get(skin_type, 1.0)
    return (200 / uv_index) * factor * effective_spf(spf)


def calculate_exposure_score(uv_index: float, duration_minutes: float, skin_type: int, spf: int | None) -> int:
    # UV=0 (למשל session שנפתח בלילה) הוא ערך תקין לגמרי, לא שגיאה — אבל
    # 200/uv_index עם 0 קורס ב-ZeroDivisionError. בלי חשיפה ל-UV בכלל
    # הסיכון הוא אפס, ללא תלות במשך הזמן, אז מחזירים 0 ישירות. באג אמיתי
    # שתפס session תקוע (id=38, UV=0) — ראה השיחה מ-31.8.2026.
    safe_minutes = safe_exposure_minutes(uv_index, skin_type, spf)
    if safe_minutes is None:
        return 0
    return round((duration_minutes / safe_minutes) * 100)


def score_to_level(score: int) -> str:
    """באנדים זהים בכל מקום בפרויקט: good < 40 <= warning < 70 <= serious < 100 <= critical."""
    if score < 40:
        return "good"
    if score < 70:
        return "warning"
    if score < 100:
        return "serious"
    return "critical"


# ---------------------------------------------------------------------
# סיכום יומי — צבירה על פני כל ה-sessions של אותו יום
# ---------------------------------------------------------------------
# נוסף 16.9.2026. עד כאן היחידה היחידה שהייתה לפרויקט היא session בודד,
# והגדרת "ציון של יום" הייתה **המקסימום** מבין ה-sessions — גם ב-
# _peak_exposure_session בבוט וגם ב-dayScoreOf בדשבורד.
#
# זו הגדרה שמקטינה סיכון. נזק UV מצטבר במשך היום: מי שיצא שלוש פעמים
# לחצי שעה ב-UV גבוה חטף את שלושתן, ו-max היה מציג לו את אחת מהן.
# שלושה sessions של 40% הם 120% מהתקציב היומי, לא 40%.
#
# **המחיר, ובמפורש:** ההגדרה של "ציון יומי" משתנה, ולכן גם dayScoreOf
# בדשבורד עבר לסכום באותו שינוי. ההערה שם הזהירה בדיוק מזה — "אם
# שניהם יתפצלו, כפתור יצבע אדום והלוח שנפתח בלחיצה עליו יראה מספר
# אחר". צבעים של ימים היסטוריים בדשבורד אכן משתנים, וזה מכוון.
#
# מסכמים את ה-exposure_score השמורים (int לכל session), ולא מחשבים
# מחדש מסכום דקות: זה מה שגם הבוט וגם הדשבורד מחזיקים ביד, וכך שניהם
# מגיעים לאותו מספר בלי תלות בעיגול.

DAILY_BAR_CELLS = 10

# גבולות הבאנדים זהים ל-score_to_level למעלה: 40 / 70 / 100.
_BAR_GREEN_CELLS = 4    # 0-39%
_BAR_YELLOW_CELLS = 7   # 40-69%
_BAR_CELL_FULL = "⬜"


def daily_exposure_score(session_scores) -> int:
    """
    סכום מדדי החשיפה של כל ה-sessions הסגורים באותו יום. None מסונן
    (session פתוח — אין לו עדיין ציון).
    """
    return sum(score for score in session_scores if score is not None)


def exposure_bar(score: int, cells: int = DAILY_BAR_CELLS) -> str:
    """
    סרגל של ריבועי אמוג'י. הודעת טלגרם לא יכולה לשאת צבע — אין HTML
    ואין CSS — אבל ריבועי אמוג'י נראים כבלוקים צבעוניים בכל לקוח,
    נשארים טקסט שניתן להעתיק, ולא דורשים רנדור תמונה.

    מתחת ל-100% הסרגל מתמלא דרך הבאנדים: ירוק, צהוב, כתום. ב-100%
    ומעלה הוא כולו אדום — "חשיפה מלאה", וכאן הבאנדים כבר לא אומרים
    כלום כי התקציב נגמר.

    **הצבע לעולם לא לבד.** השורה שמתחת לסרגל נושאת את המספר ואת שם
    הרמה במילים, כי ירוק-מול-אדום הוא בדיוק הצמד שדויטרנופיה לא
    מבדילה — ובהודעה, בשונה מהדשבורד, אין כפתור נגישות להציע.
    """
    if score >= 100:
        return "🟥" * cells

    filled = min(cells, max(1, int(score / (100 / cells)))) if score > 0 else 0
    out = []
    for i in range(cells):
        if i >= filled:
            out.append(_BAR_CELL_FULL)
        elif i < _BAR_GREEN_CELLS:
            out.append("🟩")
        elif i < _BAR_YELLOW_CELLS:
            out.append("🟨")
        else:
            out.append("🟧")
    return "".join(out)


def daily_summary_he(
    day_score: int,
    session_count: int,
    total_minutes: float,
    peak_city: str | None = None,
    peak_score: int | None = None,
) -> str:
    """
    בלוק הסיכום היומי, בעברית. פונקציה טהורה — bot_commands.py שולף
    את הנתונים, וה-Worker של השקיעה מייצר את אותו טקסט בדיוק מ-
    logic.ts (נבדק מול fixture שנוצר מכאן).
    """
    level = score_to_level(day_score)
    headline = {
        "good": "בטווח הבטוח.",
        "warning": "בטווח הבינוני.",
        "serious": "בטווח הגבוה.",
        "critical": "חשיפה מלאה — עברתם את התקציב היומי.",
    }[level]

    if day_score < 100:
        headline += f" עוד {100 - day_score}% עד חשיפה מלאה."

    # session_count מגיע כפרמטר אבל **לא מוצג** (16.9.2026). הוא הוצג
    # בגרסה הראשונה, ובבדיקה אמיתית יצא "16 sessions · 23 דקות בשמש"
    # — כלומר 1.4 דקות לכל אחד. המספר מודד כמה פעמים נלחץ /start_session,
    # לא כמה שמש נספגה, והוא רק מוריד את העין מהמספר שכן חשוב. הוא
    # נשאר בחתימה כי הוא קובע אם יש בכלל "הגבוה ביותר" להציג.
    minutes = round(total_minutes)
    lines = [
        f"{exposure_bar(day_score)}  {day_score}%",
        headline,
        "",
        f"היום: {minutes} דקות בשמש",
    ]
    if peak_city and peak_score is not None and session_count > 1:
        # אותו נוסח כמו ב-/today ("מדד החשיפה הגבוה ביותר"), ולא
        # "הגבוה מביניהם" — בלי מספר ה-sessions אין למה להתייחס.
        lines.append(f"הגבוה ביותר: {peak_city}, {peak_score}%")
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Geocoding — Open-Meteo (ראשי) + Nominatim (גיבוי)
# ---------------------------------------------------------------------
def _raw_geocode_search(client: httpx.Client, name: str, count: int = 1) -> list[dict]:
    """קריאה גולמית ל-Open-Meteo Geocoding — מחזירה עד `count` מועמדים גולמיים."""
    response = client.get(
        GEOCODING_URL,
        params={"name": name, "count": count, "language": "he", "format": "json"},
        timeout=10.0,
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    return [
        {
            "name": r.get("name"),
            "country": r.get("country"),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
        }
        for r in results
    ]


def _text_matches(hint: str, value: str | None) -> bool:
    """
    התאמת טקסט "רכה" בין country_hint שהמשתמש הקליד לבין שדה country
    שחזר מ-Open-Meteo — בלי תלות במקף/גרשיים, ובכיוון הכלה כלשהו (כך
    ש"ארה\"ב" יתאים גם ל"ארצות הברית" אם אחד מהם מוכל במשנהו, לא רק
    שוויון מדויק).
    """
    if not value or not hint:
        return False
    normalize = lambda s: s.strip().replace("-", " ").replace("״", "").replace('"', "")
    hint_n, value_n = normalize(hint), normalize(value)
    return bool(hint_n) and (hint_n in value_n or value_n in hint_n)


def _nominatim_forward_geocode(client: httpx.Client, city_name: str) -> dict:
    """
    שכבת גיבוי ל-geocode_city (למטה) — נקראת רק אחרי ש-Open-Meteo/
    GeoNames נכשל לגמרי (גם התאמה ישירה וגם כל פיצול עיר/מדינה). מריצה
    חיפוש טקסט-חופשי (q=) מול Nominatim, שם המחרוזת כולה (כולל ציון-
    מדינה אם יש, למשל "סן חוסה קוסטה ריקה") נכנסת כמו שהיא — Nominatim
    כבר יודע לפרש "עיר, מדינה" בעצמו, אז אין צורך בלוגיקת הפיצול
    שקיימת למעלה בשביל Open-Meteo.

    לא זורקת אם אין תוצאות/כתובת — מחזירה found=False, אותו חוזה בדיוק
    כמו geocode_city.
    """
    response = client.get(
        NOMINATIM_SEARCH_URL,
        params={"q": city_name, "format": "json", "accept-language": "he", "limit": 1, "addressdetails": 1},
        headers={"User-Agent": NOMINATIM_USER_AGENT},
        timeout=10.0,
    )
    response.raise_for_status()
    results = response.json() or []
    if not results:
        return {"found": False}

    result = results[0]
    address = result.get("address") or {}
    name = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
        or result.get("name")
    )
    try:
        latitude, longitude = float(result["lat"]), float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return {"found": False}
    if not name:
        return {"found": False}

    return {"found": True, "name": name, "country": address.get("country"), "latitude": latitude, "longitude": longitude}


def geocode_city(client: httpx.Client, city_name: str) -> dict:
    """
    מזהה עיר לפי שם חופשי. קודם מנסים את המחרוזת המלאה כמו שהיא — המקרה
    השכיח, שם עיר יחיד כמו "תל אביב". אם זה נכשל וישנן כמה מילים, כנראה
    שם העיר מלווה בציון מדינה (כמו "סן חוזה קוסטה ריקה" — כדי להבדיל
    מ-San Jose שבארה"ב, שהיא עיר גדולה יותר ותקבל עדיפות בברירת המחדל
    של Open-Meteo לפי אוכלוסייה). ל-Open-Meteo אין פרמטר סינון-לפי-מדינה
    נפרד, אז מפצלים את המחרוזת לחלק-עיר וחלק-מדינה (1 עד 3 המילים
    האחרונות — מכסה גם מדינות דו-מילתיות כמו "קוסטה ריקה"), שולפים כמה
    מועמדים לחלק-העיר, ובודקים איזה מהם ה-country שלו תואם את חלק-המדינה.

    אם גם זה נכשל — לפני שמוותרים לגמרי, מנסים Nominatim
    (_nominatim_forward_geocode) כשכבה שלישית: מקרה אמיתי שנתקלנו בו
    ב-2026-09-08 — "סן חוסה קוסטה ריקה" (וגם האיות "סן חוזה") לא נמצא
    ב-Open-Meteo/GeoNames בשום איות ובשום פיצול, כי ל-GeoNames פשוט אין
    בכלל שם עברי רשום לעיר הזו (לא רק בעיית-איות — Open-Meteo לא עושה
    שום fuzzy/typo matching, בשונה מ-Google). Nominatim (OpenStreetMap)
    הוא מאגר קהילתי גדול יותר עם כיסוי רב-לשוני עשיר יותר — לא הבטחה
    לכל עיר, אבל שכבת גיבוי חינמית וסבירה לפני "לא נמצא".

    בלי התאמה בשום שכבה — "לא נמצא", בלי לנחש עיר שגויה בשקט.

    הערה חשובה: עד 2026-09-09 הפונקציה הזו הייתה כפולה — גרסה מלאה כאן
    (3 שכבות) וגרסה חלקית (שכבה 1 בלבד, בלי הפיצול ובלי Nominatim) בתוך
    ה-@mcp.tool() המקביל ב-mcp_weather_server.py. מאז האיחוד, גם ה-Agent
    Loop (send_uv_report.py) מקבל את אותן שלוש השכבות.
    """
    results = _raw_geocode_search(client, city_name, count=1)
    if results:
        return {"found": True, **results[0]}

    tokens = city_name.strip().split()
    for suffix_len in (1, 2, 3):
        if len(tokens) <= suffix_len:
            break
        city_part = " ".join(tokens[:-suffix_len])
        country_hint = " ".join(tokens[-suffix_len:])
        candidates = _raw_geocode_search(client, city_part, count=10)
        match = next((c for c in candidates if _text_matches(country_hint, c.get("country"))), None)
        if match:
            return {"found": True, **match}

    return _nominatim_forward_geocode(client, city_name)


# ---------------------------------------------------------------------
# UV — שתי עטיפות דקות מכוונות סביב Open-Meteo forecast, לא זהות בפועל:
# get_current_uv מחזירה רק את המספר (מה ש-bot_commands.py צריך בכל
# session), fetch_current_weather מחזירה גם temperature_2m/cloud_cover
# (מה ש-mcp_weather_server.py צריכה כדי לתעד קריאה מלאה ב-uv_readings
# דרך log_uv_reading). לא היו זהות-לגמרי גם לפני האיחוד — רק גרות
# באותו מקום מעכשיו במקום כל אחת בקובץ נפרד.
# ---------------------------------------------------------------------
class UvUnavailableError(Exception):
    """Open-Meteo ענה 200 אבל בלי מדד UV שאפשר להשתמש בו."""


def _nearest_hourly_uv(hourly: dict, now: datetime) -> float | None:
    """
    הערך ההשעתי הקרוב ביותר ל-now, מדלג על שעות שבהן הערך null.
    הזמנים מגיעים כ-ISO בלי אזור זמן כי אנחנו מבקשים timezone=UTC.
    """
    times = (hourly or {}).get("time") or []
    values = (hourly or {}).get("uv_index") or []
    best: tuple[float, float] | None = None  # (מרחק בשניות, ערך)
    for stamp, value in zip(times, values):
        if value is None:
            continue
        try:
            moment = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        distance = abs((moment - now).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, float(value))
    return None if best is None else best[1]


def get_current_uv(client: httpx.Client, lat: float, lon: float) -> float:
    """
    מדד ה-UV הנוכחי בנקודה. מחזירה תמיד float, או זורקת
    UvUnavailableError — **לעולם לא None**.

    16.9.2026: הגרסה הקודמת הייתה `return response.json()["current"]["uv_index"]`
    בלי שום בדיקה. Open-Meteo מחזיר לפעמים 200 עם `"uv_index": null`
    בבלוק current (פער בנתוני המודל), ואז None זרם הלאה ל-insert_row
    ונכתב לעמודה שמוגדרת `double precision not null` — 400 Bad Request
    מ-Supabase, traceback בלוגים, והמשתמש קיבל שתיקה מוחלטת על
    /start_session. נצפה בפועל ב-11:56 על "חיפה", שעתיים אחרי ש-session
    זהה נפתח בהצלחה: לא הקוד השתנה, התשובה מ-Open-Meteo השתנתה.
    לכן גם מבקשים hourly באותה קריאה — נפילה חזרה לשעה הקרובה ביותר
    עדיפה על חסימת ה-session, וזו אותה סדרה שמשמשת את /end_session
    לממוצע המשוקלל.
    """
    response = client.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "uv_index",
            "hourly": "uv_index",
            "forecast_days": 1,
            "timezone": "UTC",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    body = response.json()

    current = (body.get("current") or {}).get("uv_index")
    if current is not None:
        return float(current)

    fallback = _nearest_hourly_uv(body.get("hourly") or {}, datetime.now(timezone.utc))
    if fallback is not None:
        logger.warning(
            "Open-Meteo returned a null current UV for %s,%s — falling back to the "
            "nearest hourly value (%s)", lat, lon, fallback,
        )
        return fallback

    raise UvUnavailableError(
        f"Open-Meteo returned no usable UV value for {lat},{lon}"
    )


def fetch_current_weather(client: httpx.Client, lat: float, lon: float) -> dict:
    """
    UV + טמפ' + עננות + לחות יחסית נוכחיים —
    {"uv_index", "temperature_2m", "cloud_cover", "relative_humidity_2m"}.

    2026-09-12: הוספת relative_humidity_2m — משתמש שאל את הבוט "לחות?"
    דרך ה-Agent Loop, וזה קיבל תשובה שמתעלמת מהשאלה (חוזרת על UV/טמפ'/
    עננות בלבד) כי הכלי פשוט לא שלף לחות מ-Open-Meteo מעולם, למרות
    שה-API תומך בזה בלי עלות נוספת. לא נוגעים ב-get_current_uv (למעלה,
    שם bot_commands.py/handle_start_session צריך רק את מספר ה-UV) —
    רק בעטיפה הזו, שמשמשת את mcp_weather_server.get_current_uv
    (ה-Agent Loop) בלבד.
    """
    response = client.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "uv_index,temperature_2m,cloud_cover,relative_humidity_2m",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()["current"]
