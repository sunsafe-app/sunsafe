"""
בדיקה ידנית (לא pytest) לצמצום סבבי המודל בשאלה חופשית (2026-09-16).

**מה שנמדד בפרודקשן.** "מה ה-UV בתל אביב?" ב-16:07, תשובה ב-16:08 —
דקה. הלוג הראה חמש בקשות Gemini לשאלה אחת:

    גייטקיפר (נתקע ב-timeout של 5 שניות ונפל פתוח)
    chat.send_message(task)                 <- פתיחה
    סבב 0 -> geocode_city                   <- עוד קריאה
    סבב 1 -> get_current_uv                 <- עוד קריאה
    סבב 2 -> log_uv_reading                 <- עוד קריאה

שני מהסבבים האלה מיותרים:

1. **log_uv_reading** כותב ל-uv_readings, ואף אחד לא קורא משם — חיפוש
   בכל הריפו מחזיר רק כתיבות. ההערה ליד DEFAULT_SERVER_PARAMS ב-
   mcp_agent_loop.py אומרת במפורש שקריאות מה-Agent Loop *לא* נרשמות
   שם, אבל כל עוד הכלי חשוף למודל הוא קורא לו. הכוונה המתועדת והקוד
   לא הסכימו; עכשיו הם כן.
2. **geocode_city + get_current_uv** היו שני סבבים לשאלה אחת על
   "עכשיו". get_weather_for_city עושה את שניהם בקריאה אחת.

התוצאה: מחמש בקשות לשלוש. זה גם מוריד ~40% מזמן המודל וגם מעלה את
הקצב משלוש שאלות חופשיות בדקה לחמש (מכסת ה-tier החינמי, 15/דקה).

לא נוגעים ברשת.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

import mcp_agent_loop as mal
import mcp_weather_server as mws
import message_gatekeeper as mg

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1. הכלי המאוחד קיים ורשום בשרת
# ---------------------------------------------------------------------
check("get_weather_for_city קיים בשרת ה-MCP", hasattr(mws, "get_weather_for_city"))
check("וגם שני הכלים הנפרדים נשארו",
      hasattr(mws, "geocode_city") and hasattr(mws, "get_current_uv"))

# ---------------------------------------------------------------------
# 2. log_uv_reading מוסתר מהמודל — אבל לא מהשרת
# ---------------------------------------------------------------------
def fake_tool(name):
    return SimpleNamespace(name=name, description=f"desc {name}",
                           inputSchema={"type": "object", "properties": {}})


ALL = ["geocode_city", "get_weather_for_city", "get_current_uv",
       "calculate_exposure_score", "log_uv_reading", "get_uv_forecast",
       "get_historical_uv"]
declared = [d.name for d in mal.mcp_tools_to_function_declarations([fake_tool(n) for n in ALL])]

check("log_uv_reading לא נחשף למודל", "log_uv_reading" not in declared, f"-> {declared}")
check("get_weather_for_city כן נחשף", "get_weather_for_city" in declared)
check("ושאר הכלים לא נפגעו", len(declared) == len(ALL) - 1, f"-> {len(declared)} מתוך {len(ALL)}")
check("השרת עצמו ממשיך לחשוף את log_uv_reading", hasattr(mws, "log_uv_reading"))

# ---------------------------------------------------------------------
# 3. הכלי המאוחד עושה geocode ואז מזג אוויר, בקריאה אחת
# ---------------------------------------------------------------------
import asyncio

calls = []


class _Client:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake_geocode(client, name):
    calls.append(("geocode", name))
    if name == "nowhere":
        return {"found": False}
    return {"found": True, "name": "תל אביב-יפו", "country": "ישראל",
            "latitude": 32.08, "longitude": 34.78}


def fake_weather(client, lat, lon):
    calls.append(("weather", lat, lon))
    return {"uv_index": 3.6, "temperature_2m": 30.7,
            "cloud_cover": 0, "relative_humidity_2m": 56}


mws.core_geocode_city = fake_geocode
mws.fetch_current_weather = fake_weather
mws.httpx = SimpleNamespace(Client=_Client)

result = asyncio.run(mws.get_weather_for_city("תל אביב"))
check("מחזיר גם מיקום וגם מזג אוויר",
      result.get("found") and result.get("uv_index") == 3.6 and result.get("name") == "תל אביב-יפו",
      f"-> {result}")
check("ועשה בדיוק שתי קריאות רשת בקריאת כלי אחת",
      [c[0] for c in calls] == ["geocode", "weather"], f"-> {calls}")

calls.clear()
missing = asyncio.run(mws.get_weather_for_city("nowhere"))
check("עיר שלא נמצאה -> found=False בלי לנחש", missing == {"found": False, "query": "nowhere"},
      f"-> {missing}")
check("ולא נשלחה קריאת מזג אוויר על קואורדינטות מנוחשות",
      [c[0] for c in calls] == ["geocode"], f"-> {calls}")

# ---------------------------------------------------------------------
# 4. ה-prompt מפנה לכלי המאוחד
# ---------------------------------------------------------------------
import bot_commands as bc

task = bc._build_freeform_task("מה ה-UV בתל אביב?")
check("ה-prompt מזכיר את get_weather_for_city", "get_weather_for_city" in task)
check("ועדיין דורש geocode_city לתחזית ולעבר", "geocode_city" in task)

# ---------------------------------------------------------------------
# 5. ה-timeout של הגייטקיפר ירד
# ---------------------------------------------------------------------
import inspect
src = inspect.getsource(mg.classify_message)
check("timeout=2.0 בגייטקיפר", "timeout=2.0" in src)
check("ולא 5.0", "timeout=5.0" not in src)

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
