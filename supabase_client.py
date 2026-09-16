
import os
import logging
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("sunsafe.supabase")
logging.basicConfig(level=logging.INFO)

# קודי סטטוס שמעידים על תקלה רגעית בצד השרת/השער, לא על בקשה שגויה —
# ראו ההערה ב-select_rows. 5xx בלבד; 4xx לעולם לא יסתדר בניסיון חוזר.
_TRANSIENT_STATUS = {500, 502, 503, 504}
_READ_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 0.5


class SupabaseError(Exception):
    """Raised when a call to the Supabase REST API fails."""


@dataclass
class SupabaseConfig:
    url: str
    service_role_key: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "SupabaseConfig":
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL ו/או SUPABASE_SERVICE_ROLE_KEY לא מוגדרים. הוסיפו אותם ל-.env."
            )
        return cls(url=url, service_role_key=key)


def insert_row(table: str, row: dict, config: "SupabaseConfig | None" = None) -> dict:
    """
    Insert one row, e.g. insert_row("exposure_log", {...}).
    Returns the inserted row as PostgREST returns it.

    16.9.2026: עד לתאריך הזה זו הייתה הפונקציה היחידה בקובץ עם
    raise_for_status() חשוף, בלי לוג ובלי SupabaseError — כלומר נתיב
    הכתיבה שפותח sessions היה היחיד שלא יכול היה להגיד *למה* הוא נכשל.
    בפרודקשן זה נראה כך:

        httpx.HTTPStatusError: Client error '400 Bad Request' for url
        '.../rest/v1/exposure_log'

    ו-400 מ-PostgREST *תמיד* מסביר את עצמו בגוף התשובה ({"code","message",
    "details","hint"}) — עמודה שלא קיימת, NOT NULL, הפרת check. הגוף הזה
    נזרק לפח, והמשתמש קיבל שתיקה. עכשיו זה מתנהג כמו כל השאר בקובץ.
    """
    config = config or SupabaseConfig.from_env()
    url = f"{config.url.rstrip('/')}/rest/v1/{table}"
    headers = {
        "apikey": config.service_role_key,
        "Authorization": f"Bearer {config.service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        response = httpx.post(url, headers=headers, json=row, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        # שמות העמודות נכנסים ללוג, הערכים לא: השורה יכולה להכיל מיקום
        # מדויק של משתמש, וללוגים של ה-Space יש גישה רחבה יותר מל-DB.
        logger.error(
            "Supabase insert into %s failed (%s) with columns %s: %s",
            table, e.response.status_code, sorted(row), e.response.text,
        )
        raise SupabaseError(f"כתיבת שורה ל-{table} נכשלה: {e.response.text}") from e
    except httpx.RequestError as e:
        logger.error("Supabase request to %s failed: %s", table, e)
        raise SupabaseError(f"בקשת רשת ל-Supabase נכשלה: {e}") from e

    inserted = response.json()
    return inserted[0] if isinstance(inserted, list) else inserted


def select_rows(table: str, params: dict, config: "SupabaseConfig | None" = None) -> list:
    """
    Select rows from a Supabase table via PostgREST filters, e.g.
    select_rows("exposure_log", {"telegram_username": "eq.gil612",
                                  "end_time": "is.null"})
    Returns a list of matching rows (empty list if none match).
    """
    config = config or SupabaseConfig.from_env()
    url = f"{config.url.rstrip('/')}/rest/v1/{table}"
    headers = {
        "apikey": config.service_role_key,
        "Authorization": f"Bearer {config.service_role_key}",
    }
    # ניסיונות חוזרים על כשל חולף (נוסף 2026-09-13). המקרה שהוביל לזה:
    # משתמש שיתף מיקום, Supabase החזיר 504 Gateway Timeout בודד על
    # שליפת users, ופתיחת ה-session נכשלה לגמרי. 502/503/504 ותקלות
    # רשת הם כמעט תמיד רגעיים, ושתי המתנות קצרות פותרות את הרוב.
    #
    # *רק קריאה* מקבלת retry, ובכוונה: select אידמפוטנטית לחלוטין.
    # כתיבות (insert בפרט) לא חוזרות כאן — 504 אומר "השער התייאש",
    # לא "הכתיבה לא קרתה", וניסיון שני עלול ליצור שורה כפולה.
    last_error: Exception | None = None
    for attempt in range(_READ_RETRIES + 1):
        is_last = attempt == _READ_RETRIES
        try:
            response = httpx.get(url, headers=headers, params=params, timeout=10.0)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            last_error = e
            if e.response.status_code not in _TRANSIENT_STATUS or is_last:
                logger.error(
                    "Supabase select from %s failed (%s): %s",
                    table, e.response.status_code, e.response.text,
                )
                raise SupabaseError(f"שליפת נתונים מ-{table} נכשלה: {e.response.text}") from e
        except httpx.RequestError as e:
            last_error = e
            if is_last:
                logger.error("Supabase request to %s failed: %s", table, e)
                raise SupabaseError(f"בקשת רשת ל-Supabase נכשלה: {e}") from e

        logger.warning(
            "Supabase select from %s failed transiently (attempt %s/%s): %s — retrying",
            table, attempt + 1, _READ_RETRIES + 1, last_error,
        )
        time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))

    # לא אמור להגיע לכאן (הלולאה תמיד מחזירה או זורקת), אבל לא משאירים
    # נתיב שקט שמחזיר None למי שמצפה לרשימה.
    raise SupabaseError(f"שליפת נתונים מ-{table} נכשלה: {last_error}")


def update_rows(table: str, params: dict, patch: dict, config: "SupabaseConfig | None" = None) -> list:
    """
    Update rows matching PostgREST filters with the given patch dict,
    e.g. update_rows("exposure_log", {"id": "eq.42"},
                      {"end_time": "...", "exposure_score": 90})
    Returns the updated rows (as PostgREST returns them).
    """
    config = config or SupabaseConfig.from_env()
    url = f"{config.url.rstrip('/')}/rest/v1/{table}"
    headers = {
        "apikey": config.service_role_key,
        "Authorization": f"Bearer {config.service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        response = httpx.patch(url, headers=headers, params=params, json=patch, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error("Supabase update on %s failed (%s): %s", table, e.response.status_code, e.response.text)
        raise SupabaseError(f"עדכון שורה ב-{table} נכשל: {e.response.text}") from e
    except httpx.RequestError as e:
        logger.error("Supabase request to %s failed: %s", table, e)
        raise SupabaseError(f"בקשת רשת ל-Supabase נכשלה: {e}") from e


def delete_rows(table: str, params: dict, config: "SupabaseConfig | None" = None) -> list:
    """
    Delete rows matching PostgREST filters, e.g.
    delete_rows("exposure_log", {"id": "eq.42"}).
    Returns the deleted rows (as PostgREST returns them) — an empty list
    means nothing matched the filter (not an error).
    """
    config = config or SupabaseConfig.from_env()
    url = f"{config.url.rstrip('/')}/rest/v1/{table}"
    headers = {
        "apikey": config.service_role_key,
        "Authorization": f"Bearer {config.service_role_key}",
        "Prefer": "return=representation",
    }
    try:
        response = httpx.delete(url, headers=headers, params=params, timeout=10.0)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error("Supabase delete from %s failed (%s): %s", table, e.response.status_code, e.response.text)
        raise SupabaseError(f"מחיקת שורה מ-{table} נכשלה: {e.response.text}") from e
    except httpx.RequestError as e:
        logger.error("Supabase request to %s failed: %s", table, e)
        raise SupabaseError(f"בקשת רשת ל-Supabase נכשלה: {e}") from e


def upsert_row(table: str, row: dict, on_conflict: str, config: "SupabaseConfig | None" = None) -> dict:
    """
    Insert a row, or update it in place if a row with the same
    `on_conflict` column already exists (e.g. upsert_row("users",
    {"telegram_username": "gil612", "skin_type": 3}, on_conflict="telegram_username")).
    """
    config = config or SupabaseConfig.from_env()
    url = f"{config.url.rstrip('/')}/rest/v1/{table}"
    headers = {
        "apikey": config.service_role_key,
        "Authorization": f"Bearer {config.service_role_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }
    try:
        response = httpx.post(
            url, headers=headers, params={"on_conflict": on_conflict}, json=row, timeout=10.0
        )
        response.raise_for_status()
        result = response.json()
        return result[0] if isinstance(result, list) else result
    except httpx.HTTPStatusError as e:
        logger.error("Supabase upsert into %s failed (%s): %s", table, e.response.status_code, e.response.text)
        raise SupabaseError(f"שמירת שורה ב-{table} נכשלה: {e.response.text}") from e
    except httpx.RequestError as e:
        logger.error("Supabase request to %s failed: %s", table, e)
        raise SupabaseError(f"בקשת רשת ל-Supabase נכשלה: {e}") from e
