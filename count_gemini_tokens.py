"""
SunSafe — מה באמת עולה כל אינטראקציה, בטוקנים
================================================
מודד את הפרומפטים האמיתיים של הבוט מול Gemini דרך countTokens.

**למה זה דרך ולא הערכה:** ההערכה "4 תווים לטוקן" נכונה לאנגלית.
הפרומפטים כאן 59%-65% עברית, ועברית מתנהגת אחרת — לפי ההערכה שלנו
בין 1.5 ל-2.5 תווים לטוקן, כלומר פער של יותר מ-50% בין הקצוות. הסקריפט
הזה מחליף את הטווח במספר.

**countTokens חינמי ולא צורך מכסה** — הוא לא מריץ את המודל, רק מטקנז.
אפשר להריץ אותו כמה שרוצים בלי לשרוף כלום.

הרצה:
    python count_gemini_tokens.py
    python count_gemini_tokens.py --image assets/hand.jpg    # גם תמונה

לפי התיעוד הרשמי: תמונה עד 384px בשני הממדים = 258 טוקנים; גדולה
מזה מחולקת לאריחים של 768x768, כל אריח 258. --image מאמת את זה על
התמונה האמיתית שלך במקום להניח.
"""

import argparse
import base64
import inspect
import mimetypes
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

API = "https://generativelanguage.googleapis.com/v1beta/models"
# אותם מודלים שהבוט משתמש בהם בפועל.
GATEKEEPER_MODEL = "gemini-3.5-flash"
AGENT_MODEL = "gemini-3.5-flash-lite"


def count(model: str, contents: list, tools: list | None = None) -> int | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY לא מוגדר ב-.env")
        raise SystemExit(2)
    payload = {"contents": contents}
    if tools:
        payload["tools"] = tools
    try:
        r = httpx.post(f"{API}/{model}:countTokens?key={key}", json=payload, timeout=30.0)
        r.raise_for_status()
        return r.json().get("totalTokens")
    except httpx.HTTPStatusError as e:
        print(f"  שגיאה מ-Gemini ({e.response.status_code}): {e.response.text[:200]}")
        return None


def text_part(s: str) -> list:
    return [{"parts": [{"text": s}]}]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="נתיב לתמונה למדידה (למשל תצלום יד)")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import message_gatekeeper as mg
    import bot_commands as bc
    import mcp_weather_server as mws
    import mcp_agent_loop as mal
    import skin_type_classifier as stc

    rows = []

    print("מודד...\n")

    # --- הגייטקיפר: על כל הודעת טקסט שאינה פקודה ---
    n = count(GATEKEEPER_MODEL, text_part(mg.CLASSIFICATION_PROMPT.format(message="מה נשמע?")))
    rows.append(("gatekeeper, one message", n, GATEKEEPER_MODEL))

    # --- ה-Agent Loop: הפרומפט + הצהרות הכלים, שנשלחות מחדש בכל קריאה ---
    task = bc._build_freeform_task("מה ה-UV בתל אביב?")
    task_n = count(AGENT_MODEL, text_part(task))
    rows.append(("agent task prompt alone", task_n, AGENT_MODEL))

    hidden = getattr(mal, "AGENT_HIDDEN_TOOLS", set())
    decls = []
    for name in ("get_weather_for_city", "geocode_city", "get_current_uv",
                 "calculate_exposure_score", "get_uv_forecast", "get_historical_uv",
                 "log_uv_reading"):
        if name in hidden or not hasattr(mws, name):
            continue
        fn = getattr(mws, name)
        decls.append({"name": name, "description": inspect.getdoc(fn) or "",
                      "parameters": {"type": "object", "properties": {}}})
    both_n = count(AGENT_MODEL, text_part(task), tools=[{"function_declarations": decls}])
    rows.append((f"task + {len(decls)} tool declarations", both_n, AGENT_MODEL))

    # --- מסווג סוג העור ---
    n = count(AGENT_MODEL, text_part(stc.CLASSIFICATION_PROMPT))
    rows.append(("skin-type prompt, text only", n, AGENT_MODEL))

    if args.image:
        if not os.path.exists(args.image):
            print(f"לא נמצאה תמונה: {args.image}")
        else:
            mime = mimetypes.guess_type(args.image)[0] or "image/jpeg"
            b64 = base64.b64encode(open(args.image, "rb").read()).decode()
            n_img = count(AGENT_MODEL, [{"parts": [
                {"text": stc.CLASSIFICATION_PROMPT},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]}])
            rows.append((f"skin-type prompt + {os.path.basename(args.image)}", n_img, AGENT_MODEL))

    print(f"{'':42s} {'tokens':>8}  model")
    for label, n, model in rows:
        print(f"{label:42s} {str(n) if n is not None else '—':>8}  {model}")

    if both_n and task_n:
        print()
        print(f"הצהרות הכלים לבדן: {both_n - task_n} טוקנים — ואלה נשלחות מחדש")
        print("בכל קריאה ב-Agent Loop. זו הסיבה שצמצום סבבים חוסך יותר")
        print("טוקנים מכפי שהוא חוסך זמן.")
        print()
        print(f"שאלה חופשית לפני הצמצום (5 קריאות): ~{4 * both_n:,} טוקני קלט")
        print(f"אחרי (3 קריאות):                    ~{2 * both_n:,} טוקני קלט")


if __name__ == "__main__":
    main()
