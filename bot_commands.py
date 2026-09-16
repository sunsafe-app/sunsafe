"""
SunSafe — Bot Command Listener (polling)
-----------------------------------------
מאזין (polling, לא webhook) לארבע פקודות: /dashboard, /set_skin_type,
/start_session, /end_session — ולתמונות בודדות (הצעת סוג עור, ראו
skin_type_classifier.py). זהו שלב ביניים מינימלי — לא זרימת שיחה
מלאה עם כפתורים (TODO #5), ולא webhook production (TODO #8) — רק מספיק
כדי לאפשר את פיצ'ר "האזור האישי" בלי להמתין לשניהם. שדרוג לכפתורים
אמיתיים בהמשך לא ידרוש לשנות את מודל הנתונים.

הרצה:
    python bot_commands.py
    (משאירים רץ ברקע; Ctrl+C לעצירה)

תלות: משתמש ב-supabase_client.py הקיים (insert_row/select_rows/
update_rows/upsert_row) — REST ישיר מול PostgREST דרך httpx, בלי
SDK נוסף, עקבי עם שאר הקוד.
"""

import functools
import inspect
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from skin_type_classifier import classify_skin_type_from_image, validate_classification
from skin_damage_classifier import (
    classify_skin_damage_from_image,
    validate_classification as validate_damage_classification,
)
from supabase_client import SupabaseError, delete_rows, insert_row, select_rows, update_rows, upsert_row
from geo_uv_core import (
    calculate_exposure_score,
    safe_exposure_minutes,
    daily_exposure_score,
    daily_summary_he,
    geocode_city,
    get_current_uv,
    UvUnavailableError,
    _nominatim_forward_geocode,
    _text_matches,
    GEOCODING_URL,
    NOMINATIM_SEARCH_URL,
)
import i18n
from i18n import resolve_language, t
from message_gatekeeper import classify_message
# ניתוב הודעות-טקסט חופשיות (לא פקודה מוכרת, אבל לא NOISE) ל-Agent Loop
# דרך MCP — אותו run() בדיוק ש-send_uv_report.py כבר משתמש בו, ראו
# _handle_freeform_question למטה. שם ה-import (run_agent_via_mcp, לא run
# הגולמי) כדי לא להתנגש עם import time הקיים ולהיות ברור בנקודת הקריאה.
from mcp_agent_loop import run as run_agent_via_mcp

load_dotenv()

logger = logging.getLogger("sunsafe.bot_commands")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# httpx רושם ללוג (ברמת INFO) את ה-URL המלא של כל בקשה כברירת מחדל —
# ו-TELEGRAM_API למטה כולל את ה-BOT_TOKEN עצמו בתוך ה-URL (לא ב-header).
# בלי השורה הזו, כל send_message/send_photo/getUpdates מדליף את הטוקן
# ללוגים בטקסט גלוי — כולל ל-HF Space logs (Application Startup) שכבר
# נראים בפועל בהיסטוריה הזו. אותה בעיה קיימת גם ב-telegram_client.py
# וב-set_bot_profile.py, ותוקנה שם באותו אופן.
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.environ["BOT_TOKEN"]
# chat_id פרטי (לא של משתמש קצה) לדיווחי-טלמטריה פנימיים בלבד — כרגע רק
# דיווח טוקנים שנוצלו בכל תמונה שמסווגים (ראו _notify_admin_token_usage
# למטה). אופציונלי במכוון (os.environ.get, לא os.environ[...] כמו
# BOT_TOKEN): זו תכונת-נחמד-להיות-לי, לא חובה לפעולת הבוט — אם לא
# מוגדר, פשוט מדלגים על השליחה בלי לקרוס. כדי לקבל chat_id שלכם: שלחו
# הודעה כלשהי לבוט ואז ראו את chat_id בטבלת users (עמודת chat_id) מול
# ה-telegram_username שלכם.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

# שיקוף שיחות המשתמשים לצ'אט האדמין — **כבוי כברירת מחדל** (2026-09-14).
#
# המראה נבנה כדי לצפות בשימוש אמיתי בזמן פיתוח, אבל הוא מעביר את תוכן
# ההודעות של משתמשים אחרים — כולל מיקומים ותמונות של הגוף — לצ'אט פרטי.
# זה לא משהו שצריך לרוץ כברירת מחדל כשאנשים אמיתיים משתמשים בבוט, ולכן
# הוא דורש הפעלה מפורשת ולא רק ADMIN_CHAT_ID מוגדר.
#
# להפעלה זמנית (למשל דיבוג של תקלה אצל משתמש, בידיעתו):
#     MIRROR_MESSAGES_TO_ADMIN=1
#
# שים לב: זה *לא* משתיק את ההתראות התפעוליות לאדמין (צריכת טוקנים,
# כשל בשליחת גרף) — הן לא מכילות תוכן של משתמשים ותלויות ב-ADMIN_CHAT_ID
# בלבד. כדי לכבות גם אותן, הסר את ADMIN_CHAT_ID.
MIRROR_MESSAGES_TO_ADMIN = os.environ.get("MIRROR_MESSAGES_TO_ADMIN", "").strip().lower() in {
    "1", "true", "yes", "on",
}

DASHBOARD_BASE_URL = os.environ.get("DASHBOARD_BASE_URL", "http://localhost:8080")
# ה-Mini App לתיעוד session אופליין (docs/session/index.html). ברירת
# מחדל localhost כדי לא לשבור בדיקות מקומיות, בדיוק כמו DASHBOARD_BASE_URL.
# ראו docs/2026-08-29-offline-session-miniapp-design.md.
SESSION_MINIAPP_URL = os.environ.get("SESSION_MINIAPP_URL", "http://localhost:8080/session")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Reverse geocoding (lat/lon -> שם עיר) לשיתוף מיקום מהטלפון. Open-Meteo
# (המקור ל-geocode_city ב-geo_uv_core.py) תומך רק ב-forward geocoding —
# אין לו נתיב reverse, לכן Nominatim (OpenStreetMap): חינמי, בלי מפתח
# API. חובה User-Agent מזהה ומקסימום בקשה/שנייה לפי ה-Usage Policy
# הרשמי — לא בעיה בפועל כאן כי יש לכל היותר קריאה אחת לכל /start_session.
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_USER_AGENT = "SunSafe-Bot/1.0 (student course project)"

LINK_TTL_MINUTES = 60 * 24  # 24 שעות — נוח לשימוש חוזר בלי לוותר על תפוגה


# ---------------------------------------------------------------------
# /today — אנליזה יומית + "מה היה קורה עם קרם הגנה" (ראו handle_today
# למטה). קבוע, לא ניתן להגדרה ע"י המשתמש כרגע — הוחלט במפורש עם המשתמש
# ב-2026-09-08 (SPF 30 קבוע כברירת מחדל, לא פרמטר פתוח) כדי לשמור על
# MVP פשוט: השוואה אחידה, בלי צורך לפרש קלט חופשי.
# ---------------------------------------------------------------------
DAILY_SUMMARY_REFERENCE_SPF = 30


def _sessions_on_date(sessions: list[dict], target_date) -> list[dict]:
    """
    מסנן sessions לאלה שה-start_time שלהם (UTC, כמו כל שאר הזמנים באפליקציה)
    נופל בדיוק על target_date. פונקציה טהורה — בלי DB, קלה לבדיקה בנפרד
    מ-handle_today.
    """
    return [s for s in sessions if datetime.fromisoformat(s["start_time"]).date() == target_date]


def _daily_session_summary(session: dict, skin_type: int | None, reference_spf: int) -> dict:
    """
    עבור session סגור בודד (יש לו end_time+exposure_score): מחזירה dict
    עם הציון בפועל לצד ציון היפותטי אילו נעשה שימוש ב-reference_spf קבוע
    לאורך כל ה-session, במקום ה-spf שבאמת נרשם (כולל None). פונקציה
    טהורה — משתמשת רק ב-calculate_exposure_score הקיים, לא נוגעת ב-DB.
    """
    start_dt = datetime.fromisoformat(session["start_time"])
    end_dt = datetime.fromisoformat(session["end_time"])
    duration_minutes = (end_dt - start_dt).total_seconds() / 60
    hypothetical_score = calculate_exposure_score(session["uv_index"], duration_minutes, skin_type, reference_spf)
    return {
        "id": session["id"],
        "city": session["city"],
        "spf": session.get("spf"),
        "actual_score": session["exposure_score"],
        "hypothetical_score": hypothetical_score,
    }


def _peak_exposure_session(sessions: list[dict]) -> dict | None:
    """
    מחזירה את ה-session (מבין אלה עם exposure_score, כלומר סגורים) עם
    מדד החשיפה הגבוה ביותר, או None אם אין אף session סגור — "המיקום
    איפה שמד החשיפה היה הגבוה ביותר" (הוחלט עם המשתמש ב-2026-09-08).
    פונקציה טהורה, קלה לבדיקה בנפרד — משמשת גם את handle_today (שורת
    טקסט) וגם את render_daily_exposure_chart/send_daily_exposure_chart
    (הדגשה חזותית + כיתוב על הגרף). session פתוח (exposure_score=None)
    לא נכלל — אין לו עדיין ציון להשוות.
    """
    closed = [s for s in sessions if s.get("exposure_score") is not None]
    if not closed:
        return None
    return max(closed, key=lambda s: s["exposure_score"])


# ---------------------------------------------------------------------
# Nominatim — reverse geocoding (lat/lon -> שם עיר) לשיתוף מיקום מהטלפון.
# geocode_city/get_current_uv (forward geocoding + UV) עברו ל-geo_uv_core.py
# (ייבוא למעלה) — זה לא היה כפול באף מקום אחר, אז נשאר מקומי כאן.
# ---------------------------------------------------------------------
def reverse_geocode_location(client: httpx.Client, lat: float, lon: float) -> dict:
    """
    הופך lat/lon (משיתוף מיקום בטלגרם) לשם עיר, דרך Nominatim. ה-address
    שחוזר משתנה לפי סוג המקום — לא תמיד יש city נקי (כפר קטן וכו') — אז
    בודקים כמה שדות בסדר עדיפות ונופלים חזרה ל-found=False אם אף אחד לא
    קיים, בדיוק כמו geocode_city למעלה כשלא נמצאה עיר.
    """
    response = client.get(
        NOMINATIM_REVERSE_URL,
        params={"lat": lat, "lon": lon, "format": "json", "accept-language": "he"},
        headers={"User-Agent": NOMINATIM_USER_AGENT},
        timeout=10.0,
    )
    response.raise_for_status()
    address = response.json().get("address") or {}
    city = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
    )
    if not city:
        return {"found": False}
    return {"found": True, "name": city, "country": address.get("country")}


# ---------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------
def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    with httpx.Client() as client:
        response = client.post(
            f"{TELEGRAM_API}/sendMessage",
            json=payload,
            timeout=10.0,
        )
        response.raise_for_status()
    _mirror_outgoing_to_admin(chat_id, text)


def send_photo(
    chat_id: int,
    photo_bytes: bytes,
    caption: str | None = None,
    reply_markup: dict | None = None,
) -> None:
    """
    שולח תמונה בודדת ל-Telegram (sendPhoto, multipart/form-data — בשונה
    מ-send_message למעלה שהוא JSON טהור). לא היה בשימוש עד כה בפרויקט
    (רק sendMessage); נדרש עבור תרשים תחזית ה-UV (send_uv_forecast_chart).

    reply_markup נוסף ב-2026-09-12 עבור בורר סוג-העור בלחיצה (ראו
    _skin_type_keyboard): הכפתורים צריכים לשבת מתחת לתמונת הסולם עצמה.
    ב-multipart, בשונה מבקשת JSON, טלגרם מצפה ל-reply_markup כמחרוזת
    JSON בתוך השדה — לא כאובייקט מקונן.
    """
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)

    with httpx.Client() as client:
        response = client.post(
            f"{TELEGRAM_API}/sendPhoto",
            data=data,
            files={"photo": ("uv_forecast.png", photo_bytes, "image/png")},
            timeout=15.0,
        )
        response.raise_for_status()
    _mirror_outgoing_photo_to_admin(chat_id, photo_bytes, caption)


def answer_callback_query(callback_query_id: str, text: str | None = None) -> None:
    """
    סוגר את "ספינר ההמתנה" שטלגרם מציג על כפתור inline אחרי לחיצה.
    חובה לקרוא לזה על *כל* callback_query — אחרת הכפתור נראה תקוע
    למשתמש גם אם הפעולה עצמה הצליחה מזמן.

    best-effort: כשל כאן לא אמור להפיל את הטיפול בלחיצה עצמה (הנתון
    כבר נשמר), אז רק נרשם ללוג.
    """
    try:
        with httpx.Client() as client:
            response = client.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": callback_query_id, **({"text": text} if text else {})},
                timeout=10.0,
            )
            response.raise_for_status()
    except Exception as e:
        logger.warning("answerCallbackQuery failed (id=%s): %s", callback_query_id, e)


# ---------------------------------------------------------------------
# "מראה מלא" לאדמין — כל הודעה נכנסת מכל משתמש (_mirror_incoming_to_admin,
# נקרא מ-handle_update) וכל תשובה יוצאת מהבוט לכל משתמש (_mirror_outgoing_*,
# נקרא מ-send_message/send_photo עצמן — כך שכל קריאה קיימת/עתידית מכוסה
# בלי לגעת בעשרות מקומות הקריאה בקוד). לא קשור ל-_notify_admin_token_usage/
# _notify_admin_chart_skip הקיימים (טלמטריה ממוקדת) — זה מראה-כללי, כל
# ה-conversation, בשני הכיוונים. שלושתן חולקות את אותו דפוס: best-effort,
# כשל בשליחת המראה עצמה לא זורק ולא משפיע על מה שכבר קרה עם המשתמש האמיתי.
#
# **כבוי כברירת מחדל.** שלושתן עוברות דרך _mirroring_enabled, שדורש גם
# ADMIN_CHAT_ID וגם MIRROR_MESSAGES_TO_ADMIN=1 (ראו ההסבר ליד ההגדרה
# למעלה). ADMIN_CHAT_ID לבדו כבר לא מספיק — הוא נשאר בשימוש להתראות
# התפעוליות, שלא מכילות תוכן של משתמשים.
#
# ה-guard str(chat_id) == str(ADMIN_CHAT_ID) חשוב בשני הכיוונים: (א)
# כשהאדמין עצמו הוא זה שמדבר עם הבוט (למשל בדיקות) — לא רוצים למראות
# לו את השיחה של עצמו בחזרה אליו. (ב) מונע רקורסיה אינסופית: send_message
# בתוך _mirror_outgoing_to_admin עצמה קוראת שוב ל-_mirror_outgoing_to_admin,
# אבל הפעם עם chat_id==ADMIN_CHAT_ID, אז היא עוצרת שם.
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# "כמה זמן אפשר להיות בשמש" — הצגת safe_exposure_minutes למשתמש
# ---------------------------------------------------------------------
# נוסף 2026-09-15 בעקבות בדיקת הצוות. הנוסחה תמיד חישבה את המספר הזה
# ומיד זרקה אותו; המשתמש קיבל רק אחוז. ראו safe_exposure_minutes
# ב-geo_uv_core.py לרקע המלא.
#
# **הסף של השעתיים הוא החלטה בטיחותית, לא עיגול.** לפי הנוסחה הנוכחית
# (effective_spf = 1 + (spf-1)*0.4), סוג עור 3 ב-UV 6.3 עם SPF 30 מקבל
# 6.7 שעות, וסוג עור 6 עם SPF 50 מקבל 34 שעות. להציג מספרים כאלה זה
# להבטיח למשתמש שהוא מוגן כל היום. ההנחיה המקובלת היא למרוח מחדש כל
# שעתיים ללא קשר ל-SPF, אז כל ערך מעל זה נחתך — ותמיד עם המשפט על
# המריחה החוזרת לצדו.
SUNSCREEN_REAPPLY_MINUTES = 120


def format_duration_he(minutes: float) -> str:
    """דקות -> טקסט עברי טבעי. 32 -> "כ-32 דקות", 105 -> "כשעה ו-45 דקות"."""
    total = round(minutes)
    if total < 60:
        return f"כ-{total} דקות"

    hours, mins = divmod(total, 60)
    if hours == 1:
        head = "כשעה"
    elif hours == 2:
        head = "כשעתיים"
    else:
        head = f"כ-{hours} שעות"

    # פחות מ-5 דקות עודפות זה רעש בהערכה כזו, לא דיוק.
    return head if mins < 5 else f"{head} ו-{mins} דקות"


def safe_exposure_line(uv_index: float, skin_type: int) -> str | None:
    """
    השורה שמסבירה למשתמש כמה זמן הוא יכול להיות בשמש עכשיו.

    מחזירה None כשאין מה לומר (UV אפס, או שאין סוג עור) — אז נקודת
    הקריאה פשוט מדלגת עליה במקום להציג שורה ריקה.
    """
    if not skin_type:
        return None
    bare = safe_exposure_minutes(uv_index, skin_type)
    if bare is None:
        return None

    with_spf = safe_exposure_minutes(uv_index, skin_type, 30)
    if with_spf and with_spf > SUNSCREEN_REAPPLY_MINUTES:
        # לא נוקבים במספר שגדול מזמן המריחה החוזרת — ראו ההערה למעלה.
        spf_part = "קרם הגנה מאריך את הזמן הזה"
    else:
        spf_part = f"קרם הגנה מאריך את הזמן הזה ל{format_duration_he(with_spf)}"

    return (
        f"לפי סוג העור שלכם, {format_duration_he(bare)} בשמש ישירה "
        "עד סיכון לכוויה, בלי הגנה.\n"
        f"{spf_part} — אבל חשוב למרוח כמות מספקת, ולחדש כל שעתיים."
    )


def _mirroring_enabled(chat_id: int) -> bool:
    """
    האם למראות את השיחה הזו לאדמין.

    קורא את MIRROR_MESSAGES_TO_ADMIN דרך המודול (ולא כקבוע שנלכד ביבוא)
    כדי שאפשר יהיה להדליק/לכבות אותו בבדיקות בלי לטעון את המודול מחדש.
    """
    if not ADMIN_CHAT_ID or not MIRROR_MESSAGES_TO_ADMIN:
        return False
    # האדמין מדבר עם הבוט בעצמו — אין טעם למראות לו את עצמו, וזה גם מה
    # שעוצר את הרקורסיה (ראו ההסבר למעלה).
    return str(chat_id) != str(ADMIN_CHAT_ID)


def _mirror_incoming_to_admin(
    chat_id: int, username: str | None, text: str, photo_sizes: list | None, location: dict | None
) -> None:
    """רץ *לפני* הגייטקיפר בכוונה — האדמין רואה גם מה מסונן כ-NOISE, לא רק מה שבאמת מטופל."""
    if not _mirroring_enabled(chat_id):
        return
    if text:
        content = text
    elif photo_sizes:
        content = "[תמונה]"
    elif location:
        content = f"[מיקום: {location.get('latitude')}, {location.get('longitude')}]"
    else:
        content = "[הודעה לא נתמכת]"
    try:
        send_message(ADMIN_CHAT_ID, f"📥 [{chat_id}] @{username or '?'}: {content}")
    except Exception as e:
        logger.warning("Failed to mirror incoming message to admin (chat_id=%s): %s", chat_id, e)


def _mirror_outgoing_to_admin(chat_id: int, text: str) -> None:
    if not _mirroring_enabled(chat_id):
        return
    try:
        send_message(ADMIN_CHAT_ID, f"📤 [{chat_id}] {text}")
    except Exception as e:
        logger.warning("Failed to mirror outgoing message to admin (chat_id=%s): %s", chat_id, e)


def _mirror_outgoing_photo_to_admin(chat_id: int, photo_bytes: bytes, caption: str | None) -> None:
    if not _mirroring_enabled(chat_id):
        return
    try:
        send_photo(ADMIN_CHAT_ID, photo_bytes, caption=f"📤 [{chat_id}] {caption or ''}".strip())
    except Exception as e:
        logger.warning("Failed to mirror outgoing photo to admin (chat_id=%s): %s", chat_id, e)


def prompt_location_share(chat_id: int) -> None:
    """
    שולח כפתור "שתפו מיקום" מובנה של טלגרם (request_location) — לחיצה
    עליו גורמת ללקוח לשלוח הודעת location עם lat/lon אמיתיים מה-GPS,
    בלי שום קוד custom בצד הלקוח (לא Mini App). ראו
    docs/2026-08-26-location-sharing-design.md.
    """
    send_message(
        chat_id,
        "אפשר להתחיל session ישירות מהמיקום שלכם — לחצו על הכפתור למטה, "
        "או שלחו /start_session <שם עיר> (או קואורדינטות, למשל \"32.08, 34.78\") ידנית.",
        reply_markup={
            "keyboard": [[{"text": "📍 שתפו מיקום", "request_location": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        },
    )


def download_telegram_photo(client: httpx.Client, file_id: str) -> bytes:
    """
    מוריד את בייטס התמונה בפועל מטלגרם, לפי file_id. שני שלבים: getFile
    (מחזיר file_path זמני) ואז הורדה מ-.../file/bot<token>/<file_path>.
    לא שומר לדיסק בשום שלב — מחזיר bytes בזיכרון בלבד; קורא(י)ם ל-
    handle_skin_type_photo זורקים אותם מיד אחרי השימוש (ראו שם).
    """
    resp = client.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=10.0)
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]

    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    file_resp = client.get(file_url, timeout=15.0)
    file_resp.raise_for_status()
    return file_resp.content


# ---------------------------------------------------------------------
# דיווח טוקנים ל-admin — לא ל-webhook נפרד (הבוט כבר מגיב מיידית לכל
# תמונה נכנסת דרך לולאת ה-polling עצמה, ראו handle_update למטה), אלא
# הודעת Telegram נוספת שנשלחת רק ל-ADMIN_CHAT_ID (לא למשתמש שהעלה את
# התמונה) בכל פעם שמתבצעת קריאת Gemini לסיווג תמונת-עור, כדי לעקוב אחרי
# עלות בזמן אמת. ראו _extract_usage ב-skin_type_classifier.py /
# skin_damage_classifier.py למקור המספרים.
# ---------------------------------------------------------------------
def _notify_admin_token_usage(feature: str, username: str, usage: dict | None) -> None:
    """
    best-effort בלבד, בכוונה: אם ADMIN_CHAT_ID לא מוגדר, אם ל-usage אין
    ערך (למשל ה-SDK לא החזיר usage_metadata), או אם השליחה עצמה נכשלת
    (Telegram API) — רק רושמים ללוג ולא זורקים. זו טלמטריה צדדית; אסור
    לה לשבש את התשובה שכבר נשלחה למשתמש עצמו (ולכן נקראת רק אחרי
    שהתקבלה תשובה תקינה מ-Gemini, לא בתוך אותו try/except שמטפל בכשל
    הסיווג עצמו).
    """
    if not ADMIN_CHAT_ID:
        logger.debug("ADMIN_CHAT_ID not set — skipping token-usage admin notification")
        return
    if not usage:
        logger.debug("No usage metadata available for %s/@%s — skipping admin notification", feature, username)
        return
    try:
        send_message(
            ADMIN_CHAT_ID,
            f"📊 {feature} | @{username} | טוקנים: קלט={usage.get('prompt_tokens')}, "
            f"פלט={usage.get('output_tokens')}, סה\"כ={usage.get('total_tokens')}",
        )
    except Exception as e:
        logger.warning("Failed to send admin token-usage notification (%s/@%s): %s", feature, username, e)


# ---------------------------------------------------------------------
# תמונה נכנסת — הצעת סוג עור (Fitzpatrick) בלבד, לא כתיבה ל-DB
# ---------------------------------------------------------------------
def handle_skin_type_photo(
    chat_id: int, username: str, photo_file_id: str, lang: str = i18n.DEFAULT_LANGUAGE
) -> None:
    """
    מוריד תמונה שנשלחה לבוט, שולח אותה ל-Gemini להערכת סוג עור (הצעה
    בלבד — ראו skin_type_classifier.py ו-docs/2026-08-26-skin-type-photo
    -design.md), ומבקש מהמשתמש לאשר/לתקן.
    הפונקציה הזו **לא** כותבת ל-users בעצמה — בכוונה, כדי שערך בטיחותי
    (הבסיס ל-exposure_score) תמיד יעבור אישור אנושי מפורש.

    מ-2026-09-12 האישור והתיקון הם בלחיצה ולא בהקלדת /set_skin_type:
    מי שהגיע לכאן עשה זאת בדיוק *כי* הוא לא בטוח באיזה סוג הוא, ולבקש
    ממנו בנקודה הזו להקליד פקודה עם מספר היה הדבר הפחות מתאים.
    """
    with httpx.Client() as client:
        photo_bytes = download_telegram_photo(client, photo_file_id)

    try:
        raw = classify_skin_type_from_image(photo_bytes)
    except Exception as e:
        logger.warning("classify_skin_type_from_image failed for @%s: %s", username, e)
        send_message(chat_id, t("photo_analysis_failed", lang), reply_markup=_skin_type_keyboard(lang))
        return

    _notify_admin_token_usage("skin_type", username, raw.pop("_usage", None))
    result = validate_classification(raw)
    if not result["ok"]:
        send_message(
            chat_id,
            t("photo_rejected", lang, reason=result["reason"]),
            reply_markup=_skin_type_keyboard(lang),
        )
        logger.info("Photo skin-type classification rejected for @%s: %s", username, result)
        return

    send_message(
        chat_id,
        t(
            "photo_suggestion", lang,
            skin_type=result["skin_type"],
            reasoning=result["reasoning"],
            confidence=result["confidence"],
        ),
        reply_markup=_skin_type_confirm_keyboard(result["skin_type"], lang),
    )
    logger.info("Photo skin-type suggestion for @%s: %s", username, result)


# ---------------------------------------------------------------------
# /diagnose_skin — הערכת נזק-שמש מתמונה, אחרי חשיפה
# ---------------------------------------------------------------------
# הבוט הזה חסר state-tracking אמיתי (כל handler בודד/stateless, נשען
# על ה-DB) — אין שום דרך קיימת "לזכור" בין הודעה להודעה. תמונה נכנסת
# הייתה עד עכשיו תמיד מנותבת ל-handle_skin_type_photo (ראו handle_update
# למטה). כדי ש-/diagnose_skin יוכל "לתפוס" את התמונה הבאה של המשתמש
# בלי לשבור את זה, יש כאן דגל pending קטן בזיכרון בלבד (לא ב-DB —
# זה מצב שיחה חולף בסדר גודל של דקות, לא נתון עסקי שצריך לשרוד
# restart של ה-process; אם ה-Space נופל/קם בדיוק בין הפקודה לתמונה,
# המשתמש פשוט חוזר להתנהגות ברירת המחדל הקיימת — הצעת סוג עור, לא
# קריסה). תוקף קצר (10 דקות) כדי שדגל ישן לא "יתפוס" תמונה לא קשורה
# ששולחים הרבה יותר מאוחר.
_PENDING_DIAGNOSE_SKIN_TTL_MINUTES = 10
_pending_diagnose_skin: dict[str, datetime] = {}

# אותו דפוס בדיוק, בשביל בחירת סוג-עור "אינטראקטיבית": אחרי שרואים את
# סולם Fitzpatrick (ב-/start) או מקבלים הודעת-שימוש מ-/set_skin_type,
# תגובה טבעית היא לשלוח סתם ספרה בודדת ("3") — לא "/set_skin_type 3"
# המלא. בלי ה-flag הזה, ספרה בודדת (≤2 תווים) נבלעת בשקט ע"י הגייטקיפר
# (fast-path לטקסט קצר = NOISE) עוד לפני שיש סיכוי להבין שזו תשובה
# לשאלה שהבוט עצמו שאל — בדיוק מה שקרה בפועל (נצפה 2026-09-10: משתמש
# שלח "3" בתגובה להודעת-שימוש, ולא קרה שום דבר). ראו handle_update
# למטה לנקודת הניתוב, וההגדרה ב-handle_start/handle_set_skin_type.
_PENDING_SKIN_TYPE_TTL_MINUTES = 15
_pending_skin_type_pick: dict[str, datetime] = {}


def _mark_pending_skin_type_pick(username: str) -> None:
    _pending_skin_type_pick[username] = datetime.now(timezone.utc) + timedelta(
        minutes=_PENDING_SKIN_TYPE_TTL_MINUTES
    )


def handle_diagnose_skin(chat_id: int, username: str, args: str) -> None:
    _pending_diagnose_skin[username] = datetime.now(timezone.utc) + timedelta(
        minutes=_PENDING_DIAGNOSE_SKIN_TTL_MINUTES
    )
    send_message(
        chat_id,
        "☀️ שלחו עכשיו תמונה ברורה של האזור בעור שנחשף לשמש (התמונה משמשת "
        "רק להערכה הזו ולא נשמרת בשום מקום).\n\n"
        "⚠️ חשוב: זו הערכה חזותית של בינה מלאכותית בלבד — לא אבחנה רפואית "
        "ולא תחליף לרופא. אם משהו מדאיג אתכם (כאב חזק, שלפוחיות, חום), "
        "פנו לרופא/מיון גם בלי לחכות לתשובה כאן.",
    )


def _most_recent_session_id(username: str) -> int | None:
    """session_id לקישור תוצאת האבחון (open או closed, הכי עדכני) — או None אם אין בכלל."""
    sessions = select_rows(
        "exposure_log",
        {"telegram_username": f"eq.{username}", "order": "start_time.desc", "limit": "1"},
    )
    return sessions[0]["id"] if sessions else None


def handle_skin_damage_photo(chat_id: int, username: str, photo_file_id: str) -> None:
    """
    מוריד תמונה שנשלחה כתגובה ל-/diagnose_skin, שולח אותה ל-Gemini
    להערכת חומרת נזק-שמש (ראו skin_damage_classifier.py), שולח למשתמש
    תשובה עם ניסוח זהיר (לא ייעוץ רפואי; המלצה מפורשת לפנות לרופא
    ב-moderate/severe), וכותב שורת תוצאה ל-skin_damage_log — התמונה
    עצמה נזרקת מיד אחרי הקריאה ל-Gemini, לא נשמרת בשום מקום.
    """
    with httpx.Client() as client:
        photo_bytes = download_telegram_photo(client, photo_file_id)

    try:
        raw = classify_skin_damage_from_image(photo_bytes)
    except Exception as e:
        logger.warning("classify_skin_damage_from_image failed for @%s: %s", username, e)
        send_message(chat_id, "לא הצלחתי לנתח את התמונה כרגע. נסו שוב עם /diagnose_skin.")
        return

    _notify_admin_token_usage("diagnose_skin", username, raw.pop("_usage", None))
    result = validate_damage_classification(raw)
    if not result["ok"]:
        send_message(
            chat_id,
            f"לא הצלחתי להעריך את התמונה הזו ({result['reason']}). נסו תמונה "
            "ברורה יותר של האזור עם /diagnose_skin.",
        )
        logger.info("Photo skin-damage classification rejected for @%s: %s", username, result)
        return

    severity = result["severity"]
    severity_labels = {
        "none": "לא נראים סימני נזק",
        "mild": "אודם קל",
        "moderate": "אודם משמעותי",
        "severe": "אודם עז / חשד לכוויה משמעותית",
    }
    lines = [f"הערכה: {severity_labels[severity]} (ביטחון: {result['confidence']}).", result["reasoning"]]
    if severity in ("moderate", "severe"):
        lines.append(
            "⚠️ מומלץ לפנות לרופא/מיון, בייחוד אם יש שלפוחיות, חום, או הרגשה רעה כללית."
        )
    lines.append("\nתזכורת: זו הערכה חזותית של בינה מלאכותית בלבד, לא אבחנה רפואית.")
    send_message(chat_id, "\n".join(lines))

    try:
        insert_row(
            "skin_damage_log",
            {
                "telegram_username": username,
                "session_id": _most_recent_session_id(username),
                "severity": severity,
                "confidence": result["confidence"],
                "reasoning": result["reasoning"],
            },
        )
    except SupabaseError as e:
        # התשובה כבר נשלחה למשתמש — כשל בשמירה ללוג/היסטוריה לא אמור
        # לגרום להודעת שגיאה נוספת שרק תבלבל (התוצאה עצמה כבר נמסרה).
        logger.error("Failed to save skin_damage_log for @%s: %s", username, e)

    logger.info("Skin-damage assessment for @%s: %s", username, result)


# ---------------------------------------------------------------------
# /dashboard — Magic Link
# ---------------------------------------------------------------------
def create_magic_link(telegram_username: str) -> str:
    """
    יוצר טוקן אקראי חסין-ניחוש (32 בייטים), שומר אותו בטבלת magic_links
    יחד עם telegram_username ותאריך תפוגה, ומחזיר את ה-URL המלא לשליחה
    בטלגרם. הטבלה הזו נגישה רק ל-service_role — אין לה policies
    שמאפשרים גישה מ-anon/authenticated, כך שרק קוד שרת (הבוט הזה,
    ובהמשך ה-Edge Function) יכולים לגעת בה.
    """
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=LINK_TTL_MINUTES)

    insert_row(
        "magic_links",
        {
            "token": token,
            "telegram_username": telegram_username,
            "expires_at": expires_at.isoformat(),
            "used": False,
        },
    )

    logger.info("Created magic link for @%s (expires %s)", telegram_username, expires_at)
    return f"{DASHBOARD_BASE_URL}/?token={token}"


# assets/ — תמונת עזר סטטית (לא נוצרת דינמית כמו הגרפים) לסולם
# Fitzpatrick, מוצגת ב-/start כדי לעזור למשתמש חדש לבחור סוג עור
# ויזואלית, במקום לנחש מספר בין 1-6 בלי שום הקשר. זה בעצם גרסה ראשונה,
# פשוטה, של "Color-swatch skin-type picker" שהמנחה ביקש בסקירה
# מ-2026-08-20 (עדיין פתוח שם כ"לחצן/צבע אינטראקטיבי" — זו רק התמונה
# כהתחלה, לא ה-UI האינטראקטיבי המלא).
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_fitzpatrick_scale_bytes: bytes | None = None


def _load_fitzpatrick_scale_image() -> bytes | None:
    """
    טוען את תמונת סולם Fitzpatrick פעם אחת ושומר בזיכרון (לא קורא
    מהדיסק בכל /start). מחזיר None אם הקובץ חסר (לא מפיל את /start
    כולו רק כי תמונת עזר לא נמצאה — שולחים את הטקסט בכל מקרה).
    """
    global _fitzpatrick_scale_bytes
    if _fitzpatrick_scale_bytes is None:
        path = os.path.join(_ASSETS_DIR, "fitzpatrick_scale.png")
        try:
            with open(path, "rb") as f:
                _fitzpatrick_scale_bytes = f.read()
        except OSError as e:
            logger.warning("Could not load Fitzpatrick scale image (%s): %s", path, e)
            return None
    return _fitzpatrick_scale_bytes


# ---------------------------------------------------------------------
# /start — טלגרם שולח את זה אוטומטית בכל פעם שמשתמש חדש לוחץ "Start"
# בפעם הראשונה בצ'אט עם הבוט. עד 2026-09-10 לא היה handler בכלל ל-
# "/start" (לא ב-COMMAND_HANDLERS), אז /start עבר את ה-gatekeeper כ-
# VALID (זו כן פקודה לגיטימית) ואז נבלע בשקט ב-handle_update כי אין
# handler תואם — משתמש חדש שלוחץ Start מקבל *שתיקה מוחלטת*, בלי שום
# רמז לאיך בכלל להשתמש בבוט. נצפה בפועל: משתמש חדש (@HRazHad) שלח
# /start ואז ניסה טקסט חופשי ("5", "3"...) בלי הצלחה, עד שהגיע במקרה
# ל-/set_skin_type הנכון. זו נקודת הכניסה הראשונה של כל משתמש חדש —
# הכי חשוב שלא תהיה שתיקה.
# ---------------------------------------------------------------------
# ---------------------------------------------------------------------
# בורר סוג העור — כפתורים, לא הקלדה
# ---------------------------------------------------------------------
# נוסף 2026-09-12 בעקבות ההערה מהפגישה ("Focus on making it as easy as
# possible for the user, so someone with not much technical ability can
# use it"): המכשול הראשון של כל משתמש חדש היה להקליד מספר בין 1 ל-6 —
# פעולה שדורשת להבין מה זה סולם Fitzpatrick *וגם* להקליד. עכשיו זו
# לחיצה אחת.
#
# התוויות מיועדות לעמוד בפני עצמן בלי התמונה: מי שמסתכל רק על הכפתורים
# מקבל תיאור מילולי של הגוון ולא רק מספר.
#
# ספרות רגילות ולא רומיות (תוקן 2026-09-12, מיד אחרי הגרסה הראשונה):
# סולם Fitzpatrick נכתב מקורית ב-I-VI, וגם תמונת העזר מציגה כך — אבל
# ספרות רומיות לא מובנות לכולם, וזה בדיוק המכשול שניסינו להסיר כאן.
# 1-6 גם עקבי עם כל שאר השפה של הבוט ועם הערך שנשמר ב-DB.
#
# מ-2026-09-14 התוויות עצמן מגיעות מ-i18n (עברית/אנגלית) — ראו
# i18n.skin_type_label. הקבוע הזה נשאר לשימושים שאין להם הקשר שפה.
SKIN_TYPE_LABELS = {n: i18n.skin_type_label(n) for n in range(1, 7)}

# ה-prefix ב-callback_data מאפשר להוסיף בעתיד סוגי כפתורים נוספים בלי
# להתנגש (ראו handle_callback_query). טלגרם מגביל את callback_data
# ל-64 בייטים — "skin:3" רחוק מזה.
SKIN_TYPE_CALLBACK_PREFIX = "skin:"
# שני suffix-ים שאינם מספר: בקשה לצלם את היד, וחזרה לבורר אחרי הצעה.
SKIN_CALLBACK_PHOTO = "photo"
SKIN_CALLBACK_AGAIN = "again"


def _skin_type_keyboard(lang: str = i18n.DEFAULT_LANGUAGE) -> dict:
    """
    שש כפתורי סוג-עור, שניים בשורה (קריא גם במסך צר), ומתחתיהם שורה
    שלמה למי שלא בטוח — צילום היד.

    כפתור הצילום נוסף 2026-09-12: גם עם התמונה והתיאורים המילוליים,
    השאלה "איזה מהשישה אני?" לא טריוויאלית, וזה בדיוק המקום שבו משתמש
    חדש נתקע. טלגרם לא מאפשר כפתור שפותח מצלמה (יש request_location
    ו-request_contact, אין request_photo), אז הכפתור שולח הסבר קצר
    והתמונה הבאה מסווגת ממילא — ראו handle_skin_type_photo, שכבר עשה
    את זה מאז 2026-08-26, פשוט בלי שאף אחד ידע שזה קיים.
    """
    buttons = [
        {
            "text": i18n.skin_type_label(n, lang),
            "callback_data": f"{SKIN_TYPE_CALLBACK_PREFIX}{n}",
        }
        for n in range(1, 7)
    ]
    rows = [buttons[i:i + 2] for i in range(0, 6, 2)]
    rows.append([{
        "text": t("skin_photo_button", lang),
        "callback_data": f"{SKIN_TYPE_CALLBACK_PREFIX}{SKIN_CALLBACK_PHOTO}",
    }])
    return {"inline_keyboard": rows}


def _skin_type_confirm_keyboard(suggested: int, lang: str = i18n.DEFAULT_LANGUAGE) -> dict:
    """
    אחרי הצעה מתמונה: אישור בלחיצה, או חזרה לבורר המלא.
    מחליף את "לאישור שלחו /set_skin_type 3" שהיה כאן קודם — הקלדת
    פקודה בדיוק בנקודה שבה המשתמש כבר הודה שהוא לא בטוח.
    """
    return {
        "inline_keyboard": [
            [{
                "text": t("photo_confirm_button", lang, label=i18n.skin_type_label(suggested, lang)),
                "callback_data": f"{SKIN_TYPE_CALLBACK_PREFIX}{suggested}",
            }],
            [{
                "text": t("photo_choose_other_button", lang),
                "callback_data": f"{SKIN_TYPE_CALLBACK_PREFIX}{SKIN_CALLBACK_AGAIN}",
            }],
        ]
    }


def handle_start(chat_id: int, username: str, args: str, lang: str = i18n.DEFAULT_LANGUAGE) -> None:
    """
    הודעת פתיחה. מ-2026-09-12 מקוצרת לשתי הודעות בלבד (ברכה + תמונה עם
    כפתורים) במקום שלוש: ההודעה השלישית פירטה את כל הפקודות עוד לפני
    שהמשתמש בחר סוג עור, כלומר ביקשה ממנו לזכור ארבע פקודות בזמן שהוא
    עדיין לא עשה כלום. במקומה, אישור בחירת סוג העור מציע את הצעד הבא
    היחיד הרלוונטי — וגם הוא כפתור (ראו _save_skin_type).

    מ-2026-09-14 דו-לשוני (ראו i18n.py): lang מגיע מ-language_code של
    ההודעה הנכנסת דרך ה-dispatch ב-handle_update.
    """
    send_message(chat_id, t("welcome", lang))

    keyboard = _skin_type_keyboard(lang)
    image_bytes = _load_fitzpatrick_scale_image()

    if image_bytes is not None:
        send_photo(chat_id, image_bytes, caption=t("skin_scale_caption", lang), reply_markup=keyboard)
    else:
        # בלי התמונה הכפתורים עדיין עומדים בפני עצמם — התוויות מילוליות.
        send_message(chat_id, t("skin_question", lang), reply_markup=keyboard)

    # גיבוי למי שמקליד בכל זאת ספרה בודדת (לקוח ישן, או הרגל) — עולה
    # כלום ומציל את המקרה. ראו _pending_skin_type_pick והניתוב ב-handle_update.
    _mark_pending_skin_type_pick(username)
    logger.info("Sent welcome message + skin-type picker to @%s", username)


def handle_dashboard(chat_id: int, username: str) -> None:
    link = create_magic_link(username)
    send_message(chat_id, f"האזור האישי שלך (בתוקף ל-24 שעות):\n{link}")
    logger.info("Sent dashboard link to @%s", username)


# ---------------------------------------------------------------------
# /offline_session — פותח את ה-Mini App לתיעוד session בלי קליטה
# (docs/session/index.html). כפתור web_app, לא קישור רגיל: מריץ את
# הדף בתוך ה-WebView של טלגרם, מה שנותן לו את initData לזיהוי המשתמש
# (ראו docs/2026-08-29-offline-session-miniapp-design.md).
# ---------------------------------------------------------------------
def handle_offline_session(chat_id: int, username: str, args: str) -> None:
    # web_app buttons חייבים HTTPS — טלגרם דוחה כל URL אחר עם 400 Bad
    # Request על ה-sendMessage עצמו (לפני שההודעה בכלל נשלחת). בלי הבדיקה
    # הזו, אם SESSION_MINIAPP_URL לא הוגדר (עדיין על ברירת המחדל
    # http://localhost), הבקשה הייתה נכשלת עם exception לא מטופל וממש
    # שום דבר לא קורה אצל המשתמש — בדיוק המצב שקרה כאן.
    if not SESSION_MINIAPP_URL.startswith("https://"):
        send_message(
            chat_id,
            "התכונה הזו עוד לא מוגדרת אצל מפעיל הבוט (SESSION_MINIAPP_URL "
            "חסר/לא HTTPS). נסו שוב מאוחר יותר.",
        )
        logger.warning(
            "SESSION_MINIAPP_URL is not HTTPS (%r) — refusing to send web_app button to @%s",
            SESSION_MINIAPP_URL, username,
        )
        return

    send_message(
        chat_id,
        "תיעוד session בלי קליטה — פתחו את זה עכשיו, כשיש לכם אינטרנט, "
        "כדי שהעמוד יישמר במכשיר וימשיך לעבוד גם בלי חיבור:",
        reply_markup={
            "inline_keyboard": [[
                {"text": "☀️ פתיחת SunSafe אופליין", "web_app": {"url": SESSION_MINIAPP_URL}},
            ]],
        },
    )
    logger.info("Sent offline-session Mini App link to @%s", username)


# ---------------------------------------------------------------------
# /set_skin_type <1-6>
# ---------------------------------------------------------------------
def handle_set_skin_type(chat_id: int, username: str, args: str) -> None:
    args = args.strip()
    if not args.isdigit() or not (1 <= int(args) <= 6):
        send_message(chat_id, "שימוש: /set_skin_type <מספר 1 עד 6> (סולם Fitzpatrick).")
        # אחרי הודעת-שימוש, תגובת-המשך סבירה היא סתם ספרה בודדת ("3")
        # בלי "/set_skin_type " לפניה — ראו _pending_skin_type_pick.
        _mark_pending_skin_type_pick(username)
        return

    _save_skin_type(chat_id, username, int(args))


def _save_skin_type(
    chat_id: int, username: str, skin_type: int, lang: str = i18n.DEFAULT_LANGUAGE
) -> None:
    """
    שמירת סוג העור + אישור למשתמש. מופרד מ-handle_set_skin_type ב-
    2026-09-12 כדי שגם נתיב הפקודה וגם לחיצה על כפתור (ראו
    handle_callback_query) יעברו באותו קוד בדיוק.
    """
    upsert_row(
        "users",
        # chat_id נשמר יחד עם skin_type — זו נקודת ה-INSERT הראשונה
        # האפשרית של שורת users (skin_type הוא NOT NULL ב-DB), אז זה
        # המקום הכי מוקדם ששומרים בו chat_id למשתמש חדש. ראו
        # docs/2026-08-26-multi-user-broadcast-design.md.
        {"telegram_username": username, "skin_type": skin_type, "chat_id": chat_id},
        on_conflict="telegram_username",
    )
    logger.info("Set skin_type=%s for @%s", skin_type, username)

    # האישור נושא גם את הצעד הבא, וגם הוא בלחיצה — זה מה שהחליף את
    # רשימת הפקודות שהייתה בהודעה השלישית של /start.
    send_message(
        chat_id,
        t("skin_saved", lang, label=i18n.skin_type_label(skin_type, lang)),
        reply_markup={
            "keyboard": [[{
                "text": t("share_location_button", lang),
                "request_location": True,
            }]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        },
    )


# ---------------------------------------------------------------------
# /start_session <עיר> — וגם שיתוף מיקום ישיר (ראו handle_start_session_location)
# ---------------------------------------------------------------------
def _can_start_session(chat_id: int, username: str, lang: str = i18n.DEFAULT_LANGUAGE) -> bool:
    """
    הבדיקות המשותפות לשני נתיבי ההתחלה (הקלדת עיר / שיתוף מיקום): יש
    סוג עור מוגדר, ואין session פתוח כבר. שולחת הודעת שגיאה בעברית
    ומחזירה False אם אחת הבדיקות נכשלה — כדי שלא נבקש מהמשתמש לשתף
    מיקום רק כדי לדחות אותו מיד אחר כך.
    """
    users = select_rows("users", {"telegram_username": f"eq.{username}"})
    if not users:
        # כפתורים ולא "/set_skin_type <1-6>": זה המחסום הראשון של כל
        # משתמש שלא עבר onboarding, ואין סיבה לדרוש ממנו להקליד כאן.
        send_message(
            chat_id,
            t("need_skin_type_first", lang),
            reply_markup=_skin_type_keyboard(lang),
        )
        return False

    open_sessions = select_rows(
        "exposure_log",
        {"telegram_username": f"eq.{username}", "end_time": "is.null"},
    )
    if open_sessions:
        send_message(
            chat_id,
            "כבר יש לך session פתוח — צריך לסגור אותו קודם:\n"
            "/end_session",
        )
        return False

    return True


# ---------------------------------------------------------------------
# תרשים תחזית UV להמשך היום — נשלח כתוספת best-effort אחרי הודעת האישור
# הטקסטואלית ב-_begin_session. שלושה שלבים נפרדים (fetch/render/send)
# כדי שכל שלב יהיה קל לבדוק/להחליף בנפרד; send_uv_forecast_chart היא
# העטיפה היחידה שבפועל נקראת מבחוץ, וזו שאחראית לכשל-בלי-לקרוס.
# ---------------------------------------------------------------------
def fetch_uv_forecast_next_24h(
    client: httpx.Client, lat: float, lon: float, from_time: datetime
) -> tuple[list[str], list[float]]:
    """
    שולף תחזית UV שעתית ל-24 השעות הבאות *בזמן המקומי של המיקום עצמו*,
    החל מהשעה שבה נפתח ה-session (from_time — datetime עם tzinfo, לרוב
    UTC; לא "עכשיו" כללי בזמן קריאת הפונקציה, אלא הרגע שנשמר בפועל
    ב-exposure_log ב-_begin_session). forecast_days=2 מבטיח מספיק שעות
    גם כש-from_time קרוב לחצות המקומית (חלון 24 שעות עלול לחצות יום
    יומן מקומי אחד). timezone=auto -> hourly.time כבר בזמן המקומי של
    המיקום, לא UTC (תוקן אחרי שהתחזית לניו יורק הוצגה לפי שעון UTC
    ולא לפי השעון המקומי שם); utc_offset_seconds שחוזר בתשובה ממיר את
    from_time לזמן המקומי המתאים באותו מיקום.
    """
    response = client.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "uv_index",
            "forecast_days": 2,
            "timezone": "auto",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    body = response.json()
    hourly = body["hourly"]
    times: list[str] = hourly["time"]
    uvs: list[float] = hourly["uv_index"]
    utc_offset_seconds = body.get("utc_offset_seconds", 0)

    local_start_hour = (from_time + timedelta(seconds=utc_offset_seconds)).replace(
        minute=0, second=0, microsecond=0, tzinfo=None
    )

    window = [(t, uv) for t, uv in zip(times, uvs) if datetime.fromisoformat(t) >= local_start_hour][:24]
    if not window:
        return [], []
    window_times, window_uvs = zip(*window)
    return list(window_times), list(window_uvs)


# fonts/ — Noto Sans (+ Noto Sans Thai) מצורפים ל-repo (לא מסתמכים על
# מה שמותקן במערכת ההפעלה של ה-deploy — ב-HF Space אין ערובה לאילו
# פונטים קיימים חוץ מ-DejaVu Sans, ברירת המחדל של matplotlib). הבעיה
# האמיתית שגילינו: DejaVu Sans מכסה עברית בסדר, אבל *לא* תאית — session
# שנפתח בעיר בתאילנד (למשל מהמנחה) ייצר כותרת גרף עם ריבועים ריקים
# במקום שם העיר (UserWarning: Glyph ... missing from font(s) DejaVu
# Sans, נצפה בפועל 2026-09-10). תיקון ממוקד, לא כיסוי-כל-שפה מלא: Noto
# Sans (לטינית/קירילית/יוונית/עברית/עוד) + Noto Sans Thai במפורש, שני
# הפונטים שבפועל נדרשו כדי לתקן את המקרה שנצפה. שפות נוספות (סינית,
# ערבית וכו') ידרשו קובץ Noto נוסף אם/כשיתגלה צורך אמיתי — לא מוסיפים
# מראש בלי עדות לצורך, בהתאם לבקשת המנחה לא "לנפח" תכונות.
_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_unicode_fonts_registered = False


def _ensure_unicode_fonts() -> None:
    """
    רושם את הפונטים המצורפים אצל matplotlib.font_manager ומגדיר
    rcParams['font.family'] לרשימת fallback (DejaVu Sans קודם — הכי
    חד וברור לעברית/אנגלית/מספרים; Noto Sans/Noto Sans Thai כגיבוי
    לתווים ש-DejaVu לא מכיל). קוראים לזה בתוך כל פונקציית רינדור גרף
    (אחרי ה-import המקומי של matplotlib), לא ברמת המודול — עקבי עם
    ההחלטה הקיימת לא לייבא matplotlib בראש הקובץ. אידמפוטנטי (safe
    לקרוא בכל רינדור גרף בלי לרשום כפול).
    """
    global _unicode_fonts_registered
    if _unicode_fonts_registered:
        return
    import matplotlib.font_manager as fm
    import matplotlib.pyplot as plt

    for filename in ("NotoSans-Regular.ttf", "NotoSansThai-Regular.ttf"):
        font_path = os.path.join(_FONTS_DIR, filename)
        try:
            fm.fontManager.addfont(font_path)
        except Exception as e:
            # לא קריטי: נופלים חזרה ל-DejaVu Sans בלבד (המצב הקודם) —
            # עדיף גרף עם ריבועים חסרים מקריסה של כל תכונת הגרפים.
            logger.warning("Failed to register font %s: %s", font_path, e)

    plt.rcParams["font.family"] = ["DejaVu Sans", "Noto Sans", "Noto Sans Thai"]
    _unicode_fonts_registered = True


def _build_uv_risk_cmap(y_max: float):
    """
    Colormap רציף (ירוק->צהוב->כתום->אדום) שממופה על טווח [0, y_max],
    עם עוגנים באותם ספים בדיוק כמו WHO/פלטת הסטטוס של SunSafe (good/
    warning/serious/critical ב-3/6/8). "רציף" בכוונה — לא 4 פסים
    שטוחים: ראו ה-docstring של render_uv_forecast_chart להסבר למה.
    """
    from matplotlib.colors import LinearSegmentedColormap

    stops_raw = [
        (0, STATUS_COLORS["good"]),
        (3, STATUS_COLORS["warning"]),
        (6, STATUS_COLORS["serious"]),
        (8, STATUS_COLORS["critical"]),
    ]
    positions: list[float] = []
    colors: list[str] = []
    last_pos = -1.0
    for value, color in stops_raw:
        pos = min(value / y_max, 1.0)
        if pos <= last_pos:  # y_max קטן מדי כדי להכיל את כל הספים (למשל UV אפסי) -> מדלגים על כפילויות
            continue
        positions.append(pos)
        colors.append(color)
        last_pos = pos
    if positions[0] > 0:
        positions.insert(0, 0.0)
        colors.insert(0, colors[0])
    if positions[-1] < 1.0:
        positions.append(1.0)
        colors.append(colors[-1])
    return LinearSegmentedColormap.from_list("uv_risk", list(zip(positions, colors)))


STATUS_COLORS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}


def render_uv_forecast_chart(hourly_times: list[str], hourly_uv: list[float], city_name: str) -> bytes:
    """
    מרנדר תרשים PNG (matplotlib, in-memory — io.BytesIO, בלי כתיבה לדיסק)
    של תחזית UV ל-24 השעות הבאות: שטח אחד רציף מתחת לקו, עם גרדיאנט
    צבע רציף (ירוק->צהוב->כתום->אדום) שמשקף את רמת ה-UV באותה שעה —
    לא 4 פסים שטוחים/חתוכים.

    זו הגרסה הרביעית אחרי סבב משוב מהמשתמש (כולם על אותו ציר-זמן,
    שלא השתנה מהתחלה): 1) עמודות צבעוניות -> נדחה ("אני לא אוהב את
    העמודות... עדיף פיתרון ויזואלי אחר"); 2) קו + שטח חתוך ל-4 פסי
    WHO -> אושר על הדוגמה ששלחתי ("אני מעוניין בכזה"); 3) המשתמש ביקש
    "שכל השטח מתחת יהיה זהה" -> הוחלף ל-צבע אחיד שטוח לכל השטח; 4)
    המשתמש ביקש שעדיין "יהיה רלוונטי לשעה" / "שהצבע... יהיה רלוונטי
    לשעה" -> צבע שטוח אחד לא הספיק (לא נושא מידע על השעה) אבל גם לא
    לחזור ל-4 פסים חתוכים ("זהה" מרמז על שטח אחד רציף) — הפתרון: שטח
    רציף אחד (לא מחולק ל-blocks) שהצבע *בתוכו* זורם באופן חלק לפי
    ערך ה-UV של אותה שעה. ממומש ע"י imshow עם גרדיאנט אופקי (עמודה
    לכל שעה, צבועה לפי hourly_uv[i] דרך _build_uv_risk_cmap), עם
    interpolation="bilinear" לבלנד חלק בין שעות סמוכות, clipped
    לפוליגון "מתחת לעקומה" (מ-fill_between עם color="none", רק בשביל
    ה-Path שלו) — כך שהצבע נשאר בתחום שמתחת לקו, לא מלבן מלא.

    הכותרת כוללת את שם העיר (city_name), מוכנס כמו שהוא ל-title בלי
    שום עיבוד bidi. היסטוריה שכדאי לתעד כאן כי היא לא אינטואיטיבית:
    ניסינו לעטוף עם get_display() של python-bidi (ה"תיקון" המקובל
    ל-Hebrew-in-matplotlib), המשתמש חשד שזה הפוך, בדקנו עם השוואת A/B
    מפורשת (עם get_display מול בלי) ובהתחלה אישר את הגרסה עם
    get_display כנכונה — אבל כשזה נבדק מול מה שבאמת מוצג בבוט החי,
    המשתמש קבע במפורש: "אני מעוניין בפיתרון של שורה עליונה" — כלומר
    הגרסה *בלי* עיבוד bidi (Option A) היא הנכונה בפועל אצלו, למרות
    שזה סותר את התיעוד הכללי על matplotlib+RTL. לא לשנות את זה שוב
    בלי אימות ישיר מול תוצאה אמיתית מהבוט החי (לא רק תמונת-השוואה).

    import מקומי (לא בראש הקובץ) בכוונה: אם matplotlib חסר בסביבת
    ה-deploy, רק הפיצ'ר הזה נכשל (ונתפס ב-send_uv_forecast_chart) —
    שאר הבוט (כולל /start_session עצמו) ממשיך לעבוד כרגיל.
    """
    import matplotlib
    matplotlib.use("Agg")  # רינדור ל-buffer בלבד, בלי חלון/תצוגה — נדרש בסביבת שרת
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import Normalize
    _ensure_unicode_fonts()

    hours = [datetime.fromisoformat(t).strftime("%H:%M") for t in hourly_times]
    x = list(range(len(hours)))
    # תקרה ל-y: קצת מעל השיא, אבל לפחות 3 (כדי שהגרף לא יהיה שטוח
    # לגמרי גם בימים עם UV אפסי, למשל session שנפתח בלילה).
    y_max = max(max(hourly_uv, default=0) * 1.15, 3)

    fig, ax = plt.subplots(figsize=(11, 4.5), dpi=150)

    # פוליגון "מתחת לעקומה" — בלי צבע משלו (color="none"), רק כדי
    # לקחת ממנו את ה-Path ולהשתמש בו כ-clip mask לגרדיאנט למטה.
    invisible_fill = ax.fill_between(x, 0, hourly_uv, color="none")
    area_under_curve = invisible_fill.get_paths()[0]

    # גרדיאנט אופקי — עמודה אחת לכל שעה, צבועה לפי ה-UV של אותה שעה,
    # עם בלנד חלק בין שעות (bilinear). clipped לשטח שמתחת לקו בלבד.
    if x:
        cmap = _build_uv_risk_cmap(y_max)
        gradient = np.array(hourly_uv, dtype=float).reshape(1, -1)
        image = ax.imshow(
            gradient, extent=[x[0] - 0.5, x[-1] + 0.5, 0, y_max], origin="lower",
            aspect="auto", cmap=cmap, norm=Normalize(vmin=0, vmax=y_max),
            interpolation="bilinear", alpha=0.6, zorder=2,
        )
        image.set_clip_path(area_under_curve, transform=ax.transData)

    # הקו עצמו — צבע ניטרלי כהה, שיבלוט מעל הגרדיאנט; marker בכל שעה
    # כדי ש-24 נקודות הנתונים יישארו קריאות.
    ax.plot(
        x, hourly_uv, color="#2b2b2b", linewidth=2,
        marker="o", markersize=4, markerfacecolor="#2b2b2b", zorder=3,
    )

    ax.set_title(f"UV Forecast — {city_name} — Next 24 Hours", fontsize=13, pad=12)
    ax.set_ylabel("UV Index")
    ax.set_ylim(0, y_max)
    if x:
        ax.set_xlim(-0.5, len(x) - 0.5)
    ax.set_xticks(x)
    ax.set_xticklabels(hours, rotation=60, ha="right", fontsize=7)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.8, zorder=1)
    ax.set_axisbelow(True)

    # תווית ישירה רק על שיא ה-UV (הערך הכי שימושי, לא על כל 24 השעות —
    # זה היה עמוס מדי לקריאה). ראו dataviz skill: "selective direct labels".
    if hourly_uv:
        peak_idx = max(range(len(hourly_uv)), key=lambda i: hourly_uv[i])
        ax.annotate(
            f"peak {hourly_uv[peak_idx]:.1f}",
            (peak_idx, hourly_uv[peak_idx]),
            textcoords="offset points", xytext=(0, 8),
            ha="center", fontsize=8, color="#333333", fontweight="bold", zorder=4,
        )

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def send_uv_forecast_chart(chat_id: int, city_name: str, lat: float, lon: float, session_start: datetime) -> None:
    """
    שולף+מרנדר+שולח את תרשים תחזית ה-UV ל-24 השעות הבאות, החל משעת
    פתיחת ה-session (session_start — לא "עכשיו" בזמן קריאת הפונקציה,
    ראו fetch_uv_forecast_next_24h) — best-effort בכוונה: כל הפונקציה
    עטופה ב-try/except רחב. session כבר נכתב ל-DB ואושר למשתמש בטקסט
    לפני שהפונקציה הזו נקראת (ראו _begin_session) — כשל כאן (רשת, חבילה
    חסרה, תגובה לא צפויה מ-Open-Meteo) לא אמור לעולם להיראות למשתמש
    כתקלה ב-/start_session עצמו, רק להירשם ללוג.
    """
    try:
        with httpx.Client() as client:
            hourly_times, hourly_uv = fetch_uv_forecast_next_24h(client, lat, lon, session_start)
        if not hourly_uv:
            logger.info("send_uv_forecast_chart: no forecast hours available for %s, skipping", city_name)
            return
        chart_png = render_uv_forecast_chart(hourly_times, hourly_uv, city_name)
        send_photo(chat_id, chart_png, caption=f"📊 תחזית UV ל-24 השעות הבאות ב{city_name}")
        logger.info("Sent UV forecast chart to chat_id=%s for %s (%d hours)", chat_id, city_name, len(hourly_uv))
    except Exception:
        logger.exception("send_uv_forecast_chart failed for chat_id=%s city=%s", chat_id, city_name)


# ---------------------------------------------------------------------
# גרף UV יומי ל-/today — עקומת UV מלאה ליום קלנדרי (00:00-23:00 UTC)
# עם חלונות ה-sessions של אותו יום מסומנים עליה. הוחלט עם המשתמש
# ב-2026-09-08 (במקום למשל בר-גרף actual-מול-SPF30): "עקומת UV של היום
# + חלונות ה-sessions מסומנים עליה", נשלח אוטומטית יחד עם הטקסט של
# /today (לא כפקודה נפרדת). ראו handle_today / send_daily_exposure_chart.
# ---------------------------------------------------------------------
def fetch_day_uv_curve(client: httpx.Client, lat: float, lon: float, target_date) -> tuple[list[str], list[float]]:
    """
    שולף את עקומת ה-UV השעתית המלאה (00:00–23:00 UTC) של יום קלנדרי שלם
    (target_date). timezone=UTC בכוונה (לא auto כמו fetch_uv_forecast_next_24h)
    כדי שהאינדקס i יתאים ישירות לשעה i ב-UTC — אותו יום קלנדרי בדיוק
    ש-_sessions_on_date בודקת מולו (start_time.date() ב-UTC), כדי
    שחלונות ה-sessions ב-render_daily_exposure_chart ייושרו נכון מול
    העקומה. past_days/forecast_days נגזרים מההפרש בין today ל-target_date;
    מחוץ לטווח הנתמך של Open-Meteo (עבר רחוק/עתיד רחוק) מחזירים ([], [])
    ומשאירים לקורא (send_daily_exposure_chart) לדלג על הגרף בלי לקרוס.
    """
    today = datetime.now(timezone.utc).date()
    days_diff = (today - target_date).days
    if days_diff > 92 or days_diff < -14:
        return [], []
    if days_diff >= 0:
        past_days, forecast_days = days_diff, 1
    else:
        past_days, forecast_days = 0, min(-days_diff + 1, 16)

    response = client.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "uv_index",
            "past_days": past_days,
            "forecast_days": forecast_days,
            "timezone": "UTC",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    hourly = response.json()["hourly"]
    times: list[str] = hourly["time"]
    uvs: list[float] = hourly["uv_index"]

    day_prefix = target_date.isoformat()
    day_hours = [(t, uv) for t, uv in zip(times, uvs) if t.startswith(day_prefix)]
    if not day_hours:
        return [], []
    day_times, day_uvs = zip(*day_hours)
    return list(day_times), list(day_uvs)


def render_daily_exposure_chart(
    hourly_times: list[str], hourly_uv: list[float], sessions: list[dict], city_name: str, target_date,
    utc_offset_seconds: int = 0,
) -> bytes:
    """
    מרנדר PNG (matplotlib, in-memory) של עקומת ה-UV ליום שלם (target_date)
    עם חלונות ה-sessions של אותו יום מסומנים עליה כרצועות אנכיות —
    התוספת הגרפית ל-/today (ראו handle_today/send_daily_exposure_chart).

    עותק עצמאי בכוונה מ-render_uv_forecast_chart, לא חולק איתה קוד
    (מלבד _build_uv_risk_cmap המשותף): לזו יש היסטוריית משוב מפורטת
    משלה (ראו ה-docstring שלה) ולא רוצים ששינוי כאן ישפיע על גרסה
    שכבר עובדת ואושרה. מאותה סיבה: הכותרת/legend/annotations כאן
    בטקסט אנגלי בלבד (רק שם-העיר עצמו, שם פרטי, יכול להיות בעברית,
    בדיוק כמו ברכיב הקיים) — נמנעים במכוון מלהכניס בלוק טקסט עברי חדש
    ל-matplotlib בלי אימות ישיר מול תוצאה אמיתית מהבוט החי (ראו את
    הדיון על get_display()/bidi ברכיב הקיים).

    session פתוח (end_time=None) מוצג עד "עכשיו" בפועל, לא עד סוף היום —
    הגרף תמיד משקף חשיפה שכבר קרתה, לא ניחוש לעתיד.

    utc_offset_seconds (נוסף ב-2026-09-09, ראו fetch_utc_offset_seconds):
    hourly_times מגיע מ-fetch_day_uv_curve ב-UTC ("בכוונה", ראו שם) —
    בלי ההזחה הזו, תוויות ציר ה-X הוצגו כשעון UTC גולמי בלי שום סימון,
    ומשתמש שראה "02:00" חשב שזה השעון המקומי שלו (תקלה אמיתית: משתמש
    בקריית ים, UTC+3, ראה את "עכשיו" מסומן ב-02:00-03:00 בזמן שהשעון
    אצלו הראה 5:00). כאן מזיזים רק את *התוויות המוצגות* לפי הזמן המקומי
    של reference — המיקום המספרי של כל רצועה/סימון (למטה) נשאר מחושב
    לפי UTC פנימית, עקבי עם _sessions_on_date/fetch_day_uv_curve, כך
    שאף רצועה לא זזה בפועל, רק הכיתוב שמעליה.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import Normalize
    _ensure_unicode_fonts()

    hours = [
        (datetime.fromisoformat(t) + timedelta(seconds=utc_offset_seconds)).strftime("%H:%M")
        for t in hourly_times
    ]
    x = list(range(len(hours)))
    y_max = max(max(hourly_uv, default=0) * 1.15, 3)

    fig, ax = plt.subplots(figsize=(11, 4.5), dpi=150)

    invisible_fill = ax.fill_between(x, 0, hourly_uv, color="none")
    area_under_curve = invisible_fill.get_paths()[0]

    if x:
        cmap = _build_uv_risk_cmap(y_max)
        gradient = np.array(hourly_uv, dtype=float).reshape(1, -1)
        image = ax.imshow(
            gradient, extent=[x[0] - 0.5, x[-1] + 0.5, 0, y_max], origin="lower",
            aspect="auto", cmap=cmap, norm=Normalize(vmin=0, vmax=y_max),
            interpolation="bilinear", alpha=0.6, zorder=2,
        )
        image.set_clip_path(area_under_curve, transform=ax.transData)

    ax.plot(
        x, hourly_uv, color="#2b2b2b", linewidth=2,
        marker="o", markersize=4, markerfacecolor="#2b2b2b", zorder=3,
    )

    # רצועות ה-sessions: קידוד חזותי נפרד מהגרדיאנט (hatch + קו-מתאר, לא
    # רק שקיפות-צבע) כדי שיישאר קריא גם ב-colorblind/הדפסה — ראו dataviz
    # skill. session פתוח מוצג עד "עכשיו" בפועל. ה-session עם מדד החשיפה
    # הגבוה ביותר (_peak_exposure_session, ראו שם) מודגש בצבע חם נפרד
    # ומתויג עם שם-העיר — "המיקום איפה שמד החשיפה היה הגבוה ביותר",
    # הוחלט עם המשתמש ב-2026-09-08.
    midnight = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    x_min, x_max = (x[0] - 0.5, x[-1] + 0.5) if x else (0, 24)
    peak_session = _peak_exposure_session(sessions)
    has_session_band = False
    has_peak_band = False
    for session in sessions:
        if not session.get("start_time"):
            continue
        start_dt = datetime.fromisoformat(session["start_time"])
        end_dt = datetime.fromisoformat(session["end_time"]) if session.get("end_time") else now
        x_start = max((start_dt - midnight).total_seconds() / 3600, x_min)
        x_end = min((end_dt - midnight).total_seconds() / 3600, x_max)
        if x_end <= x_start:
            continue
        is_peak = peak_session is not None and session is peak_session
        color = "#c0392b" if is_peak else "#2b6cb0"
        if is_peak:
            band_label = None if has_peak_band else "highest exposure"
            has_peak_band = True
        else:
            band_label = None if has_session_band else "session"
            has_session_band = True
        ax.axvspan(
            x_start, x_end, facecolor=color, alpha=0.22 if is_peak else 0.16, hatch="//",
            edgecolor=color, linewidth=1.4 if is_peak else 1.0,
            zorder=2.6 if is_peak else 2.5, label=band_label,
        )
        if is_peak:
            label = f"{session.get('city', '')} {session['exposure_score']}%"
        else:
            label = f"{session['exposure_score']}%" if session.get("exposure_score") is not None else "open"
        ax.annotate(
            label, ((x_start + x_end) / 2, y_max * 0.95), ha="center", va="top",
            fontsize=8, color=color, fontweight="bold", zorder=4,
        )

    ax.set_title(f"Daily UV Exposure — {city_name} — {target_date.strftime('%d.%m.%Y')}", fontsize=13, pad=12)
    ax.set_ylabel("UV Index")
    # מציינים "local time" במפורש על הציר עצמו — לא רק שהשעות מוזחות
    # (utc_offset_seconds למעלה), אלא כדי שלא ייראה כמו UTC סתום שוב
    # בעתיד אם ה-offset הזה ייכשל-בשקט ויחזור ל-0 (fetch_utc_offset_seconds
    # נכשל best-effort).
    ax.set_xlabel("Hour (local time)", fontsize=9, color="#555555")
    ax.set_ylim(0, y_max)
    if x:
        ax.set_xlim(-0.5, len(x) - 0.5)
    ax.set_xticks(x)
    ax.set_xticklabels(hours, rotation=60, ha="right", fontsize=7)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e5e5e5", linewidth=0.8, zorder=1)
    ax.set_axisbelow(True)

    if hourly_uv:
        peak_idx = max(range(len(hourly_uv)), key=lambda i: hourly_uv[i])
        ax.annotate(
            f"peak {hourly_uv[peak_idx]:.1f}",
            (peak_idx, hourly_uv[peak_idx]),
            textcoords="offset points", xytext=(0, 8),
            ha="center", fontsize=8, color="#333333", fontweight="bold", zorder=4,
        )

    if has_session_band or has_peak_band:
        ax.legend(loc="upper left", fontsize=8, framealpha=0.9)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def _notify_admin_chart_skip(target_date, reason: str) -> None:
    """
    best-effort בלבד: מדווחת ל-ADMIN_CHAT_ID (אם מוגדר, ראו ADMIN_CHAT_ID
    למעלה) מתי ולמה גרף /today לא נשלח בפועל. נוספה ב-2026-09-08 אחרי
    שהתברר שהמשתמש הריץ /today וקיבל טקסט תקין אבל בלי תמונה בכלל —
    send_daily_exposure_chart בולעת כל כשל בכוונה (כדי שכשל בגרף לעולם
    לא ישבור את הטקסט של /today עצמו), אבל זה הפך את הכשל ל"שקט" לגמרי
    ובלתי-ניתן-לאבחון בלי גישה ללוגים של ה-Space. לא זורקת ולא מעכבת
    את /today עצמו במקרה של כשל בשליחה גם כאן.
    """
    if not ADMIN_CHAT_ID:
        return
    try:
        send_message(ADMIN_CHAT_ID, f"⚠️ גרף /today לא נשלח (תאריך {target_date}): {reason[:500]}")
    except Exception as e:
        # תוקן ב-2026-09-08 אחרי הפעלה ראשונה בפרודקשן: השליחה עצמה נכשלה
        # (WARNING בלוג) אבל בלי סיבה — כי ה-except הישן בלע את e ולא הדפיס
        # אותו, כמו _notify_admin_token_usage למעלה שכן עושה זאת. עכשיו
        # מדפיסים את e בפועל כדי שכשל הבא יהיה ניתן-לאבחון בלי ניחושים.
        logger.warning("Failed to send admin chart-skip notification for target_date=%s: %s", target_date, e)


def send_daily_exposure_chart(chat_id: int, todays_sessions: list[dict], target_date) -> None:
    """
    שולח (best-effort) את גרף ה-UV היומי עם חלונות ה-sessions מסומנים —
    תוספת גרפית ל-/today (ראו handle_today). עוטף הכל ב-try/except רחב:
    הטקסט של /today כבר נשלח למשתמש לפני שהפונקציה הזו נקראת (אותו
    דפוס בדיוק כמו send_uv_forecast_chart) — כשל כאן (רשת, matplotlib
    חסר, אין session עם lat/lon) לעולם לא אמור להיראות למשתמש כתקלה
    ב-/today עצמו. כל דילוג/כשל גם מדווח ל-_notify_admin_chart_skip
    (best-effort נוסף, ADMIN_CHAT_ID בלבד) — כדי שהדילוג לא יהיה שקט
    לגמרי (ראו שם לרציונל).

    בוחרים את מיקום-הייחוס לעקומת ה-UV לפי ה-session המוקדם ביותר היום
    שיש לו lat/lon שמור (sessions ישנים/לפני migration ה-lat/lon עשויים
    להיות בלי lat/lon בכלל — אז מדלגים על הגרף כליל, לא מנחשים מיקום).
    """
    try:
        reference = min(
            (s for s in todays_sessions if s.get("lat") is not None and s.get("lon") is not None),
            key=lambda s: s["start_time"],
            default=None,
        )
        if reference is None:
            logger.info("send_daily_exposure_chart: no session today has lat/lon yet, skipping chart")
            _notify_admin_chart_skip(target_date, "אף session באותו יום לא כולל lat/lon שמור")
            return

        with httpx.Client() as client:
            hourly_times, hourly_uv = fetch_day_uv_curve(client, reference["lat"], reference["lon"], target_date)
            if not hourly_uv:
                logger.info("send_daily_exposure_chart: no UV curve available for %s, skipping", target_date)
                _notify_admin_chart_skip(target_date, "fetch_day_uv_curve לא החזירה נתונים (טווח לא נתמך/תשובה ריקה)")
                return
            # תוקן ב-2026-09-09: ציר-השעות של הגרף הוצג לפי UTC גולמי בלי
            # שום סימון (ראו render_daily_exposure_chart) — תקלה אמיתית
            # שדווחה: משתמש בקריית ים (UTC+3) ראה את "עכשיו" מסומן סביב
            # 02:00-03:00 בזמן שהשעון אצלו הראה 5:00, ונראה כמו תקלה.
            # אותו מנגנון fetch_utc_offset_seconds בדיוק כמו ב-/add_session
            # ו-/edit_session (ראו שם) — best-effort, נופל חזרה ל-0 (UTC
            # כפי שהיה) אם הקריאה נכשלת.
            utc_offset_seconds = fetch_utc_offset_seconds(client, reference["lat"], reference["lon"])

        chart_png = render_daily_exposure_chart(
            hourly_times, hourly_uv, todays_sessions, reference["city"], target_date,
            utc_offset_seconds=utc_offset_seconds,
        )
        caption = f"📊 עקומת UV ל-{target_date.strftime('%d.%m.%Y')} עם ה-sessions שלך מסומנים עליה"
        peak = _peak_exposure_session(todays_sessions)
        if peak is not None:
            caption += f"\nהחשיפה הגבוהה ביותר: {peak['city']} ({peak['exposure_score']}%)"
        send_photo(chat_id, chart_png, caption=caption)
        logger.info("Sent daily exposure chart to chat_id=%s for %s", chat_id, target_date)
    except Exception as e:
        logger.exception("send_daily_exposure_chart failed for chat_id=%s target_date=%s", chat_id, target_date)
        _notify_admin_chart_skip(target_date, f"חריגה: {e}")


def _begin_session(
    chat_id: int,
    username: str,
    city_name: str,
    country: str | None,
    uv_index: float,
    lat: float,
    lon: float,
    clear_keyboard: bool = False,
) -> None:
    """כתיבת exposure_log + הודעת אישור — משותף לנתיב הקלדת-עיר ונתיב-מיקום."""
    now = datetime.now(timezone.utc)
    insert_row(
        "exposure_log",
        {
            "telegram_username": username,
            "city": city_name,
            "country": country,
            "start_time": now.isoformat(),
            "end_time": None,
            "uv_index": uv_index,
            "lat": lat,
            "lon": lon,
            "spf": None,
            "exposure_score": None,
        },
    )
    # מציגים גם country בהודעת האישור — כדי שאם geocode_city פענח עיר לא
    # נכונה (למשל "סן חוזה" -> ארה"ב במקום קוסטה ריקה) המשתמש יבחין מיד
    # ולא רק כשה-UV/מזג האוויר לא הגיוני.
    location_label = f"{city_name}, {country}" if country else city_name

    # שורת "כמה זמן מותר לי" — שליפה אחת של סוג העור. best-effort
    # בכוונה: ה-session כבר נכתב, וכשל כאן לא אמור למנוע את האישור.
    exposure_line = None
    try:
        users = select_rows("users", {"telegram_username": f"eq.{username}"})
        if users:
            exposure_line = safe_exposure_line(uv_index, users[0].get("skin_type"))
    except Exception:
        logger.exception("Could not build the safe-exposure line for @%s", username)

    send_message(
        chat_id,
        f"התחלת session ב{location_label} ☀️\n"
        f"UV נוכחי: {uv_index:.1f}\n\n"
        + (f"{exposure_line}\n\n" if exposure_line else "")
        + "כשתסיימו, שלחו\n"
        "/end_session\n\n"
        "אם השתמשתם בקרם הגנה, הוסיפו את מספר ה-SPF:\n"
        "/end_session 50",
        reply_markup={"remove_keyboard": True} if clear_keyboard else None,
    )
    logger.info("Started session for @%s in %s (UV=%s)", username, city_name, uv_index)
    send_uv_forecast_chart(chat_id, city_name, lat, lon, now)


# מקבל "32.08,34.78", "32.08, 34.78" וגם "32.08 34.78" (רווח בין השניים
# במקום פסיק) — כל השלושה יוצאים מ-copy-paste של קואורדינטות ממפות שונות
# (Google Maps למשל מפריד בפסיק). שני מספרים עשרוניים בלבד, כלום מעבר
# לזה — כדי לא לתפוס בטעות שם עיר עם מספר בתוכו כקואורדינטה.
_COORDINATE_ARGS_RE = re.compile(r"^(-?\d+(?:\.\d+)?)[,\s]\s*(-?\d+(?:\.\d+)?)$")


def handle_start_session(chat_id: int, username: str, args: str, lang: str = i18n.DEFAULT_LANGUAGE) -> None:
    location_text = args.strip()
    if not _can_start_session(chat_id, username, lang):
        return

    if not location_text:
        # בלי ארגומנט — מציעים כפתור מיקום במקום רק להחזיר שגיאת שימוש.
        prompt_location_share(chat_id)
        return

    coords = _COORDINATE_ARGS_RE.match(location_text)
    if coords:
        lat, lon = float(coords.group(1)), float(coords.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            send_message(
                chat_id,
                f"קואורדינטות לא תקינות: {lat}, {lon}. "
                "טווח חוקי: קו רוחב (lat) בין 90- ל-90, קו אורך (lon) בין 180- ל-180.",
            )
            return
        with httpx.Client() as client:
            # reverse geocoding רק בשביל שם תצוגה בהודעת האישור/בגרף — אם
            # הוא נכשל (מיקום מרוחק בלי עיר קרובה, Nominatim לא זמין וכו')
            # ה-session עדיין נפתח: ה-UV נשלף ישירות מה-lat/lon המדויקים
            # בלי תלות בזיהוי שם, בדיוק כמו בנתיב שיתוף-המיקום.
            geo = reverse_geocode_location(client, lat, lon)
            city_name = geo["name"] if geo["found"] else f"מיקום {lat:.4f}, {lon:.4f}"
            country = geo.get("country") if geo["found"] else None
            uv_index = get_current_uv(client, lat, lon)
        _begin_session(chat_id, username, city_name, country, uv_index, lat, lon)
        return

    with httpx.Client() as client:
        geo = geocode_city(client, location_text)
        if not geo["found"]:
            send_message(
                chat_id,
                f'לא הצלחתי לזהות עיר בשם "{location_text}". בדקו את האיות, או שלחו '
                'קואורדינטות ישירות (למשל "32.08, 34.78") ונסו שוב.',
            )
            return
        uv_index = get_current_uv(client, geo["latitude"], geo["longitude"])

    _begin_session(chat_id, username, geo["name"], geo["country"], uv_index, geo["latitude"], geo["longitude"])


def handle_start_session_location(chat_id: int, username: str, lat: float, lon: float, lang: str = i18n.DEFAULT_LANGUAGE) -> None:
    """
    מטפל בהודעת location שמגיעה משיתוף מיקום (כפתור request_location) —
    ראו docs/2026-08-26-location-sharing-design.md. שימוש ב-lat/lon
    המדויקים מהטלפון (לא מרכז-עיר משוער) גם עבור קריאת ה-UV.
    """
    if not _can_start_session(chat_id, username, lang):
        return

    with httpx.Client() as client:
        geo = reverse_geocode_location(client, lat, lon)
        if not geo["found"]:
            send_message(
                chat_id,
                "לא הצלחתי לזהות עיר מהמיקום ששיתפתם. נסו /start_session <שם עיר> ידנית.",
            )
            return
        uv_index = get_current_uv(client, lat, lon)

    _begin_session(chat_id, username, geo["name"], geo["country"], uv_index, lat, lon, clear_keyboard=True)


# ---------------------------------------------------------------------
# /end_session [SPF]
# ---------------------------------------------------------------------
def handle_end_session(chat_id: int, username: str, args: str) -> None:
    args = args.strip()
    spf = None
    if args:
        if not args.isdigit():
            send_message(
                chat_id,
                "בלי קרם הגנה, שלחו\n"
                "/end_session\n\n"
                "— או עם קרם הגנה (מספר ה-SPF):\n"
                "/end_session 30",
            )
            return
        spf = int(args)

    open_sessions = select_rows(
        "exposure_log",
        {"telegram_username": f"eq.{username}", "end_time": "is.null"},
    )
    if not open_sessions:
        send_message(chat_id, "אין לך session פתוח כרגע. שלחו /start_session <עיר> כדי להתחיל אחד.")
        return

    session = open_sessions[0]
    users = select_rows("users", {"telegram_username": f"eq.{username}"})
    # 1 ולא 3 (שונה 16.9.2026): 3 הוא *אמצע* הסולם, לא ברירת מחדל
    # זהירה — ההערה שהייתה כאן קראה לו כך בטעות. סוג עור 1 נשרף
    # הכי מהר (factor 0.5), כלומר הוא נותן את תקציב הזמן הקצר ביותר.
    # באפליקציית בטיחות, כשלא יודעים מי המשתמש, מניחים את מי שנשרף
    # ראשון — שגיאה לכיוון "תמרח קרם" עדיפה על שגיאה לכיוון "אתה בסדר".
    skin_type = users[0]["skin_type"] if users else 1

    start_time = datetime.fromisoformat(session["start_time"])
    end_time = datetime.now(timezone.utc)
    duration_minutes = (end_time - start_time).total_seconds() / 60

    # ברירת מחדל: הדגימה הבודדת שנשמרה ב-_begin_session (התנהגות ישנה).
    # אם יש lat/lon שמורים (שורות אחרי migration ה-lat/lon) מנסים לרענן
    # לממוצע-משוקלל-משך על פני כל ה-session בפועל — ראו weighted_average_uv
    # לרציונל המלא (תיקון לתקלת מצפה רמון, 2026-09-08: session ארוך עם
    # דגימה בודדת ליד חצות הציג UV=0.0 במקום שיא אמיתי בצהריים). כשל
    # ברענון (רשת, lat/lon חסרים בשורות ישנות מלפני ה-migration, וכו')
    # נופל בחזרה בבטחה לדגימה המקורית — אף פעם לא מונע מ-/end_session
    # לסיים בהצלחה.
    uv_index = session["uv_index"]
    uv_is_average = False
    lat, lon = session.get("lat"), session.get("lon")
    if lat is not None and lon is not None:
        try:
            with httpx.Client() as client:
                refreshed_uv = fetch_historical_uv(client, lat, lon, start_time, end_time)
            if refreshed_uv is not None:
                uv_index = refreshed_uv
                uv_is_average = True
        except Exception:
            logger.exception(
                "handle_end_session: failed to refresh weighted-average UV for session id=%s — "
                "falling back to the original single-snapshot value",
                session["id"],
            )

    score = calculate_exposure_score(uv_index, duration_minutes, skin_type, spf)

    update_rows(
        "exposure_log",
        {"id": f"eq.{session['id']}"},
        {"end_time": end_time.isoformat(), "spf": spf, "exposure_score": score, "uv_index": uv_index},
    )

    # "55%" לבדו הוא אחוז מתקציב שהמשתמש לא ראה מעולם. מציגים לצדו את
    # התקציב עצמו, כך שהמספר מקבל משמעות: 45 מתוך 32 דקות מסביר את
    # ה-140% הרבה יותר טוב מהאחוז לבדו (2026-09-15).
    #
    # ומציגים גם את ה-UV עצמו, כי אחרת שתי ההודעות סותרות זו את זו
    # לכאורה: ב-/start_session הוצג UV רגעי (למשל 2.3 -> 65 דקות), וכאן
    # מוצג הממוצע המשוקלל על פני ה-session (1.5 -> 100 דקות). שני
    # המספרים נכונים ומודדים דברים שונים — נצפה בבדיקה אמיתית בלפקדה,
    # 2026-09-15, ובלי ה-UV לצדם זה נראה כמו באג.
    # שורת "מדד חשיפה: X% — Y מתוך Z הדקות המותרות" הוסרה ב-16.9.2026
    # לבקשת המשתמש. היא נוספה ב-15.9 כדי לתת לאחוז משמעות, אבל מאז
    # נוסף הסרגל היומי — וההודעה הגיעה לתשע שורות על session של אפס
    # דקות. התקציב עצמו עדיין מוצג ב-/start_session, שם הוא מגיע בזמן
    # שעוד אפשר לפעול לפיו, והפירוט לכל session נמצא ב-/dashboard.
    #
    # score עצמו ממשיך להיחשב ולהיכתב ל-exposure_log למעלה — רק
    # התצוגה שלו בהודעה הוסרה.

    # הסיכום היומי בהודעת הסיום (16.9.2026). /today כבר עשה בדיוק את
    # החישוב הזה — sum על ה-sessions הסגורים של אותו תאריך UTC — אבל
    # הוא פקודה שצריך לזכור לשלוח, ובלי שום צבע. מי שמסיים session
    # מקבל עכשיו את התמונה היומית בלי לבקש.
    #
    # אותם עוזרים בדיוק (_sessions_on_date, _peak_exposure_session)
    # ולא חישוב מקביל, כדי שההודעה הזו ו-/today לא יוכלו להיפרד.
    # ה-update_rows למעלה כבר רץ, אז ה-session שנסגר כרגע נכלל בשליפה.
    #
    # best-effort בכוונה: ה-session כבר נסגר ונכתב, וכשל בשליפה לא
    # אמור למנוע מהמשתמש את התוצאה של עצמו.
    daily_part = ""
    try:
        recent = select_rows(
            "exposure_log",
            {"telegram_username": f"eq.{username}", "order": "start_time.desc", "limit": "50"},
        )
        todays = _sessions_on_date(recent, end_time.date())
        closed_today = [s for s in todays if s["end_time"] and s["exposure_score"] is not None]
        if closed_today:
            day_score = daily_exposure_score(s["exposure_score"] for s in closed_today)
            total_minutes = sum(
                (datetime.fromisoformat(s["end_time"]) - datetime.fromisoformat(s["start_time"])).total_seconds() / 60
                for s in closed_today
            )
            peak = _peak_exposure_session(closed_today)
            daily_part = "\n" + daily_summary_he(
                day_score,
                len(closed_today),
                total_minutes,
                peak["city"] if peak else None,
                peak["exposure_score"] if peak else None,
            )
    except Exception:
        logger.exception("Could not build the daily summary for @%s", username)

    send_message(
        chat_id,
        f"{round(duration_minutes)} דקות ב{session['city']}."
        f"{daily_part}\n"
        "כדי לראות את הנתונים באזור האישי — לחצו\n"
        "/dashboard",
    )
    logger.info("Ended session id=%s for @%s: score=%s", session["id"], username, score)


# ---------------------------------------------------------------------
# /add_session — רישום ידני מלא של session שכבר הסתיים, בפקודת טקסט
# אחת (בשונה מ-/offline_session שפותח Mini App). UV Index נשלף אוטומטית
# מההיסטוריה של Open-Meteo לפי העיר והשעה שצוינו; uv=<מספר> הוא escape
# hatch ידני למקרה שההיסטוריה לא זמינה (Open-Meteo תומך עד 92 יום אחורה,
# ולפעמים אין נתון גם בטווח הזה).
# ---------------------------------------------------------------------
def _parse_kv_fields(tokens: list[str], allowed_keys: set[str]) -> tuple[list[str], dict]:
    """
    מפריד רשימת טוקנים לחלק "טקסט חופשי" (בהתחלה, למשל שם עיר עם רווחים)
    ואחריו זוגות key=value. עוצר בטוקן הראשון עם "=" שהמפתח שלו מוכר,
    ואוסף את כל ה-key=value מאותה נקודה והלאה. מחזיר (free_text_tokens, fields).
    """
    for i, tok in enumerate(tokens):
        key, sep, _ = tok.partition("=")
        if sep and key in allowed_keys:
            fields = {}
            for t in tokens[i:]:
                k, s, v = t.partition("=")
                if s and k in allowed_keys:
                    fields[k] = v
            return tokens[:i], fields
    return tokens, {}


def weighted_average_uv(
    hourly_times: list[str], hourly_uv: list[float | None], start_time: datetime, end_time: datetime
) -> float | None:
    """
    ממוצע UV משוקלל-משך על פני כל טווח ה-session [start_time, end_time) —
    מחליף התאמה לשעה בודדת (הגרסה הקודמת של fetch_historical_uv), שנתנה
    תמונה שגויה ל-sessions ארוכים/רב-שעתיים. התיקון נובע מתקלה אמיתית
    ב-2026-09-08: session של 11 שעות במצפה רמון הוצג עם UV=0.0 בדשבורד,
    כי הדגימה הבודדת (בזמן פתיחת ה-session) נפלה על שעת לילה, בעוד
    שהיה שיא UV מעל 7 בצהריים אותו יום.

    כל bucket שעתי של Open-Meteo (hourly_times[i]) מייצג את הטווח
    [t, t+1h). מחשבים לכל bucket את החפיפה (בדקות) עם [start_time,
    end_time), ומחזירים ממוצע UV משוקלל לפי משך-החפיפה. זה מתמטית שקול
    לסכימת "מנת חשיפה" לפי-שעה ואז חלוקה בסך-הכל, כי calculate_exposure_score
    לינארית ב-uv_index עבור duration/skin_type/spf קבועים — כלומר אפשר
    להזין את הממוצע-המשוקלל פעם אחת לנוסחה הקיימת בלי לשנות אותה או את
    חוזה ה-DB בכלל. bucket-ים עם uv=None מדולגים. בלי שום חפיפה (או כל
    ה-UV חסר) מחזירים None ומשאירים לקורא (fetch_historical_uv) להחליט.
    """
    total_weight = 0.0
    weighted_sum = 0.0
    for t, uv in zip(hourly_times, hourly_uv):
        if uv is None:
            continue
        bucket_start = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        bucket_end = bucket_start + timedelta(hours=1)
        overlap_start = max(bucket_start, start_time)
        overlap_end = min(bucket_end, end_time)
        overlap_minutes = (overlap_end - overlap_start).total_seconds() / 60
        if overlap_minutes <= 0:
            continue
        weighted_sum += uv * overlap_minutes
        total_weight += overlap_minutes
    if total_weight <= 0:
        return None
    return weighted_sum / total_weight


def fetch_historical_uv(
    client: httpx.Client, lat: float, lon: float, start_time: datetime, end_time: datetime
) -> float | None:
    """
    שולף UV Index היסטורי כממוצע-משוקלל-משך על פני כל טווח ה-session
    [start_time, end_time) — ראו weighted_average_uv לרציונל המלא (כולל
    התקלה האמיתית שהובילה לתיקון הזה). מקבלת start_time/end_time כ-UTC
    "אמיתי" (aware, tzinfo=UTC) — מי שקורא לפונקציה הזו (handle_add_session/
    handle_edit_session) כבר אחראי להמיר את הזמן המקומי שהמשתמש הקליד
    ל-UTC לפני כן (ראו fetch_utc_offset_seconds למטה; תוקן ב-2026-09-08,
    לפני כן הייתה כאן הנחה שגויה ש-HH:MM שהמשתמש מקליד הוא כבר UTC).
    timezone=UTC (לא auto כמו fetch_uv_forecast_next_24h) בדיוק כדי
    ש-hourly.time יתאים ישירות בלי המרה נוספת. past_days מחושב מתאריך
    ההתחלה (start_time, לא end_time —
    ה-session כולו כבר בעבר, ו-forecast_days=1 מכסה את כל שעות "היום"
    הנוכחי, כולל אם end_time הוא today). Open-Meteo תומך עד 92 יום
    אחורה; מעבר לזה (או שהתאריך בעתיד) מחזירים None ומשאירים לקורא
    להחליט איך להגיב (הודעת שגיאה / נפילה חזרה לדגימה הישנה, לפי הנתיב).
    """
    today = datetime.now(timezone.utc).date()
    days_back = (today - start_time.date()).days
    if not (0 <= days_back <= 92):
        return None

    response = client.get(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "uv_index",
            "past_days": days_back,
            "forecast_days": 1,
            "timezone": "UTC",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    hourly = response.json()["hourly"]
    times: list[str] = hourly["time"]
    uvs: list[float] = hourly["uv_index"]
    return weighted_average_uv(times, uvs, start_time, end_time)


def fetch_utc_offset_seconds(client: httpx.Client, lat: float, lon: float) -> int:
    """
    נוספה ב-2026-09-08: מחזירה את הפרשי-השעות מ-UTC (בשניות) של lat/lon
    נתון, לפי ה-timezone (IANA) שבו Open-Meteo מזהה את המיקום בעצמו —
    timezone=auto גורם לתשובה לכלול utc_offset_seconds, בדיוק אותו
    mechanism שכבר בשימוש ותקין ב-fetch_uv_forecast_next_24h למעלה (ראו
    שם: זה גם מה שתיקן בעבר תחזית שהוצגה לפי UTC לניו יורק במקום השעון
    המקומי שם).

    התקלה שהובילה לפונקציה הזו: /add_session ו-/edit_session פירשו
    start=/end=HH:MM כ-UTC "כמו שהוא" בלי שום קשר למיקום בפועל — מי
    שהקליד "מצפה רמון start=5:00" התכוון ל-5 בבוקר שעון ישראל (UTC+2/+3),
    לא ל-5 בבוקר UTC (=7/8 בבוקר בישראל, כבר לא "בוקר מוקדם" בכלל).
    זה גם גרם לבדיקת "שעת הסיום לא יכולה להיות בעתיד" להיכשל/להצליח
    לפי מקרה שרירותי, כי היא השוותה UTC אמיתי מול "UTC" שגוי.

    best-effort: כשל (רשת/תשובה לא תקינה) -> מחזירה 0 (UTC), כלומר
    נופלים בחזרה בבטחה להתנהגות הישנה במקום לקרוס.
    """
    try:
        response = client.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "uv_index",
                "forecast_days": 1,
                "timezone": "auto",
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json().get("utc_offset_seconds", 0)
    except Exception:
        logger.warning("fetch_utc_offset_seconds failed for lat=%s lon=%s — falling back to UTC", lat, lon)
        return 0


def fetch_sunset_utc(client: httpx.Client, lat: float, lon: float) -> datetime | None:
    """
    נוספה 2026-09-12 — מחזירה את רגע השקיעה המקומית של *היום* ב-lat/lon
    נתון, כ-datetime מודע-UTC.

    שימשה את הסגירה האוטומטית בשקיעה, שעברה ב-2026-09-16 ל-Cloudflare
    Worker (cloudflare/sunset-worker — שם ההמרה נמצאת ב-sunsetToUtc).
    נשארה כאן כי היא עומדת בזכות עצמה, ובפרט לבדיקות ידניות.

    Open-Meteo עם daily=sunset&timezone=auto מחזיר משהו כמו
    "2026-09-12T18:47" — שעון *מקומי* (ה-timezone שזוהה אוטומטית לפי
    lat/lon), בלי offset בסוף המחרוזת. אותה תשובה כוללת גם
    utc_offset_seconds (אותו mechanism בדיוק כמו fetch_utc_offset_seconds
    למעלה) — מחסרים אותו מהזמן המקומי כדי לקבל את רגע ה-UTC האמיתי,
    ולא מניחים UTC כמו שהוא (אותה תקלה בדיוק ש-fetch_utc_offset_seconds
    כבר תיקן במקום אחר בקובץ הזה).

    best-effort: כשל (רשת/תשובה לא תקינה/שדה חסר) -> None. הקורא מדלג
    על ה-session הזה בסבב הנוכחי ומנסה שוב בסבב הבא, במקום לסגור
    session שלא בטוחים לגביו.
    """
    try:
        response = client.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "sunset",
                "forecast_days": 1,
                "timezone": "auto",
            },
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()
        sunset_local_naive = datetime.fromisoformat(data["daily"]["sunset"][0])
        utc_offset_seconds = data.get("utc_offset_seconds", 0)
        return (sunset_local_naive - timedelta(seconds=utc_offset_seconds)).replace(tzinfo=timezone.utc)
    except Exception:
        logger.warning("fetch_sunset_utc failed for lat=%s lon=%s", lat, lon)
        return None


def handle_add_session(chat_id: int, username: str, args: str, lang: str = i18n.DEFAULT_LANGUAGE) -> None:
    """
    /add_session <עיר> start=HH:MM end=HH:MM [spf=<מספר>] [date=D.M] [uv=<מספר>]
    לדוגמה: /add_session תל אביב start=14:00 end=16:30 spf=30

    בלי date — מניחים היום (UTC). start=/end=HH:MM מתפרשים כזמן *מקומי
    של העיר* (fetch_utc_offset_seconds, ראו שם) — לא UTC — כי ככה משתמש
    מתכוון: "מצפה רמון start=5:00" זה 5 בבוקר שעון ישראל, לא 5 בבוקר UTC.
    תוקן ב-2026-09-08 (ראו fetch_utc_offset_seconds לרציונל המלא/לתקלה
    האמיתית). end<=start מתפרש כחציית חצות (יום למחרת, בזמן המקומי),
    כמו ב-/edit_session. uv= הוא override ידני; בלעדיו שולפים UV היסטורי
    אוטומטית (fetch_historical_uv, שמקבלת כבר UTC אמיתי).
    """
    ALLOWED = {"start", "end", "spf", "date", "uv"}
    tokens = args.strip().split()
    city_tokens, fields = _parse_kv_fields(tokens, ALLOWED)
    city = " ".join(city_tokens).strip()

    usage = (
        "שימוש: /add_session <עיר> start=HH:MM end=HH:MM [spf=<מספר>] [date=D.M]\n"
        "לדוגמה: /add_session תל אביב start=14:00 end=16:30 spf=30\n"
        "(שעות בזמן המקומי של העיר; בלי date מניחים היום; UV Index נשלף "
        "אוטומטית לפי ההיסטוריה)."
    )
    if not city or "start" not in fields or "end" not in fields:
        send_message(chat_id, usage)
        return

    users = select_rows("users", {"telegram_username": f"eq.{username}"})
    if not users:
        send_message(
            chat_id,
            t("need_skin_type_first", lang),
            reply_markup=_skin_type_keyboard(lang),
        )
        return
    skin_type = users[0]["skin_type"]

    now = datetime.now(timezone.utc)
    target_date = now.date()
    if "date" in fields:
        try:
            day, month = fields["date"].split(".")
            target_date = target_date.replace(month=int(month), day=int(day))
            if target_date > now.date():
                target_date = target_date.replace(year=target_date.year - 1)
        except (ValueError, IndexError):
            send_message(chat_id, "פורמט תאריך לא תקין. השתמשו ב-date=D.M (למשל date=25.8).")
            return

    with httpx.Client() as client:
        geo = geocode_city(client, city)
        if not geo["found"]:
            send_message(chat_id, f'לא הצלחתי לזהות עיר בשם "{city}". בדקו את האיות ונסו שוב.')
            return

        # geocoding זז לפני parsing השעות (בשונה מהקוד הישן) כי צריך את
        # lat/lon כדי לדעת את אזור-הזמן המקומי לפני שאפשר בכלל להמיר
        # start=/end=HH:MM ל-UTC אמיתי.
        utc_offset_seconds = fetch_utc_offset_seconds(client, geo["latitude"], geo["longitude"])

        try:
            start_hh, start_mm = fields["start"].split(":")
            local_start = datetime(
                target_date.year, target_date.month, target_date.day,
                int(start_hh), int(start_mm),
            )
            start_time = (local_start - timedelta(seconds=utc_offset_seconds)).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            send_message(chat_id, "פורמט שעת התחלה לא תקין. השתמשו ב-start=HH:MM (למשל start=14:00).")
            return

        try:
            end_hh, end_mm = fields["end"].split(":")
            local_end = local_start.replace(hour=int(end_hh), minute=int(end_mm), second=0, microsecond=0)
            if local_end <= local_start:
                local_end += timedelta(days=1)  # session שחצה חצות (בזמן המקומי)
            end_time = (local_end - timedelta(seconds=utc_offset_seconds)).replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            send_message(chat_id, "פורמט שעת סיום לא תקין. השתמשו ב-end=HH:MM (למשל end=16:30).")
            return

        if end_time > now:
            send_message(chat_id, "שעת הסיום לא יכולה להיות בעתיד.")
            return

        spf = None
        if "spf" in fields:
            if not fields["spf"].isdigit():
                send_message(chat_id, "spf חייב להיות מספר, למשל spf=30.")
                return
            spf = int(fields["spf"])

        if "uv" in fields:
            try:
                uv_index = float(fields["uv"])
            except ValueError:
                send_message(chat_id, "uv חייב להיות מספר, למשל uv=6.5.")
                return
        else:
            uv_index = fetch_historical_uv(client, geo["latitude"], geo["longitude"], start_time, end_time)
            if uv_index is None:
                send_message(
                    chat_id,
                    "לא הצלחתי לשלוף UV היסטורי לשעה/תאריך הזה (זמין עד כ-92 יום אחורה, "
                    "ולפעמים פחות). אפשר לנסות עם date= קרוב יותר, או להוסיף uv=<מספר> "
                    "ידנית לפקודה.",
                )
                return

    duration_minutes = (end_time - start_time).total_seconds() / 60
    score = calculate_exposure_score(uv_index, duration_minutes, skin_type, spf)

    insert_row(
        "exposure_log",
        {
            "telegram_username": username,
            "city": geo["name"],
            "country": geo["country"],
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "uv_index": uv_index,
            "lat": geo["latitude"],
            "lon": geo["longitude"],
            "spf": spf,
            "exposure_score": score,
        },
    )

    location_label = f"{geo['name']}, {geo['country']}" if geo.get("country") else geo["name"]
    send_message(
        chat_id,
        f"נוסף: session ב{location_label} ({round(duration_minutes)} דקות, UV {uv_index:.1f}). "
        f"מדד חשיפה: {score}%.",
    )
    logger.info("Added manual session for @%s in %s: UV=%s score=%s", username, geo["name"], uv_index, score)


# ---------------------------------------------------------------------
# /my_sessions, /edit_session, /delete_session — ניהול sessions קיימים
# מתוך הבוט (בלי דשבורד/UI נפרד). נועד גם לתקן session שנתקע (כמו
# id=38 ש-uv_index=0 שלו גרם ל-ZeroDivisionError בעבר, ראו התיקון של
# calculate_exposure_score למעלה) בלי לפנות למפתח לתקן ידנית ב-SQL.
# ---------------------------------------------------------------------
def _fmt_dt(iso: str) -> str:
    """מציג timestamp כ-'D.M HH:MM' (UTC) — תואם לפורמט התאריכים בשאר הבוט."""
    dt = datetime.fromisoformat(iso)
    return f"{dt.day}.{dt.month} {dt.strftime('%H:%M')}"


def handle_my_sessions(chat_id: int, username: str) -> None:
    """
    /my_sessions — עד 8 ה-sessions האחרונים של המשתמש, עם ה-id של כל
    אחד כדי לאפשר התייחסות אליו ב-/edit_session/-/delete_session.
    """
    sessions = select_rows(
        "exposure_log",
        {"telegram_username": f"eq.{username}", "order": "start_time.desc", "limit": "8"},
    )
    if not sessions:
        send_message(chat_id, "עוד אין לך sessions רשומים. שלחו /start_session <עיר> כדי להתחיל.")
        return

    lines = ["ה-sessions האחרונים שלך:"]
    for s in sessions:
        start = _fmt_dt(s["start_time"])
        if s["end_time"]:
            status = f"{start}–{datetime.fromisoformat(s['end_time']).strftime('%H:%M')}"
        else:
            status = f"{start}→פתוח"

        extra = []
        if s["uv_index"] is not None:
            extra.append(f"UV {s['uv_index']:.1f}")
        if s["spf"]:
            extra.append(f"SPF {s['spf']}")
        if s["exposure_score"] is not None:
            extra.append(f"ציון {s['exposure_score']}%")
        extra_str = f" · {' · '.join(extra)}" if extra else ""

        lines.append(f"#{s['id']} · {s['city']} · {status}{extra_str}")

    lines.append("")
    # ההוספה/עריכה/מחיקה עברו לדשבורד ב-2026-09-12 (ראו
    # handle_moved_to_dashboard למטה) — הרשימה כאן נשארת לצפייה מהירה
    # בטלגרם, אבל כל שינוי בפועל נעשה באזור האישי.
    lines.append("להוספה, עריכה או מחיקה: /dashboard")
    send_message(chat_id, "\n".join(lines))


def handle_today(chat_id: int, username: str, args: str) -> None:
    """
    /today [date=D.M] — אנליזה יומית: סה"כ מדד חשיפה על כל ה-sessions
    הסגורים שהתחילו ביום המבוקש (UTC, כמו שאר הזמנים באפליקציה; בלי
    date= — היום), + לכל session השוואה "מה היה קורה עם SPF קבוע"
    (DAILY_SUMMARY_REFERENCE_SPF, ראו שם) — כולל sessions שכבר השתמשו
    ב-SPF כלשהו (ההשוואה תמיד מול אותו קבוע, לא רק "בלי הגנה בכלל").
    session פתוח כרגע לא נכלל בסכימה (עדיין אין לו exposure_score מחושב)
    אבל מוזכר בנפרד עם תזכורת ל-/end_session.

    date=D.M הוחלט עם המשתמש ב-2026-09-08 ("הוסף chart_date ככה שאוכל
    לראות את החשיפה לפי תאריך") — אותו פורמט/פרסינג בדיוק כמו
    date= ב-/add_session (כולל גלגול-שנה כש-D.M "עתידי" ביחס להיום),
    לעקביות. גם הגרף (send_daily_exposure_chart) מקבל את אותו target_date.

    באותה בקשה: "שהגרף יציג את המיקום איפה שמד החשיפה היה הגבוה ביותר"
    — מומש ב-_peak_exposure_session (משותף עם render_daily_exposure_chart):
    מוצג גם כאן בטקסט וגם מודגש חזותית על הגרף.

    שולף עד 50 sessions אחרונים ומסנן ליום המבוקש בפייתון (לא ב-query
    עם range filter על start_time) — פשוט יותר לבדיקה, וקצב שימוש-קורס
    שלא מצדיק אופטימיזציה מוקדמת (ראו handle_my_sessions לדפוס דומה).
    """
    args = args.strip()
    target_date = datetime.now(timezone.utc).date()
    if args:
        if not args.startswith("date="):
            send_message(chat_id, "שימוש: /today או /today date=D.M (למשל date=25.8).")
            return
        try:
            day, month = args[len("date="):].split(".")
            candidate = target_date.replace(month=int(month), day=int(day))
            if candidate > target_date:
                candidate = candidate.replace(year=candidate.year - 1)
            target_date = candidate
        except (ValueError, IndexError):
            send_message(chat_id, "פורמט תאריך לא תקין. השתמשו ב-date=D.M (למשל date=25.8).")
            return

    users = select_rows("users", {"telegram_username": f"eq.{username}"})
    skin_type = users[0]["skin_type"] if users else None

    sessions = select_rows(
        "exposure_log",
        {"telegram_username": f"eq.{username}", "order": "start_time.desc", "limit": "50"},
    )
    todays_sessions = _sessions_on_date(sessions, target_date)

    closed = [s for s in todays_sessions if s["end_time"] and s["exposure_score"] is not None]
    open_sessions = [s for s in todays_sessions if not s["end_time"]]

    if not closed and not open_sessions:
        if target_date == datetime.now(timezone.utc).date():
            send_message(chat_id, "עוד אין לך sessions היום. שלחו /start_session <עיר> כדי להתחיל.")
        else:
            send_message(chat_id, f"אין לך sessions בתאריך {target_date.strftime('%d.%m.%Y')}.")
        return

    lines = [f"📊 סיכום ליום {target_date.strftime('%d.%m.%Y')}:"]

    if closed:
        summaries = [_daily_session_summary(s, skin_type, DAILY_SUMMARY_REFERENCE_SPF) for s in closed]
        total_actual = sum(sm["actual_score"] for sm in summaries)
        total_hypothetical = sum(sm["hypothetical_score"] for sm in summaries)
        lines.append(f'{len(closed)} sessions · סה"כ מדד חשיפה: {total_actual}%')
        lines.append(f"עם SPF {DAILY_SUMMARY_REFERENCE_SPF} קבוע לאורך כל היום: כ-{total_hypothetical}% במקום זאת")

        peak = _peak_exposure_session(closed)
        if peak is not None:
            lines.append(f"מדד החשיפה הגבוה ביותר: {peak['city']} ({peak['exposure_score']}%)")

        lines.append("")
        for s, sm in zip(closed, summaries):
            spf_label = f"SPF {sm['spf']}" if sm["spf"] else "בלי קרם הגנה"
            lines.append(
                f"#{sm['id']} · {sm['city']} · UV {s['uv_index']:.1f} · {spf_label} · "
                f"ציון {sm['actual_score']}% (עם SPF {DAILY_SUMMARY_REFERENCE_SPF}: {sm['hypothetical_score']}%)"
            )
        lines.append("")

    if open_sessions:
        cities = ", ".join(s["city"] for s in open_sessions)
        lines.append(f"יש לך גם session פתוח כרגע ב-{cities} — הוא יתווסף לסיכום אחרי /end_session.")

    send_message(chat_id, "\n".join(lines).strip())
    send_daily_exposure_chart(chat_id, todays_sessions, target_date)


def handle_delete_session(chat_id: int, username: str, args: str) -> None:
    """/delete_session <id> — מוחק session, רק אם הוא שייך למשתמש שביקש."""
    session_id = args.strip()
    if not session_id.isdigit():
        send_message(chat_id, "שימוש: /delete_session <מספר> (ראו /my_sessions למספרים).")
        return

    rows = select_rows("exposure_log", {"id": f"eq.{session_id}"})
    if not rows or rows[0]["telegram_username"] != username:
        # אותה הודעה גם אם ה-id שייך למישהו אחר וגם אם הוא לא קיים —
        # לא חושפים למשתמש אם id מסוים "תפוס" ע"י מישהו אחר.
        send_message(chat_id, "לא נמצא session כזה. שלחו /my_sessions לרשימה מעודכנת.")
        return

    session = rows[0]
    delete_rows("exposure_log", {"id": f"eq.{session_id}"})
    send_message(chat_id, f"נמחק: session #{session_id} ב{session['city']} ({_fmt_dt(session['start_time'])}).")
    logger.info("Deleted session id=%s for @%s", session_id, username)


def handle_edit_session(chat_id: int, username: str, args: str) -> None:
    """
    /edit_session <id> [end=now|HH:MM] [spf=<מספר>] — עריכת session קיים
    (סוגר session תקוע, מתקן SPF ששכחו לציין וכו'). לפחות אחד מ-end/spf
    חייב להינתן. end=HH:MM מתפרש כזמן *מקומי* של ה-session (לפי lat/lon
    שלו, אם שמור — fetch_utc_offset_seconds; תוקן ב-2026-09-08 יחד עם
    התיקון הזהה ב-/add_session, ראו שם לרציונל המלא) על אותו יום קלנדרי
    מקומי כמו start_time — אם השעה "לפני" שעת ההתחלה המקומית, מניחים
    חציית חצות ומזיזים ליום הבא. session ישן בלי lat/lon שמור (מלפני
    ה-migration) נופל בחזרה ל-UTC "כמו שהוא", ההתנהגות הישנה. מדד
    החשיפה מחושב מחדש בכל עריכה שיש אחריה end_time (חדש או קיים) —
    אחרת נשאר None, בדיוק כמו session פתוח רגיל (יחושב סופית ב-/end_session).
    """
    parts = args.strip().split()
    if not parts or not parts[0].isdigit():
        send_message(
            chat_id,
            "שימוש: /edit_session <מספר> end=now|HH:MM ו/או spf=<מספר>\n"
            "לדוגמה: /edit_session 38 end=now spf=30\n"
            "(ראו /my_sessions למספרים).",
        )
        return

    session_id = parts[0]
    fields = {}
    for token in parts[1:]:
        key, sep, value = token.partition("=")
        if sep and key in ("end", "spf"):
            fields[key] = value

    if not fields:
        send_message(chat_id, "צריך לציין לפחות end=... או spf=... לעריכה.")
        return

    rows = select_rows("exposure_log", {"id": f"eq.{session_id}"})
    if not rows or rows[0]["telegram_username"] != username:
        send_message(chat_id, "לא נמצא session כזה. שלחו /my_sessions לרשימה מעודכנת.")
        return
    session = rows[0]

    patch = {}

    if "spf" in fields:
        if not fields["spf"].isdigit():
            send_message(chat_id, "spf חייב להיות מספר, למשל spf=30.")
            return
        patch["spf"] = int(fields["spf"])

    if "end" in fields:
        start_time = datetime.fromisoformat(session["start_time"])
        if fields["end"].lower() == "now":
            end_time = datetime.now(timezone.utc)
        else:
            lat, lon = session.get("lat"), session.get("lon")
            utc_offset_seconds = 0
            if lat is not None and lon is not None:
                with httpx.Client() as client:
                    utc_offset_seconds = fetch_utc_offset_seconds(client, lat, lon)
            try:
                hh, mm = fields["end"].split(":")
                local_start = (start_time + timedelta(seconds=utc_offset_seconds)).replace(tzinfo=None)
                local_end = local_start.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if local_end <= local_start:
                    local_end += timedelta(days=1)  # session שחצה חצות (בזמן המקומי)
                end_time = (local_end - timedelta(seconds=utc_offset_seconds)).replace(tzinfo=timezone.utc)
            except (ValueError, IndexError):
                send_message(chat_id, "פורמט שעה לא תקין. השתמשו ב-end=now או end=HH:MM (למשל end=22:30).")
                return
        patch["end_time"] = end_time.isoformat()

    effective_end = patch.get("end_time", session["end_time"])
    effective_spf = patch.get("spf", session["spf"])
    if effective_end:
        end_dt = datetime.fromisoformat(effective_end)
        start_dt = datetime.fromisoformat(session["start_time"])
        duration_minutes = (end_dt - start_dt).total_seconds() / 60
        if duration_minutes < 0:
            send_message(chat_id, "שעת הסיום לא יכולה להיות לפני שעת ההתחלה.")
            return
        users = select_rows("users", {"telegram_username": f"eq.{username}"})
        skin_type = users[0]["skin_type"] if users else 1  # ראו ההסבר ב-handle_end_session
        patch["exposure_score"] = calculate_exposure_score(
            session["uv_index"], duration_minutes, skin_type, effective_spf
        )

    update_rows("exposure_log", {"id": f"eq.{session_id}"}, patch)
    send_message(chat_id, f"עודכן: session #{session_id} ב{session['city']}.")
    logger.info("Edited session id=%s for @%s: %s", session_id, username, patch)


# ---------------------------------------------------------------------
# /add_session, /edit_session, /delete_session — הועברו לדשבורד
# ---------------------------------------------------------------------
def handle_moved_to_dashboard(chat_id: int, username: str, args: str) -> None:
    """
    2026-09-12: הוספה, עריכה ומחיקה של sessions עברו מהבוט לאזור האישי
    (docs/dashboard/index.html + Edge Function dashboard-sessions). טופס
    עם שדות מובנים עדיף כאן על תחביר טקסטואלי שצריך לזכור
    ("/add_session <עיר> start=HH:MM end=HH:MM spf=.."), במיוחד לעריכה
    שדרשה גם לדעת את מספר ה-session מראש.

    הפקודות עצמן נשארות רשומות ב-COMMAND_HANDLERS *בכוונה*, ממופות
    לפונקציה הזו: מי שרגיל אליהן מקבל הסבר וקישור ישיר במקום שתיקה או
    תשובה כללית מה-Agent Loop (כל טקסט שלא תואם פקודה מוכרת מגיע לשם).
    """
    link = create_magic_link(username)
    send_message(
        chat_id,
        "הוספה, עריכה ומחיקה של sessions עברו לאזור האישי — שם יש טופס "
        "מסודר במקום לזכור תחביר של פקודה.\n\n"
        f"{link}\n\n"
        "(הקישור בתוקף ל-24 שעות. למדידה בזמן אמת אפשר להמשיך להשתמש "
        "ב-/start_session ו-/end_session כרגיל.)",
    )
    logger.info("Redirected @%s from a moved session command to the dashboard", username)


# ---------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------
# הערה על handle_add_session/handle_edit_session/handle_delete_session
# למעלה: הן כבר לא מחוברות לשום פקודה (ראו handle_moved_to_dashboard),
# אבל הקוד שלהן — והבדיקות test_add_session_manual.py/test_edit_session.py
# שמכסות אותו — נשמרו בכוונה בשלב הזה, כדי שהמעבר לדשבורד יהיה הפיך
# בלי לשחזר לוגיקה מההיסטוריה. אפשר למחוק אותן בניקיון נפרד אחרי
# שהזרימה החדשה תרוץ בפרודקשן ותוכיח את עצמה.
COMMAND_HANDLERS = {
    "/start": handle_start,
    "/dashboard": lambda chat_id, username, args: handle_dashboard(chat_id, username),
    "/set_skin_type": handle_set_skin_type,
    "/start_session": handle_start_session,
    "/end_session": handle_end_session,
    "/offline_session": handle_offline_session,
    "/my_sessions": lambda chat_id, username, args: handle_my_sessions(chat_id, username),
    "/today": handle_today,
    "/delete_session": handle_moved_to_dashboard,
    "/edit_session": handle_moved_to_dashboard,
    "/add_session": handle_moved_to_dashboard,
    "/diagnose_skin": handle_diagnose_skin,
}


def _build_freeform_task(user_text: str, lang: str = i18n.DEFAULT_LANGUAGE) -> str:
    """
    בונה את ה-task שנשלח ל-Agent Loop עבור הודעת טקסט חופשית (ראו
    _handle_freeform_question).

    ה-prompt עצמו נשאר בעברית — הוא פונה למודל, לא למשתמש; רק שפת
    *התשובה* נגזרת מ-lang (2026-09-14).

    באותו תאריך נוספה גם ההנחיה לשאלות על הבוט עצמו. עד אז הגייטקיפר
    סיווג אותן כ-NOISE והן נענו בשתיקה מוחלטת — משתמש אמיתי שאל
    "English?" ולא קיבל כלום. מאז שהן VALID הן מגיעות לכאן, וצריכה
    להיות להן תשובה אמיתית ולא הפניה גנרית לתפריט.
    """
    answer_language = "בעברית" if lang == "he" else "באנגלית"

    # התאריך חייב להיכנס ל-prompt במפורש (2026-09-15). בלי זה המודל
    # מחשב "אתמול" מתוך תחושת ה"עכשיו" שנצרבה באימון שלו — באג אמיתי
    # שנתפס בפרודקשן: על השאלה "מה היה ה-UV במצפה רמון אתמול?" הוא קרא
    # ל-get_historical_uv עם 2025-05-18 והחזיר UV אמיתי לתאריך שגוי
    # בשנה וארבעה חודשים. הכלי עשה בדיוק מה שהתבקש; ההקשר הוא שחסר.
    #
    # הגרסה הראשונה של התיקון נקבה רק ב"אתמול", וזה לא הספיק: "שלשום",
    # "לפני שבוע" ו"בשבת" נשארו תלויים בחישוב של המודל. במקום להוסיף
    # מילה-מילה, מוסרים לו **לוח עוגנים** — שבעת הימים האחרונים עם שם
    # היום והתאריך, ועוד שני עוגנים רחוקים. כך המודל לא מחשב אף פעם,
    # הוא רק בוחר שורה; וזה מכסה גם ניסוחים שלא חשבנו עליהם מראש.
    now_utc = datetime.now(timezone.utc)
    today = now_utc.date()
    today_iso = today.isoformat()

    hebrew_weekdays = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")
    relative_names = {1: "אתמול", 2: "שלשום"}

    anchor_lines = [f"  היום (יום {hebrew_weekdays[today.weekday()]}) = {today_iso} · days_back=0"]
    for back in range(1, 8):
        d = today - timedelta(days=back)
        label = f"יום {hebrew_weekdays[d.weekday()]}"
        if back in relative_names:
            label = f"{relative_names[back]}, {label}"
        elif back == 7:
            label = f"לפני שבוע, {label}"
        anchor_lines.append(f"  {label} = {d.isoformat()} · days_back={back}")
    anchor_lines.append(
        f"  לפני חודש = {(today - timedelta(days=30)).isoformat()} · days_back=30"
    )
    anchor_lines.append(
        f"  לפני שנה = {(today - timedelta(days=365)).isoformat()} · days_back=365"
    )
    anchors = "\n".join(anchor_lines)

    return (
        f"השעה עכשיו ב-UTC: {today_iso} {now_utc:%H:%M}.\n"
        "לוח התאריכים שלך — השתמש בו ואל תחשב תאריכים בעצמך, "
        "ואל תסתמך על שום ידיעה אחרת לגבי התאריך הנוכחי:\n"
        f"{anchors}\n"
        "לביטוי שלא מופיע בלוח, בחר את העוגן הקרוב ביותר וספור ממנו "
        "ימים שלמים.\n\n"
        "אתה חלק מבוט טלגרם בשם SunSafe שעוזר למשתמשים לעקוב אחרי חשיפה "
        "לקרינת UV ולהתגונן מהשמש. המשתמש שלח הודעה חופשית (לא פקודה "
        "מוכרת) שכבר סוננה מספאם/רעש ברורים על ידי סינון קודם:\n\n"
        f'"{user_text}"\n\n'
        "אם זו שאלה על UV/מזג אוויר במקום מסוים — ענה עליה עם הכלים "
        "הזמינים לך, ובמספר הקריאות הקטן ביותר. לשאלה על *עכשיו* "
        "קרא ל-get_weather_for_city עם שם העיר: הוא מחזיר UV, "
        "טמפרטורה, עננות ולחות בקריאה אחת, ואין צורך ב-geocode_city "
        "לפניו. לתחזית ולעבר כן צריך geocode_city קודם (אסור לנחש "
        "קואורדינטות מידע כללי), ואחריו get_uv_forecast "
        "לימים הבאים, או get_historical_uv לכל תאריך שכבר עבר — כולל "
        "אתמול, החודש שעבר או לפני שנה. אם get_historical_uv מחזיר "
        "found=false, אמור זאת כפי שהוא ואל תעריך ערך בעצמך.\n\n"
        "לשאלה יחסית (\"אתמול\", \"לפני שבוע\", \"לפני שנה\") העבר "
        "ל-get_historical_uv את days_back — 1, 7, 365 — ולא תאריך "
        "שחישבת בעצמך. תאריך מוחלט (date_iso) רק כשהמשתמש נקב בתאריך "
        "מדויק. **תמיד ציין בתשובה את התאריך שהכלי החזיר בשדה date**, "
        "כדי שהמשתמש יראה על איזה יום ענית.\n\n"
        "שים לב לגבולות התחום: לעבר יש **UV בלבד**. טמפרטורה, עננות "
        "ולחות זמינות רק למצב הנוכחי (get_current_uv). אם שאלו על "
        "\"מזג האוויר\" בתאריך שעבר — תן את ה-UV ואמור בפשטות שטמפרטורה "
        "היסטורית היא לא משהו שהבוט עוקב אחריו.\n\n"
        "אם זו שאלה על הנתונים **האישיים** של המשתמש — החשיפה שלו, "
        "ה-sessions שלו, כמה זמן *הוא* היה בשמש — למשל \"כמה זמן הייתי "
        "בשמש היום\", \"מה היה מדד החשיפה שלי אתמול\", \"תראה לי את "
        "ה-sessions שלי\" — **אל תנסה לחשב או לנחש תשובה** (אין לך גישה "
        "לנתונים של המשתמש דרך הכלים שברשותך): תפנה אותו ל-/dashboard, "
        "האזור האישי, שם יש היסטוריה מלאה, ניתוח יומי וחודשי, ואפשרות "
        "להוסיף ולערוך sessions.\n\n"
        "ההבחנה הזו חשובה: \"מה היה ה-UV במצפה רמון אתמול\" היא שאלה על "
        "*מקום* ונענית עם get_historical_uv. \"מה היה מדד החשיפה שלי "
        "אתמול\" היא שאלה על *המשתמש* ונענית בהפניה ל-/dashboard.\n\n"
        "ובשום מקרה אל תטען שהבוט לא שומר היסטוריה או שאין לו נתוני "
        "עבר — יש לו את שניהם: כל session של המשתמש נשמר ומוצג "
        "ב-/dashboard, ו-UV היסטורי נשלף בכלי שלמעלה.\n\n"
        "אם זו שאלה על הבוט עצמו — מה הוא יודע לעשות או איך משתמשים בו "
        "— ענה עליה ישירות ובקצרה: הוא עוקב אחרי חשיפה לשמש "
        "(/start_session כשיוצאים, /end_session כשחוזרים, והוא מחשב מדד "
        "חשיפה אישי לפי סוג העור וקרם ההגנה), מדווח UV ותחזית לכל מקום, "
        "ומרכז הכל ב-/dashboard.\n\n"
        "אם זו שאלה על שפה (\"English?\", \"אפשר באנגלית?\") — ענה בדיוק "
        "את האמת הזו ואל תוסיף עליה: הבוט עובד בעברית בלבד כרגע. "
        "**אין** הגדרת שפה, אין מתג ואין מסך הגדרות — אסור להמציא כאלה "
        "ואסור להפנות את המשתמש ל\"הגדרות\" או לדשבורד בשביל שפה, "
        "ואסור להבטיח שפות שהבוט לא מדבר. אפשר לומר בנימוס שתמיכה "
        "באנגלית מתוכננת בהמשך.\n\n"
        "אם זו לא שאלה מאף אחד מהסוגים האלה (קטע טקסט לא ברור) — הסבר "
        "בקצרה מה הבוט עושה ושאפשר לשאול אותו ישירות על UV במקום מסוים.\n\n"
        f"ענה {answer_language}, קצר וברור (זו הודעת טלגרם) — בלי Markdown."
    )


def _handle_freeform_question(
    chat_id: int, username: str, text: str, lang: str = i18n.DEFAULT_LANGUAGE
) -> None:
    """
    טקסט חופשי שעבר את הגייטקיפר כ-VALID אבל לא תואם אף פקודה מוכרת —
    למשל "מה ה-UV בתל אביב עכשיו?" — מנותב ל-Agent Loop (mcp_agent_loop.py),
    שמדבר עם mcp_weather_server.py דרך MCP, במקום להיעלם בשקט כמו קודם.
    ראו docs/2026-08-20 (סעיף "MCP-integration discussion" בסיכום הסקירה):
    זה בדיוק הרעיון שהוזכר שם, מוצמד ל-VALID הקיים במקום מנגנון נפרד.

    בכוונה רק כלי-מידע (geocode_city/get_current_uv/get_uv_forecast) —
    אין ל-Agent Loop שום כלי MCP לפעולות session (start/end וכו'), אז
    הודעה חופשית לא יכולה "לפתוח session" בטעות במקום המשתמש; לכל היותר
    תיתן תשובת מידע או הסבר שמפנה לפקודה המתאימה.

    הערה על latency: זו קריאה סינכרונית שמרימה subprocess (MCP server)
    ועושה סבב-שיחה מול Gemini — לוקחת כמה שניות. עד 16.9.2026 היא גם
    חסמה את לולאת ה-polling כולה, כלומר שאלה אחת עצרה את *כל* המשתמשים
    עד שהיא נענתה. זה כבר לא המצב: העיבוד עבר ל-ThreadPoolExecutor
    (ראו UPDATE_WORKERS), אז שאלה איטית מעכבת רק את השיחה שלה. מה
    שנשאר יקר זה הרמת ה-subprocess בכל שאלה ומכסת Gemini — ראו
    rate_limit.py.
    """
    try:
        answer = run_agent_via_mcp(_build_freeform_task(text, lang))
    except Exception:
        logger.exception("Agent Loop failed answering free-text message from @%s: %s", username, text[:50])
        send_message(
            chat_id,
            t("agent_failed", lang),
        )
        return

    send_message(chat_id, answer)
    logger.info("Answered free-text message from @%s via Agent Loop: %s", username, text[:50])


@functools.lru_cache(maxsize=None)
def _handler_takes_lang(handler) -> bool:
    """
    האם ה-handler מצהיר על פרמטר lang. מאפשר לתרגם פקודה אחת בכל פעם
    (ראו ההערה על שלבים ב-i18n.py) בלי לשנות בבת אחת את החתימה של כל
    שנים-עשר ה-handlers ואת הבדיקות שלהם. ממוקאש כי זה נקרא על כל עדכון.
    """
    try:
        return "lang" in inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return False


def _dispatch(handler, chat_id: int, username: str, args: str, lang: str) -> None:
    """
    קורא ל-handler, ומעביר lang רק אם הוא יודע לקבל אותו.

    16.9.2026: כל חריגה מ-handler הגיעה עד ה-except של poll_forever,
    שרשם "Failed to handle update" ללוג — ולמשתמש לא נשלח *שום דבר*.
    כלומר כל תקלה בצד שלנו נראתה למשתמש כמו בוט שפשוט מתעלם ממנו.
    נצפה בפועל כשה-UV חזר null מ-Open-Meteo ו-/start_session נפל על
    400 מ-Supabase: שלוש הודעות, אפס תשובות. הלוג נשאר בדיוק כשהיה
    (poll_forever עוד רושם exception), רק שעכשיו גם עונים.
    """
    try:
        if _handler_takes_lang(handler):
            handler(chat_id, username, args, lang=lang)
        else:
            handler(chat_id, username, args)
    except UvUnavailableError:
        logger.exception("No UV value available for @%s", username)
        send_message(
            chat_id,
            "לא הצלחתי לקבל את מדד ה-UV לנקודה הזו כרגע — זו תקלה בשירות "
            "מזג האוויר, לא אצלכם. נסו שוב בעוד כמה דקות.",
        )
    except Exception:
        # מכוון רחב: עדיף הודעה גנרית על שתיקה. ה-exception ממשיך ללוג
        # דרך logger.exception כאן, אז שום מידע לא נאבד לדיבוג.
        logger.exception("Handler failed for @%s (args=%r)", username, args)
        send_message(
            chat_id,
            "משהו נשבר אצלי בדרך לתשובה. נסו שוב, ואם זה חוזר — זו תקלה "
            "אצלנו ולא אצלכם.",
        )


def handle_callback_query(callback_query: dict) -> None:
    """
    לחיצה על כפתור inline. נוסף 2026-09-12 יחד עם בורר סוג-העור — עד אז
    handle_update קרא רק update["message"], כלומר כפתורי inline בכלל לא
    יכלו להחזיר תשובה (הכפתור היחיד שהיה, ה-Mini App של האופליין, פותח
    web_app ולא שולח callback).

    שני דברים שחייבים לקרות בכל מסלול: קריאה ל-answer_callback_query
    (אחרת הכפתור נראה תקוע למשתמש), ואימות שה-callback_data הוא באמת
    אחד מאלה שאנחנו שולחים — callback_data מגיע מהלקוח ואין שום ערובה
    שהוא לא נגרד/שוחזר ידנית, אז מתייחסים אליו כקלט לא אמין.
    """
    query_id = callback_query.get("id")
    data = callback_query.get("data") or ""
    message = callback_query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    username = (callback_query.get("from") or {}).get("username")
    # ללחיצה על כפתור אין טקסט משלה, אז נגזרים מההודעה שהכפתור
    # יושב עליה — היא נשלחה בשפה מסוימת, והתשובה צריכה להתאים לה.
    lang = resolve_language(message.get("caption") or message.get("text"))

    if not query_id or not chat_id or not username:
        logger.warning("Ignoring malformed callback_query: %s", callback_query)
        return

    if not data.startswith(SKIN_TYPE_CALLBACK_PREFIX):
        answer_callback_query(query_id)
        logger.warning("Unknown callback_data from @%s: %r", username, data)
        return

    raw = data[len(SKIN_TYPE_CALLBACK_PREFIX):]

    # "לא בטוחים? שלחו תמונה" — אין ב-Telegram כפתור שפותח מצלמה, אז
    # מסבירים ומסתמכים על נתיב התמונה הקיים (handle_skin_type_photo,
    # שהוא ממילא ברירת המחדל לכל תמונה נכנסת).
    if raw == SKIN_CALLBACK_PHOTO:
        answer_callback_query(query_id)
        send_message(chat_id, t("skin_photo_instructions", lang))
        logger.info("Sent hand-photo instructions to @%s", username)
        return

    # "בחירה אחרת" — חוזרים לבורר המלא אחרי הצעה מתמונה.
    if raw == SKIN_CALLBACK_AGAIN:
        answer_callback_query(query_id)
        send_message(chat_id, t("skin_question", lang), reply_markup=_skin_type_keyboard(lang))
        return

    if not raw.isdigit() or not (1 <= int(raw) <= 6):
        answer_callback_query(query_id)
        logger.warning("Out-of-range skin type in callback_data from @%s: %r", username, data)
        return

    skin_type = int(raw)
    # סוגרים את הספינר לפני הכתיבה ל-DB: הלחיצה כבר "נקלטה" מבחינת
    # המשתמש, ואין סיבה שהוא יראה כפתור תקוע בזמן קריאה ל-Supabase.
    answer_callback_query(query_id, t("skin_toast", lang, n=skin_type))
    _pending_skin_type_pick.pop(username, None)
    _save_skin_type(chat_id, username, skin_type, lang)
    logger.info("Skin type %s set by @%s via button", skin_type, username)


def handle_update(update: dict) -> None:
    """
    מטפל בעדכון בודד מ-getUpdates: לחיצה על כפתור inline, אחת מהפקודות
    המוכרות, תמונה בודדת (הצעת סוג עור/בדיקת נזק-שמש), מיקום משותף, או
    טקסט חופשי — VALID (לא NOISE, לא פקודה מוכרת) מנותב ל-Agent Loop
    דרך MCP, ראו _handle_freeform_question.
    """
    callback_query = update.get("callback_query")
    if callback_query:
        handle_callback_query(callback_query)
        return

    message = update.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    username = message.get("from", {}).get("username")

    text = (message.get("text") or "").strip()
    photo_sizes = message.get("photo")  # רשימת PhotoSize מהקטנה לגדולה, או None
    location = message.get("location")  # {"latitude": ..., "longitude": ...} או None

    if not photo_sizes and not location and not text:
        return

    _mirror_incoming_to_admin(chat_id, username, text, photo_sizes, location)

    # בחירת סוג-עור "אינטראקטיבית" — ראו _pending_skin_type_pick למעלה.
    # ספרה בודדת (1-6) שמגיעה בזמן שיש דגל pending (מ-/start או מהודעת-
    # שימוש של /set_skin_type) מנותבת ישירות ל-handle_set_skin_type,
    # *לפני* הגייטקיפר: טקסט כל כך קצר (≤2 תווים) היה נבלע כ-NOISE
    # ב-classify_message לפני שיש בכלל סיכוי להבין שזו תשובה לשאלה של
    # הבוט עצמו. בכוונה תקף רק כשה-flag פעיל (TTL קצר) — כדי שספרה
    # בודדת סתמית בלי הקשר (לא בעקבות שאלה שהבוט שאל) לא "תיתפס" בטעות.
    if (
        username
        and text
        and not photo_sizes
        and not location
        and text.isdigit()
        and 1 <= int(text) <= 6
    ):
        expires_at = _pending_skin_type_pick.pop(username, None)
        if expires_at is not None and datetime.now(timezone.utc) < expires_at:
            handle_set_skin_type(chat_id, username, text)
            return

    # Lightweight LLM gatekeeper — רץ על *כל* הודעת טקסט נכנסת, *לפני* כל
    # לוגיקה אמיתית (כתיבה ל-DB, dispatch לפקודות). תמונות ומיקומים הם
    # קלט טלגרם מובנה (share מפורש של המשתמש) ותמיד עוברים בלי סיווג.
    # פקודה מוכרת בדיוק (התאמה מלאה ל-COMMAND_HANDLERS) מדלגת על הקריאה
    # ל-LLM — היא כבר מובנית ולגיטימית מבחינה מבנית, וזה חוסך quota.
    # כל טקסט אחר (כולל "/" שלא תואם שום פקודה אמיתית — למשל ניסיון
    # spam/injection שמתחפש לפקודה) עובר סיווג אמיתי אצל Gemini. זה
    # התיקון לבאג הקודם: קודם הבדיקה רצה *אחרי* פילטר שכבר סינן כל דבר
    # שלא "/", וגם הייתה ב-classify_message עצמו קיצור-דרך ש"/" = VALID
    # תמיד בלי לשאול את המודל בכלל — כלומר שום הודעה לא הייתה מסווגת
    # בפועל אף פעם. שני הדברים תוקנו.
    if text and not photo_sizes and not location:
        command, _, _ = text.partition(" ")
        if command not in COMMAND_HANDLERS:
            classification = classify_message(text)
            # logger.info, לא logger.debug בכוונה — כל שאר הלוגר במערכת (ראו
            # logging.basicConfig(level=logging.INFO) למעלה) מוגדר ברמת INFO,
            # אז logger.debug פשוט לא היה נראה בכלל בלוגים של ה-HF Space.
            # זה בדיוק מה שקרה בפועל: לא הייתה שום דרך לוודא מבחוץ אם ה-
            # gatekeeper בכלל רץ, גם כשהוא כן עבד. עכשיו רואים את שתי
            # התוצאות האפשריות במפורש.
            logger.info(
                "Gatekeeper: classified @%s message as %s: %s",
                username, classification, text[:50],
            )
            if classification == "NOISE":
                return

    # עברית כברירת מחדל; אנגלית רק אם המשתמש באמת כותב אנגלית.
    # ראו i18n.resolve_language לרציונל (ולתקלה שהובילה לזה).
    lang = resolve_language(text)

    if not username:
        send_message(chat_id, t("need_username", lang))
        return

    # רענון הזדמנותי של chat_id על כל הודעה — לא insert (PATCH בלבד),
    # אז אם עוד אין שורת users למשתמש הזה (לא קבע סוג עור מעולם) זה
    # פשוט לא פוגע בכלום. מכסה משתמשים שקבעו סוג עור *לפני* שהיה
    # chat_id בכלל. ראו docs/2026-08-26-multi-user-broadcast-design.md.
    try:
        update_rows("users", {"telegram_username": f"eq.{username}"}, {"chat_id": chat_id})
    except SupabaseError as e:
        logger.warning("Failed to refresh chat_id for @%s: %s", username, e)

    # כל שלושת נתיבי הפעולה (תמונה / מיקום / פקודה) עטופים יחד:
    # עד 2026-09-13 רק נתיב הפקודות היה מוגן, ונתיבי התמונה והמיקום
    # חזרו ב-return לפני ה-try. המשמעות בפועל (נצפה בפרודקשן): משתמש
    # שיתף מיקום, Supabase החזיר 504 חולף, החריגה עלתה עד poll_forever,
    # נרשמה ללוג — והמשתמש לא קיבל *כלום*. בדיוק אותה חוויה של "לחצתי
    # ולא קרה כלום" שכבר רדפה אותנו. עכשיו לכל כשל כזה יש תשובה.
    try:
        if photo_sizes:
            largest_photo = photo_sizes[-1]
            # /diagnose_skin "תופס" את התמונה הבאה (בתוך חלון הזמן), אחרת
            # ברירת המחדל הקיימת נשארת — הצעת סוג עור. ראו ההערה מעל
            # _pending_diagnose_skin להסבר המלא על הבחירה הזו.
            expires_at = _pending_diagnose_skin.pop(username, None)
            if expires_at is not None and datetime.now(timezone.utc) < expires_at:
                handle_skin_damage_photo(chat_id, username, largest_photo["file_id"])
            else:
                handle_skin_type_photo(chat_id, username, largest_photo["file_id"], lang)
            return

        if location:
            handle_start_session_location(chat_id, username, location["latitude"], location["longitude"], lang)
            return

        command, _, args = text.partition(" ")
        handler = COMMAND_HANDLERS.get(command)
        if handler is None:
            # לא פקודה מוכרת, אבל כבר עבר את הגייטקיפר כ-VALID (אחרת היינו
            # חוזרים למעלה) — טקסט חופשי לגיטימי-כנראה, מנותב ל-Agent Loop
            # במקום להיעלם בשקט (ראו _handle_freeform_question).
            _handle_freeform_question(chat_id, username, text, lang)
            return

        _dispatch(handler, chat_id, username, args, lang)
    except SupabaseError as e:
        logger.error("Supabase error handling update for @%s: %s", username, e)
        send_message(chat_id, t("storage_error", lang))


# ---------------------------------------------------------------------
# עיבוד עדכונים במקביל
# ---------------------------------------------------------------------
# נוסף 16.9.2026. getUpdates נשאר צרכן *יחיד* — אין ברירה, טלגרם מחזיר
# 409 Conflict לשני מאזינים על אותו טוקן, וזו תקרה ארכיטקטונית שאין
# חומרה שקונה דרכה. אבל ה*טיפול* בעדכונים לא חייב להיות טורי, וזה מה
# שהיה: handle_update נקרא בלופ, אחד אחרי השני, כך שמשתמש אחד ששאל
# שאלה חופשית (הרמת תהליך MCP + כמה סבבי Gemini — שניות) עצר את כל
# השאר עד שקיבל תשובה.
#
# שתי מגבלות שהתכנון חייב לכבד, ושתיהן נובעות מהמקביליות עצמה:
#
# 1. **סדר בתוך שיחה.** שתי הודעות מאותו משתמש חייבות להתעבד בסדר
#    שנשלחו — אחרת "/start_session" ו-"תל אביב" עלולים להתחלף, או
#    ששתי לחיצות על בורר סוג-העור יתנגשו על _pending_skin_type_pick.
#    לכן מנעול לכל chat_id: טורי בתוך שיחה, מקבילי בין שיחות. זו גם
#    הסיבה שאין צורך במנעול על שני מילוני ה-_pending_* — הם ממופתחים
#    לפי משתמש, ושני threads לא יגעו באותו מפתח בו-זמנית.
#
# 2. **מכסת Gemini.** העיבוד הטורי היה מגבִּיל-קצב בלי שתוכנן ככזה:
#    thread אחד שמחכה ~10 שניות לתשובה לא *מסוגל* להוציא יותר מ-~6
#    קריאות בדקה, וה-tier החינמי מוגבל ל-~15-30. ברגע שמקבילים,
#    המגבלה המקרית הזו נעלמת וה-429-ים מתחילים. ראו rate_limit.py —
#    בלי הדלי הזה, המקביליות הופכת "איטי" ל"נכשל".
UPDATE_WORKERS = int(os.environ.get("UPDATE_WORKERS", "8"))

_chat_locks: dict[int, threading.Lock] = {}
_chat_locks_guard = threading.Lock()


def _chat_lock(chat_id: int) -> threading.Lock:
    with _chat_locks_guard:
        return _chat_locks.setdefault(chat_id, threading.Lock())


def _chat_id_of(update: dict) -> int | None:
    """
    ה-chat_id שאליו שייך העדכון, לכל סוגי העדכונים שאנחנו מטפלים בהם.
    None = לא הצלחנו לזהות, ואז העדכון מטופל בלי מנעול (עדיף מלהפיל
    אותו; בפועל לכל message/callback_query יש chat).
    """
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is not None:
        return chat_id
    callback_message = (update.get("callback_query") or {}).get("message") or {}
    return (callback_message.get("chat") or {}).get("id")


def _handle_update_serialized(update: dict) -> None:
    """handle_update תחת מנעול הצ'אט, עם אותו לוג כשל כמו קודם."""
    chat_id = _chat_id_of(update)
    try:
        if chat_id is None:
            handle_update(update)
        else:
            with _chat_lock(chat_id):
                handle_update(update)
    except Exception:
        logger.exception("Failed to handle update: %s", update)


def poll_forever() -> None:
    """
    לולאת polling מול getUpdates. long-polling של 30 שניות לכל בקשה —
    לא צורך CPU/רשת מיותרים בין עדכונים.

    הקליטה טורית (צרכן יחיד, כפי שטלגרם דורש) וה*עיבוד* מקבילי דרך
    ThreadPoolExecutor — ראו ההערה מעל UPDATE_WORKERS. הלולאה עצמה
    לא עושה שום עבודה מלבד להתקדם ב-offset ולהעביר ל-pool, ולכן
    handler איטי לא מעכב יותר את הקליטה של עדכונים חדשים.
    """
    logger.info("Listening for commands (polling): %s", list(COMMAND_HANDLERS))
    logger.info("Update workers: %s", UPDATE_WORKERS)
    offset = None
    with httpx.Client() as client, ThreadPoolExecutor(
        max_workers=UPDATE_WORKERS, thread_name_prefix="sunsafe-update"
    ) as pool:
        while True:
            # קריאת ה-getUpdates עצמה עטופה עכשיו ב-try/except (בעבר לא
            # הייתה עטופה — 409 Conflict אמיתי מטלגרם, למשל משני מאזינים
            # על אותו טוקן, הפיל את כל התהליך עם unhandled exception).
            # כשל חד-פעמי (409, timeout, 5xx רגעי) נרשם ללוג ומנסים שוב
            # אחרי המתנה קצרה, במקום להפיל את הבוט כולו.
            try:
                params = {"timeout": 30}
                if offset is not None:
                    params["offset"] = offset
                response = client.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35.0)
                response.raise_for_status()
                updates = response.json().get("result", [])
            except Exception:
                logger.exception("getUpdates failed — retrying in 5s")
                time.sleep(5)
                continue

            for update in updates:
                # ה-offset מתקדם לפני העיבוד, כמו קודם: טלגרם מקבל
                # אישור שהעדכון נקלט ולא ישלח אותו שוב. המשמעות לא
                # השתנתה מהמימוש הטורי — גם שם offset עלה לפני
                # handle_update — אבל כאן היא בולטת יותר: אם התהליך
                # ייפול בעוד עדכונים ב-pool, הם יאבדו. זו ההתנהגות
                # שהייתה, והחלופה (אישור אחרי עיבוד) הייתה גורמת
                # לעיבוד כפול בכל פריסה מחדש.
                offset = update["update_id"] + 1
                pool.submit(_handle_update_serialized, update)


# ---------------------------------------------------------------------
# סגירה אוטומטית של sessions אחרי שקיעה — עברה ל-Cloudflare Worker
# ---------------------------------------------------------------------
# נבנתה כאן ב-2026-09-12 כ-thread רקע בטיק של 5 דקות, ועברה ב-2026-09-16
# ל-cloudflare/sunset-worker (Cron Trigger). הסיבה: זו עבודה מתוזמנת,
# ולהריץ אותה בתוך התהליך שמריץ את לולאת ה-polling היחידה הפיל אותה
# יחד עם הבוט בכל פריסה מחדש או קריסה.
#
# ה-Worker מייצר את **אותה הודעת סיום בדיוק** — יש שם בדיקה שמשווה
# אותה תו-בתו מול fixture שנוצר מהפייתון הזה (ראו
# cloudflare/sunset-worker/scripts/gen_fixture.py). אם משנים כאן את
# הנוסחה או את נוסח ההודעה, יש להריץ אותו מחדש ולבדוק ששניהם מסכימים.


if __name__ == "__main__":
    poll_forever()
