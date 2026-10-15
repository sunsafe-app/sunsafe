"""
בדיקה ידנית (לא pytest) למוסכמת אזורי הזמן בקריאות Open-Meteo (2026-10-15).

הרקע: שאלה לבוט ב-12:02 שעון ישראל נענתה "ה-UV בתל אביב עכשיו
(15.10.2026, 09:00)". 09:00 הוא UTC. fetch_current_weather שלחה params
בלי "timezone" בכלל, וברירת המחדל של Open-Meteo היא GMT — אז השעה
חזרה ב-UTC וה-Agent Loop הדפיס אותה כשעון מקומי. get_uv_forecast סבלה
מאותו דבר (peak_hour), רק שאף אחד עוד לא שאל.

הבדיקה השלישית כאן היא העיקרית: היא לא בודקת את שני התיקונים אלא
אוסרת על המחלקה כולה — כל קריאת מזג אוויר חייבת לנקוב באזור זמן
במפורש, בין אם "auto" ובין אם "UTC". ברירת מחדל שקטה היא הבאג.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

import re
import sys
import asyncio
import pathlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import geo_uv_core
import mcp_weather_server as mws

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1. fetch_current_weather מבקשת שעון מקומי ומחזירה אותו מתאר-עצמו
# ---------------------------------------------------------------------
BODY = {
    "timezone": "Asia/Jerusalem",
    "timezone_abbreviation": "IDT",
    "utc_offset_seconds": 10800,
    "current": {"time": "2026-10-15T12:00", "interval": 900, "uv_index": 5.6,
                "temperature_2m": 27.5, "cloud_cover": 16, "relative_humidity_2m": 42},
}


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, body):
        self._body = body
        self.params = None

    def get(self, url, params=None, timeout=None):
        self.params = params
        return FakeResponse(self._body)


client = FakeClient(BODY)
result = geo_uv_core.fetch_current_weather(client, 32.08, 34.78)

check("fetch_current_weather שולחת timezone=auto",
      client.params.get("timezone") == "auto", f"-> {client.params.get('timezone')!r}")
check("השעה המקומית עוברת כמו שהיא",
      result.get("time") == "2026-10-15T12:00", f"-> {result.get('time')!r}")
for key, want in (("timezone", "Asia/Jerusalem"), ("timezone_abbreviation", "IDT"),
                  ("utc_offset_seconds", 10800)):
    check(f"מחזירה {key}", result.get(key) == want, f"-> {result.get(key)!r}")
check("ערכי מזג האוויר לא נפגעו", result.get("uv_index") == 5.6, f"-> {result.get('uv_index')}")


# ---------------------------------------------------------------------
# 2. get_uv_forecast מבקשת שעון מקומי
# ---------------------------------------------------------------------
FORECAST_BODY = {
    "timezone": "Asia/Jerusalem",
    "timezone_abbreviation": "IDT",
    "utc_offset_seconds": 10800,
    "hourly": {"time": ["2026-10-16T13:00"], "uv_index": [6.2]},
}
sent = {}


class FakeAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, timeout=None):
        sent.update(params or {})
        return FakeResponse(FORECAST_BODY)


real_async_client = mws.httpx.AsyncClient
mws.httpx.AsyncClient = FakeAsyncClient
try:
    forecast = asyncio.run(mws.get_uv_forecast(32.08, 34.78, days=1))
finally:
    mws.httpx.AsyncClient = real_async_client

check("get_uv_forecast שולחת timezone=auto",
      sent.get("timezone") == "auto", f"-> {sent.get('timezone')!r}")
check("get_uv_forecast מחזירה את אזור הזמן",
      forecast.get("timezone_abbreviation") == "IDT", f"-> {forecast.get('timezone_abbreviation')!r}")
check("get_uv_forecast שומרת על הסדרה",
      forecast.get("uv_index") == [6.2], f"-> {forecast.get('uv_index')!r}")


# ---------------------------------------------------------------------
# 3. הכלל: כל קריאת מזג אוויר נוקבת באזור זמן במפורש
# ---------------------------------------------------------------------
# קריאת מזג אוויר מזוהה לפי מפתח current/hourly/daily ב-params. גיאוקודינג,
# הורדת תמונה ו-getUpdates לא נכללים, ובצדק — אין להם ציר זמן.
WEATHER_KEYS = ("current", "hourly", "daily")

for fname in ("geo_uv_core.py", "mcp_weather_server.py", "bot_commands.py"):
    text = pathlib.Path(ROOT, fname).read_text(encoding="utf-8")
    for m in re.finditer(r"params\s*=\s*\{", text):
        start = text.index("{", m.start())
        depth, k = 0, start
        while k < len(text):
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        block = text[start:k + 1]
        if not any(f'"{key}"' in block for key in WEATHER_KEYS):
            continue
        lineno = text[:m.start()].count("\n") + 1
        owner = (re.findall(r"\n\s*(?:async\s+)?def\s+(\w+)", text[:m.start()]) or ["?"])[-1]
        tz = re.search(r'"timezone"\s*:\s*"([^"]+)"', block)
        check(f"{fname}:{lineno} {owner} נוקב באזור זמן",
              tz is not None, f"-> {tz.group(1) if tz else 'חסר (ברירת המחדל GMT נכנסת בשקט)'}")

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
