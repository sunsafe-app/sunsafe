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

import httpx

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
def get_current_uv(client: httpx.Client, lat: float, lon: float) -> float:
    response = client.get(
        OPEN_METEO_URL,
        params={"latitude": lat, "longitude": lon, "current": "uv_index"},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()["current"]["uv_index"]


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
