"""
בדיקה ידנית ל-geocode_city: זיהוי ערים דו-משמעיות (כמו "סן חוזה" - יש
גם בקוסטה ריקה וגם בארה"ב) דרך Open-Meteo, ושכבת הגיבוי ל-Nominatim
כש-Open-Meteo/GeoNames לא מכיר את השם בעברית בכלל (לא רק בעיית-איות —
ראו geocode_city / _nominatim_forward_geocode ב-bot_commands.py לרציונל
המלא, כולל המקרה האמיתי מ-2026-09-08: "סן חוסה קוסטה ריקה" נכשל לגמרי
מול Open-Meteo). מדמים את שני ה-API-ים עם תשובות קבועות מראש - לא
נוגעים ברשת אמיתית (חסומה מהסביבה הזו ממילא).
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

# tests/ נמצא רמה אחת מתחת לשורש הריפו — מוסיפים את שורש הריפו ל-sys.path
# כדי ש-import bot_commands (ומודולים אחיים אחרים) ימשיך לעבוד גם כשמריצים
# מ-tests/ ולא משורש הריפו.
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_commands as bc

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# (name, count) -> list of raw Open-Meteo-shaped result dicts
OPEN_METEO_CANNED = {
    ("תל אביב", 1): [
        {"name": "תל אביב", "country": "ישראל", "latitude": 32.08, "longitude": 34.78},
    ],
    ("סן חוזה קוסטה ריקה", 1): [],
    ("סן חוזה קוסטה", 10): [],  # suffix_len=1 -> city_part גיבוב, בלי תוצאות
    ("סן חוזה", 10): [  # suffix_len=2 -> city_part נכון, כמה מועמדים
        {"name": "סן חוזה", "country": "ארצות הברית", "latitude": 37.34, "longitude": -121.89},
        {"name": "סן חוזה", "country": "קוסטה ריקה", "latitude": 9.93, "longitude": -84.08},
    ],
    ("עיר לא קיימת בעולם כלל", 1): [],
    ("עיר לא קיימת בעולם", 10): [],
    ("עיר לא קיימת", 10): [],
    ("עיר לא", 10): [],
    ("סן חוזה מדינה בדיונית", 1): [],
    ("סן חוזה מדינה", 10): [],  # suffix_len=1, hint="בדיונית" -> אין תוצאות לצירוף הזה
    # המקרה האמיתי מ-2026-09-08: גם "סן חוסה" (סמך) לא נמצא ב-Open-Meteo
    # בשום פיצול — לא בעיית-איות, GeoNames פשוט לא מכיר את השם בעברית.
    ("סן חוסה קוסטה ריקה", 1): [],
    ("סן חוסה קוסטה", 10): [],
    ("סן חוסה", 10): [],
    # 2026-10-13: GeoNames מכיר "לוס אנגלס" בלבד (בלי גרש) ו"תל אביב" בלבד (בלי מקף)
    ("לוס אנג'לס", 1): [],
    ("לוס אנג׳לס", 1): [],
    ("לוס אנגלס", 1): [
        {"name": "לוס אנגלס", "country": "ארה״ב", "latitude": 34.05, "longitude": -118.24},
    ],
    ("תל-אביב", 1): [],
    ("לוס אנג'לס ארה״ב", 1): [],
    ("לוס אנגלס ארהב", 1): [],
    ("לוס אנגלס", 10): [
        {"name": "לוס אנגלס", "country": "ארה״ב", "latitude": 34.05, "longitude": -118.24},
    ],
}

# q (חיפוש טקסט-חופשי) -> תשובת Nominatim גולמית (list, לא dict-עם-results)
NOMINATIM_CANNED = {
    "סן חוסה קוסטה ריקה": [
        {
            "name": "סן חוסה",
            "lat": "9.9333",
            "lon": "-84.0833",
            "address": {"city": "סן חוסה", "country": "קוסטה ריקה"},
        }
    ],
    "עיר לא קיימת בעולם כלל": [],  # גם Nominatim לא מכיר -> found=False בסוף
    "סן חוזה מדינה בדיונית": [],
}


class FakeClient:
    """מדמה גם את Open-Meteo Geocoding וגם את Nominatim, מנותב לפי ה-URL."""

    def __init__(self):
        self.calls = []  # [("open-meteo", name, count)] או [("nominatim", q)]

    def get(self, url, params=None, timeout=None, headers=None):
        if url == bc.GEOCODING_URL:
            name, count = params["name"], params["count"]
            self.calls.append(("open-meteo", name, count))
            return FakeResponse({"results": OPEN_METEO_CANNED.get((name, count), [])})
        if url == bc.NOMINATIM_SEARCH_URL:
            q = params["q"]
            self.calls.append(("nominatim", q))
            return FakeResponse(NOMINATIM_CANNED.get(q, []))
        raise AssertionError(f"unexpected URL in test: {url}")


# 1) התאמה ישירה — המקרה השכיח, לא נוגע בלוגיקת הפיצול או ב-Nominatim בכלל
client = FakeClient()
geo = bc.geocode_city(client, "תל אביב")
check("direct match: found", geo["found"] is True, f"-> {geo}")
check("direct match: single API call only (Nominatim not reached)", len(client.calls) == 1, f"-> {client.calls}")

# 2) פיצול נכון: "סן חוזה קוסטה ריקה" -> city="סן חוזה", country hint="קוסטה ריקה"
#    נמצא כבר ב-Open-Meteo -> Nominatim לא אמור להיקרא בכלל.
client = FakeClient()
geo = bc.geocode_city(client, "סן חוזה קוסטה ריקה")
check("disambiguation: found", geo["found"] is True, f"-> {geo}")
check("disambiguation: resolved to Costa Rica, not USA", geo.get("country") == "קוסטה ריקה", f"-> {geo}")
check("disambiguation: correct lat/lon (CR, not US)", geo.get("latitude") == 9.93, f"-> {geo}")
check("disambiguation: resolved via Open-Meteo alone, no Nominatim fallback needed", all(c[0] == "open-meteo" for c in client.calls), f"-> {client.calls}")

# 3) אין שום התאמה ב-Open-Meteo בשום פיצול, וגם Nominatim לא מכיר -> not found
client = FakeClient()
geo = bc.geocode_city(client, "עיר לא קיימת בעולם כלל")
check("no match anywhere (Open-Meteo + Nominatim both empty): found=False", geo["found"] is False, f"-> {geo}")
check("no match anywhere: Nominatim was tried as last resort", any(c[0] == "nominatim" for c in client.calls), f"-> {client.calls}")

# 4) יש מועמדים לחלק-העיר ב-Open-Meteo, אבל אף אחד לא תואם את רמז-המדינה,
#    וגם Nominatim לא מכיר את הצירוף -> not found (לא בוחרים "את הראשון שיש" בשקט)
client = FakeClient()
geo = bc.geocode_city(client, "סן חוזה מדינה בדיונית")
check("hint doesn't match any candidate country, Nominatim also empty: found=False", geo["found"] is False, f"-> {geo}")

# 5) המקרה האמיתי מ-2026-09-08: "סן חוסה קוסטה ריקה" (וגם "סן חוזה")
#    נכשל לגמרי מול Open-Meteo (בשום איות/פיצול) — Nominatim כן מכיר,
#    ואמור "להציל" את הבקשה במקום "לא נמצא".
client = FakeClient()
geo = bc.geocode_city(client, "סן חוסה קוסטה ריקה")
check("Open-Meteo total miss -> Nominatim fallback succeeds", geo["found"] is True, f"-> {geo}")
check("Nominatim fallback: correct city name", geo.get("name") == "סן חוסה", f"-> {geo}")
check("Nominatim fallback: correct country", geo.get("country") == "קוסטה ריקה", f"-> {geo}")
check("Nominatim fallback: correct lat/lon", geo.get("latitude") == 9.9333 and geo.get("longitude") == -84.0833, f"-> {geo}")
check(
    "Nominatim fallback: Open-Meteo was tried first (direct + all 3 splits) before falling back",
    [c[0] for c in client.calls] == ["open-meteo", "open-meteo", "open-meteo", "open-meteo", "nominatim"],
    f"-> {client.calls}",
)
check("Nominatim fallback: called with the full original city string", client.calls[-1] == ("nominatim", "סן חוסה קוסטה ריקה"), f"-> {client.calls}")

# 6) _nominatim_forward_geocode ישירות — תשובה בלי lat/lon תקין -> found=False, לא קורס
class BadLatLonClient:
    def get(self, url, params=None, timeout=None, headers=None):
        return FakeResponse([{"name": "משהו", "lat": "not-a-number", "lon": "-84.08", "address": {"city": "משהו"}}])


geo_bad = bc._nominatim_forward_geocode(BadLatLonClient(), "משהו")
check("_nominatim_forward_geocode: invalid lat/lon -> found=False, no crash", geo_bad == {"found": False}, f"-> {geo_bad}")

# 8) כתיבים חלופיים (2026-10-13): גרש ומקף
for typed in ("לוס אנג'לס", "לוס אנג׳לס"):
    client = FakeClient()
    geo = bc.geocode_city(client, typed)
    check(f"geresh variant {typed!r} -> found Los Angeles", geo.get("found") and geo.get("name") == "לוס אנגלס", f"-> {geo}")
    check(f"...without reaching Nominatim", not any(c[0] == "nominatim" for c in client.calls), f"-> {client.calls}")

client = FakeClient()
geo = bc.geocode_city(client, "תל-אביב")
check("hyphen variant 'תל-אביב' -> found Tel Aviv", geo.get("found") and geo.get("name") == "תל אביב", f"-> {geo}")

client = FakeClient()
geo = bc.geocode_city(client, "לוס אנג'לס ארה״ב")
check("geresh + country hint -> found via the split on the normalised name",
      geo.get("found") and geo.get("country") == "ארה״ב", f"-> {geo}")

from geo_uv_core import spelling_variants
check("spelling_variants: plain name has no variants", spelling_variants("תל אביב") == [])
check("spelling_variants: geresh removed", spelling_variants("לוס אנג'לס") == ["לוס אנגלס"])
check("spelling_variants: hyphen -> space", spelling_variants("תל-אביב") == ["תל אביב"])
check("spelling_variants: Hebrew maqaf too", spelling_variants("תל־אביב") == ["תל אביב"])

# הבדיקה הישירה (1) נשארת קריאה אחת בלבד — הכתיבים החלופיים רק אחרי כישלון
# 7) _text_matches — כיוון הכלה דו-צדדי, בלי תלות במקפים/גרשיים
check("_text_matches: exact", bc._text_matches("קוסטה ריקה", "קוסטה ריקה") is True)
check("_text_matches: hint contained in value", bc._text_matches("ריקה", "קוסטה ריקה") is True)
check("_text_matches: value contained in hint", bc._text_matches("ארה\"ב ולא רק", "ארה\"ב") is True)
check("_text_matches: no relation", bc._text_matches("קוסטה ריקה", "ארצות הברית") is False)
check("_text_matches: None value", bc._text_matches("קוסטה ריקה", None) is False)


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
else:
    print("All checks passed.")
