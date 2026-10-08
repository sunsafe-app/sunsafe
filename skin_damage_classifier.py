"""
SunSafe — הערכת נזק-שמש (Sunburn) מתמונה, אחרי חשיפה
--------------------------------------------------------
קריאה חד-פעמית ל-Gemini (multimodal) שמעריכה חומרת נזק-שמש/כוויית שמש
לפי תמונה שנשלחת *אחרי* חשיפה (בניגוד ל-skin_type_classifier.py, שמעריך
סוג עור *לפני* חשיפה — שני קבצים נפרדים בכוונה, כל אחד עם prompt/schema
משלו, כי אלה שתי שאלות שונות לגמרי על שתי תמונות שונות בזמנים שונים).

הוחלט לבנות את התכונה הזו ("גרסה מלאה") אחרי דיון מפורש עם המשתמש על
הסיכון שהועלה בסיכום הפגישה ("שאלות פרטיות בנוגע לתמונות משתמשים") —
המשתמש בחר במפורש להמשיך. בהתאם, נשמרים אותם עקרונות זהירות כמו ב-
skin_type_classifier.py: **לא** שומרת את התמונה עצמה בשום מקום (רק
bytes בזיכרון, נזרקים מיד אחרי הקריאה ל-Gemini — ראו handle_skin_damage
_photo ב-bot_commands.py). מה שכן נשמר ל-DB (ב-skin_damage_log, לפי
בקשת המשתמש) הוא רק *תוצאת ההערכה* (חומרה/ביטחון/המלצה/נימוק טקסטואלי)
— אף פעם לא פיקסלים.

זהירות נוספת ספציפית לתכונה הזו (מעבר למה שהיה ב-skin_type_classifier):
זו הערכה שנוגעת ישירות ל"האם יש נזק" — קרוב יותר לתחום רפואי מאשר
"מה סוג העור שלך", ולכן ה-prompt וגם הודעת התשובה למשתמש (ב-
bot_commands.py) חייבים לכלול ניסוח ברור של "לא ייעוץ רפואי, לא תחליף
לרופא" — במיוחד בחומרה moderate/severe, שם ההודעה חייבת להמליץ במפורש
לפנות לרופא/מיון אם יש שלפוחיות, חום, או הרגשה רעה כללית.
"""

import json
import logging
import os

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

logger = logging.getLogger("sunsafe.skin_damage_classifier")

# אותו מודל בו כבר משתמשים skin_type_classifier.py / mcp_agent_loop.py — עקביות.
CLASSIFIER_MODEL = "gemini-3.5-flash-lite"

# 4 רמות חומרה, מהקל לחמור. "none" חשוב כאפשרות תקינה (לא רק ברירת-
# מחדל של כשל) — אחרת משתמש עם עור תקין תמיד ייראה כמו "לא זוהה".
SEVERITY_LEVELS = ("none", "mild", "moderate", "severe")

SKIN_DAMAGE_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "detected": {
            "type": "BOOLEAN",
            "description": "true אם ניתן לזהות עור אנושי בבירור בתמונה.",
        },
        "severity": {
            "type": "STRING",
            "enum": list(SEVERITY_LEVELS),
            "description": (
                "רלוונטי רק אם detected=true. none=אין סימני נזק-שמש נראים; "
                "mild=אודם קל; moderate=אודם משמעותי/רגישות; "
                "severe=אודם עז, שלפוחיות, או נראה כמו כוויה משמעותית."
            ),
        },
        "confidence": {
            "type": "STRING",
            "enum": ["low", "medium", "high"],
        },
        "reasoning": {
            "type": "STRING",
            "description": "הסבר קצר בעברית (משפט אחד) למה נבחרה רמת החומרה הזו.",
        },
    },
    "required": ["detected", "confidence", "reasoning"],
}

CLASSIFICATION_PROMPT = (
    "התמונה המצורפת היא תצלום של עור אדם, שנשלח *אחרי* חשיפה לשמש, "
    "לצורך הערכה חזותית גסה של חומרת נזק-שמש/כוויית שמש אפשרית, כחלק "
    "מאפליקציית מעקב חשיפה לשמש בשם SunSafe.\n\n"
    "חשוב מאוד, וזה חייב להנחות את הניסוח שלך ב-reasoning: זו הערכה "
    "חזותית משוערת בלבד מתמונה, לא בדיקה רפואית ולא תחליף לרופא. "
    "אסור לך לנסח את זה כאבחנה רפואית. אם החומרה שאתה קובע היא "
    "moderate או severe, ה-reasoning שלך *חייב* לכלול המלצה מפורשת "
    "לפנות לרופא/מיון, בייחוד אם יש חשד לשלפוחיות, חום, או הרגשה רעה "
    "כללית — אלה מעבר למה שהערכת תמונה יכולה לשפוט.\n\n"
    "אם ניתן לזהות עור אנושי בבירור בתמונה: קבע detected=true, severity "
    "אחד מתוך none/mild/moderate/severe (none=עור תקין, בלי סימני נזק "
    "נראים; mild=אודם קל בלבד; moderate=אודם משמעותי או רגישות נראית "
    "לעין; severe=אודם עז, שלפוחיות, או מראה של כוויה משמעותית), "
    "confidence (low/medium/high לפי כמה ברור לך העור בתמונה), "
    "ו-reasoning קצר בעברית.\n\n"
    "אם התמונה לא מראה עור אנושי בבירור (לא רלוונטי, מטושטש, זווית/"
    "תאורה גרועה מדי כדי להעריך): קבע detected=false, השאר severity "
    "ריק, ותן reasoning שמסביר למה — למשל שהמשתמש צריך לשלוח תמונה "
    "ברורה יותר."
)


# 2026-10-08: ה-reasoning מוצג למשתמש כמו שהוא, אז הוא צריך להיות
# בשפה שלו. ה-prompt עצמו נשאר בעברית — המודל קורא אותו בכל מקרה.
_REASONING_LANGUAGE_SUFFIX = {
    "en": "\n\nחשוב: כתוב את שדה reasoning באנגלית (English), לא בעברית.",
}


def make_client() -> genai.Client:
    """Gemini Developer API — זהה ל-make_client ב-skin_type_classifier.py."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY לא מוגדר. הוסיפו אותו ל-.env. "
            "מקבלים מפתח חינמי דרך https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def _extract_usage(response) -> dict | None:
    """
    שולף את מספר הטוקנים בפועל (usage_metadata של ה-SDK) מתשובת Gemini —
    עותק מקביל בכוונה ל-_extract_usage ב-skin_type_classifier.py (שני
    קבצים נפרדים, ראו docstring למעלה). לצורך דיווח-עלות בזמן אמת דרך
    _notify_admin_token_usage ב-bot_commands.py.

    best-effort: usage_metadata הוא attribute של ה-SDK, לא חלק מה-JSON
    schema שאנחנו שולטים בו — אם המבנה חסר/משתנה בין גרסאות SDK, מעדיפים
    לוותר על הדיווח (מחזירים None) מאשר להפיל את כל קריאת הסיווג בגלל
    טלמטריה צדדית.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    try:
        return {
            "prompt_tokens": usage.prompt_token_count,
            "output_tokens": usage.candidates_token_count,
            "total_tokens": usage.total_token_count,
        }
    except AttributeError:
        return None


def classify_skin_damage_from_image(image_bytes: bytes, mime_type: str = "image/jpeg", lang: str = "he") -> dict:
    """
    שולח תמונה בודדת ל-Gemini ומחזיר את ה-JSON הגולמי (dict) לפי
    SKIN_DAMAGE_RESPONSE_SCHEMA, בתוספת מפתח "_usage" (dict עם
    prompt_tokens/output_tokens/total_tokens, או None אם לא זמין — ראו
    _extract_usage למעלה). אין כאן שום כתיבה ל-DB ואין שמירה של
    התמונה — הבייטים משמשים רק לקריאה הזו ונזרקים מיד אחריה (הקוד
    שקורא לפונקציה הזו, ב-bot_commands.py, לא שומר אותם לדיסק בשום שלב).

    מחזיר את הפלט הגולמי מהמודל בלבד — **חובה** להעביר דרך
    validate_classification לפני שימוש בפועל (לא סומכים עיוור על
    Structured Output, גם עם schema אכוף). validate_classification
    מתעלמת ממפתח "_usage" — קוראי הפונקציה הזו אמורים לחלץ אותו
    (raw.pop("_usage", None)) לפני/אחרי הוולידציה, לפי הצורך.
    """
    client = make_client()
    response = client.models.generate_content(
        model=CLASSIFIER_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            CLASSIFICATION_PROMPT + _REASONING_LANGUAGE_SUFFIX.get(lang, ""),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SKIN_DAMAGE_RESPONSE_SCHEMA,
        ),
    )
    raw = json.loads(response.text)
    raw["_usage"] = _extract_usage(response)
    logger.info("classify_skin_damage_from_image -> %s", raw)
    return raw


def validate_classification(raw: dict) -> dict:
    """
    פונקציה טהורה (בלי רשת/DB) שמאמתת ומנקה את הפלט הגולמי של המודל.
    אותו עיקרון כמו ב-skin_type_classifier.validate_classification —
    לא סומכים על Gemini גם עם response_schema אכוף (המודל עדיין יכול
    להחזיר detected=true בלי severity, ערך מחוץ ל-SEVERITY_LEVELS, וכו').

    מחזיר תמיד dict עם מפתח "ok":
    - ok=False, "reason": str — לא ניתן לזהות/הפלט לא תקין/חסר.
    - ok=True, "severity": str (אחד מ-SEVERITY_LEVELS), "confidence": str,
      "reasoning": str.
    """
    if not isinstance(raw, dict) or not raw.get("detected"):
        reason = (raw or {}).get("reasoning") or "לא זוהה עור אנושי בבירור בתמונה."
        return {"ok": False, "reason": reason}

    severity = raw.get("severity")
    if severity not in SEVERITY_LEVELS:
        return {"ok": False, "reason": f"המודל החזיר רמת חומרה לא תקינה ({severity!r})."}

    return {
        "ok": True,
        "severity": severity,
        "confidence": raw.get("confidence", "low"),
        "reasoning": raw.get("reasoning", ""),
    }
