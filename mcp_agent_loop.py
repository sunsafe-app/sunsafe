"""
SunSafe — Agent Loop מול MCP Weather Server
-----------------------------------------------
גרסה מעודכנת של agent_loop.py: במקום כלים מוגדרים ידנית ב-Python
(TOOLS dict), הכלים מגיעים דינמית משרת MCP (mcp_weather_server.py) —
ה-Agent Loop "לא יודע" איך בנוי ה-Weather API, הוא רק מדבר עם MCP.
זה מאפשר להחליף ספק (Open-Meteo -> משהו אחר) בלי לגעת בלוגיקת הסוכן,
ולהשתמש באותו שרת גם מ-Claude Desktop לבדיקות ידניות.

התקנה:
    pip install mcp google-genai python-dotenv httpx

הרצה כבדיקה עצמאית:
    python mcp_agent_loop.py
"""

import asyncio
import json
import logging
import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from rate_limit import spend_gemini

load_dotenv()

logger = logging.getLogger("sunsafe.mcp_agent")
logging.basicConfig(level=logging.INFO)

# הפעלת mcp_weather_server.py כתת-תהליך, בתקשורת stdio.
#
# חשוב: משתמשים ב-sys.executable (לא רק "python") כדי להריץ את השרת
# עם *אותו* אינטרפרטר בדיוק שמריץ את הקובץ הזה — כולל אותו venv עם
# החבילות המותקנות (mcp, httpx). אם משתמשים רק ב-"python", ב-Windows
# זה עלול להצביע על התקנת Python אחרת (בלי mcp/httpx מותקנים), מה
# שגורם לתת-התהליך לקרוס מיד עם ImportError — וללקוח זה נראה כמו
# "Connection closed" סתום בלי שום רמז לסיבה האמיתית.
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))

# env=os.environ.copy() — קריטי, לא קוסמטי: ה-SDK של MCP, כשלא מציינים
# env במפורש, *לא* מעביר לתת-התהליך את משתני הסביבה של התהליך האב —
# הוא מעביר רק רשימה בטוחה מצומצמת (HOME/PATH/USER/וכו', ראו
# mcp.client.stdio.get_default_environment). גילינו את זה כשראינו
# בפרודקשן ש-log_uv_reading נכשל תמיד עם "SUPABASE_URL ו/או
# SUPABASE_SERVICE_ROLE_KEY לא מוגדרים" — הסודות *כן* מוגדרים ב-Space
# (BOT_TOKEN/GEMINI_API_KEY עובדים מצוין), הם פשוט אף פעם לא הגיעו
# לתת-התהליך של mcp_weather_server.py. בלי זה, כל קריאת UV שעוברת דרך
# ה-Agent Loop (הודעות חופשיות) לא נרשמת בטבלת uv_readings.
DEFAULT_SERVER_PARAMS = StdioServerParameters(
    command=sys.executable,
    args=[os.path.join(_SERVER_DIR, "mcp_weather_server.py")],
    cwd=_SERVER_DIR,
    env=os.environ.copy(),
)


def make_client() -> genai.Client:
    """Gemini Developer API — מסלול הברירת מחדל של הקורס (API Key, ללא GCP)."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY לא מוגדר. הוסיפו אותו ל-.env. "
            "מקבלים מפתח חינמי דרך https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def _normalize_schema_types(schema: dict) -> dict:
    """
    MCP מחזיר JSON Schema עם types באותיות קטנות ("object", "string"),
    בעוד שה-SDK של Gemini מצפה לאותיות גדולות ("OBJECT", "STRING").
    הפונקציה הזו ממירה רקורסיבית בין הפורמטים.
    """
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            result[key] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            result[key] = {k: _normalize_schema_types(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = _normalize_schema_types(value)
        else:
            result[key] = value
    return result


# כלים שה-Agent Loop *לא* חושף ל-Gemini.
#
# log_uv_reading כותב לטבלת uv_readings, ואף אחד לא קורא ממנה — לא
# הדשבורד, לא ה-Edge Functions, ולא שום פקודה בבוט. חיפוש בכל הריפו
# מחזיר רק כתיבות. ההערה ליד DEFAULT_SERVER_PARAMS למטה אומרת במפורש
# שקריאות שעוברות דרך ה-Agent Loop *לא* נרשמות שם — אבל כל עוד הכלי
# חשוף למודל, הוא קורא לו. נמדד בפרודקשן ב-16.9.2026: שאלה אחת
# ("מה ה-UV בתל אביב?") גררה geocode_city, get_current_uv, ואז
# log_uv_reading — סבב מודל שלם, בשביל כתיבה שאף אחד לא צורך.
#
# השרת ממשיך לחשוף את הכלי כרגיל; ההסתרה היא בצד ה-Agent Loop בלבד,
# למי שכן ירצה אותו (למשל send_uv_report.py) דרך רשימה אחרת.
AGENT_HIDDEN_TOOLS = frozenset({"log_uv_reading"})


def mcp_tools_to_function_declarations(mcp_tools) -> list[types.FunctionDeclaration]:
    """ממיר רשימת Tools שהתקבלה מ-session.list_tools() ל-FunctionDeclaration של Gemini."""
    declarations = []
    for tool in mcp_tools:
        if tool.name in AGENT_HIDDEN_TOOLS:
            continue
        schema = _normalize_schema_types(
            tool.inputSchema or {"type": "OBJECT", "properties": {}}
        )
        declarations.append(
            types.FunctionDeclaration(
                name=tool.name,
                description=tool.description or "",
                parameters=schema,
            )
        )
    return declarations


def _parse_tool_result(result) -> object:
    """מחלץ תוצאה קריאה מתוך CallToolResult (לרוב TextContent עם JSON בפנים)."""
    texts = [c.text for c in result.content if hasattr(c, "text")]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return joined


# שגיאות שרת זמניות מ-Gemini. 503 נצפה בפרודקשן ב-15.10 והפיל את
# ההודעה החופשית הראשונה של המשתמשת החיצונית הראשונה — היא קיבלה
# הודעת שגיאה גנרית בגלל כשל שחלף תוך שניות. 429 (מכסה), 500 ו-504
# מאותה משפחה: כולם חולפים מעצמם.
#
# הגייטקיפר ידע לספוג את זה מההתחלה (fail open ל-VALID
# ב-message_gatekeeper.py). דווקא המסלול היקר יותר נשאר חשוף.
GEMINI_RETRY_CODES = frozenset({429, 500, 503, 504})
AGENT_RETRY_ATTEMPTS = int(os.environ.get("AGENT_RETRY_ATTEMPTS", "3"))
AGENT_RETRY_BASE_SECONDS = float(os.environ.get("AGENT_RETRY_BASE_SECONDS", "1.0"))


def _send_with_retry(chat, message, purpose: str):
    """
    chat.send_message עם ניסיונות חוזרים על שגיאת שרת זמנית.

    **למה בטוח לנסות שוב:** ה-SDK בונה את contents_to_model כרשימה
    מקומית וקורא ל-record_history רק *אחרי* ש-generate_content חוזר
    בהצלחה. קריאה שנכשלה לא השאירה את ההודעה בהיסטוריה, אז הניסיון
    החוזר שולח אותה פעם אחת ולא פעמיים.

    **כל ניסיון צורך טוקן מהתקציב** — זו באמת קריאה נוספת ל-Gemini,
    ו-rate_limit.py מודד קצב מול ה-API ולא מספר שאלות. אם התקציב
    מסרב, מפסיקים לנסות במקום לחרוג מהקצב.

    ההמתנה מעריכית ומתחילה בשנייה (1, 2). התקרה נמוכה בכוונה: המשתמש
    כבר ממתין בטלגרם, ועדיף להיכשל מהר מלהשאיר אותו תלוי.
    """
    for attempt in range(AGENT_RETRY_ATTEMPTS):
        label = f"{purpose} [{attempt + 1}/{AGENT_RETRY_ATTEMPTS}]"
        if not spend_gemini(label, timeout=20.0):
            raise RuntimeError(f"Gemini rate budget exhausted before {purpose}")
        try:
            return chat.send_message(message)
        except genai_errors.APIError as e:
            is_last = attempt == AGENT_RETRY_ATTEMPTS - 1
            if getattr(e, "code", None) not in GEMINI_RETRY_CODES or is_last:
                raise
            delay = AGENT_RETRY_BASE_SECONDS * (2 ** attempt)
            logger.warning(
                "%s failed with %s — retrying in %.0fs (attempt %d/%d)",
                purpose, e.code, delay, attempt + 1, AGENT_RETRY_ATTEMPTS,
            )
            time.sleep(delay)
    raise RuntimeError(f"{purpose}: retry loop ended without a result")


async def agent_loop_mcp(
    task: str,
    server_params: StdioServerParameters = DEFAULT_SERVER_PARAMS,
    max_iterations: int = 15,
) -> str:
    client = make_client()

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            tool_declarations = mcp_tools_to_function_declarations(tools_result.tools)
            logger.info(
                "Loaded %d tool(s) from MCP server: %s",
                len(tool_declarations),
                [t.name for t in tool_declarations],
            )

            chat = client.chats.create(
                model="gemini-3.5-flash-lite",
                config=types.GenerateContentConfig(
                    tools=[types.Tool(function_declarations=tool_declarations)]
                ),
            )
            # תקציב Gemini (16.9.2026). כל שאלה חופשית עולה כאן
            # *כמה* קריאות — אחת לפתיחה ואחת לכל סבב כלים — וזה
            # הנתיב היקר ביותר בבוט. ההמתנה ארוכה: המשתמש כבר ממתין
            # לתשובה, ו-20 שניות עדיפות על כשל. acquire חוסם, וזה
            # בסדר כאן — chat.send_message עצמה סינכרונית וחוסמת,
            # וה-event loop הזה מוקדש לשאלה הבודדת הזו בלבד.
            response = _send_with_retry(chat, task, "agent loop (opening)")

            for i in range(max_iterations):
                fn_calls = [
                    p.function_call
                    for p in response.candidates[0].content.parts
                    if p.function_call
                ]
                if not fn_calls:
                    return response.text  # סיום — אין עוד קריאות לכלים

                results = []
                for fc in fn_calls:
                    args = dict(fc.args)
                    logger.info("[iter %s] calling MCP tool %s(%s)", i, fc.name, args)
                    mcp_result = await session.call_tool(fc.name, args)
                    parsed = _parse_tool_result(mcp_result)
                    logger.info("[iter %s] tool=%s -> %s", i, fc.name, parsed)
                    results.append(
                        types.Part.from_function_response(
                            name=fc.name, response={"result": parsed}
                        )
                    )
                response = _send_with_retry(chat, results, f"agent loop (iteration {i})")

            raise RuntimeError(f"Agent exceeded {max_iterations} iterations")


def run(task: str, server_params: StdioServerParameters = DEFAULT_SERVER_PARAMS) -> str:
    """עטיפה סינכרונית נוחה לקריאה מקוד רגיל (למשל send_uv_report.py)."""
    return asyncio.run(agent_loop_mcp(task, server_params))


if __name__ == "__main__":
    answer = run(
        "מה ה-UV Index הנוכחי בתל אביב? (קואורדינטות: lat=32.08, lon=34.78). "
        "תן תשובה קצרה בעברית, כולל אם צריך הגנה מהשמש עכשיו."
    )
    print("תשובת הסוכן (דרך MCP):")
    print(answer)
