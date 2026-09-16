"""
בדיקה ידנית (לא pytest) לתכונת /diagnose_skin — הערכת נזק-שמש מתמונה
אחרי חשיפה. מדמים send_message/select_rows/insert_row/update_rows +
download_telegram_photo/classify_skin_damage_from_image/
validate_damage_classification — לא נוגעים ברשת/DB/Gemini אמיתיים.

מכסה: הגדרת דגל ה-pending ע"י /diagnose_skin; ניתוב תמונה שמגיעה בתוך
חלון ה-TTL ל-handle_skin_damage_photo (לא לסווג-סוג-עור הקיים); נפילה
חזרה לברירת המחדל (סווג-סוג-עור) כשאין דגל/הדגל פג; כתיבת שורה נכונה
ל-skin_damage_log כולל session_id; אזהרת "פנו לרופא" ב-moderate/severe;
וכשל/דחייה של הסיווג -> הודעת נפילה, בלי כתיבה ל-DB.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

# tests/ נמצא רמה אחת מתחת לשורש הריפו — מוסיפים את שורש הריפו ל-sys.path
# כדי ש-import bot_commands (ומודולים אחיים אחרים) ימשיך לעבוד גם כשמריצים
# מ-tests/ ולא משורש הריפו.
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import bot_commands as bc
import skin_damage_classifier

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


sent_messages = []
inserted_rows = []


def fake_send_message(chat_id, text, reply_markup=None):
    sent_messages.append(text)


def fake_insert_row(table, row):
    inserted_rows.append((table, row))
    return row


def fake_update_rows(table, params, patch_fields):
    return None


def fake_download_telegram_photo(client, file_id):
    return b"fake-jpeg-bytes"


# ---------------------------------------------------------------------
# 1) COMMAND_HANDLERS: /diagnose_skin רשום נכון
# ---------------------------------------------------------------------
check("COMMAND_HANDLERS: /diagnose_skin registered", bc.COMMAND_HANDLERS.get("/diagnose_skin") is bc.handle_diagnose_skin)


# ---------------------------------------------------------------------
# 2) handle_diagnose_skin — מגדיר את דגל ה-pending נכון + שולח הסבר
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message):
    bc._pending_diagnose_skin.clear()
    sent_messages.clear()

    before = datetime.now(timezone.utc)
    bc.handle_diagnose_skin(123, "gil612", "")
    after = datetime.now(timezone.utc)

    check("diagnose_skin: pending flag set for user", "gil612" in bc._pending_diagnose_skin)
    if "gil612" in bc._pending_diagnose_skin:
        expires_at = bc._pending_diagnose_skin["gil612"]
        expected_min = before + timedelta(minutes=bc._PENDING_DIAGNOSE_SKIN_TTL_MINUTES)
        expected_max = after + timedelta(minutes=bc._PENDING_DIAGNOSE_SKIN_TTL_MINUTES)
        check(
            "diagnose_skin: pending flag TTL ~10 minutes",
            expected_min <= expires_at <= expected_max,
            f"-> {expires_at}",
        )
    check(
        "diagnose_skin: prompt mentions this isn't a medical diagnosis",
        len(sent_messages) == 1 and "לא אבחנה רפואית" in sent_messages[0],
        f"-> {sent_messages}",
    )

bc._pending_diagnose_skin.clear()


# ---------------------------------------------------------------------
# 3) handle_update — ניתוב תמונה: pending בתוך TTL -> handle_skin_damage_photo
# ---------------------------------------------------------------------
def make_photo_update(username="gil612", chat_id=123):
    return {
        "message": {
            "chat": {"id": chat_id},
            "from": {"username": username},
            "photo": [{"file_id": "small"}, {"file_id": "large_id"}],
        }
    }


damage_calls = []
skin_type_calls = []


def fake_handle_skin_damage_photo(chat_id, username, photo_file_id):
    damage_calls.append((chat_id, username, photo_file_id))


def fake_handle_skin_type_photo(chat_id, username, photo_file_id, lang="he"):
    # lang נוסף ב-2026-09-14 (תמיכה דו-לשונית) — handle_update מעביר את
    # שפת ההודעה הנכנסת הלאה לנתיב התמונה.
    skin_type_calls.append((chat_id, username, photo_file_id))


with patch.object(bc, "update_rows", fake_update_rows), \
     patch.object(bc, "handle_skin_damage_photo", fake_handle_skin_damage_photo), \
     patch.object(bc, "handle_skin_type_photo", fake_handle_skin_type_photo):

    # 3a) בתוך TTL -> מנותב לתמונת-נזק, לא לסווג-סוג-עור
    bc._pending_diagnose_skin.clear()
    damage_calls.clear()
    skin_type_calls.clear()
    bc._pending_diagnose_skin["gil612"] = datetime.now(timezone.utc) + timedelta(minutes=5)
    bc.handle_update(make_photo_update())
    check(
        "handle_update: photo within TTL -> routed to skin-damage handler",
        len(damage_calls) == 1 and len(skin_type_calls) == 0,
        f"-> damage={damage_calls} skin_type={skin_type_calls}",
    )
    check(
        "handle_update: photo within TTL -> correct file_id (largest)",
        damage_calls and damage_calls[0][2] == "large_id",
        f"-> {damage_calls}",
    )
    check("handle_update: pending flag consumed (popped) after use", "gil612" not in bc._pending_diagnose_skin)

    # 3b) דגל פג-תוקף -> נופל חזרה לברירת המחדל (סווג-סוג-עור)
    damage_calls.clear()
    skin_type_calls.clear()
    bc._pending_diagnose_skin["gil612"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    bc.handle_update(make_photo_update())
    check(
        "handle_update: expired pending flag -> falls back to skin-type handler",
        len(skin_type_calls) == 1 and len(damage_calls) == 0,
        f"-> damage={damage_calls} skin_type={skin_type_calls}",
    )

    # 3c) אין דגל בכלל (משתמש לא שלח /diagnose_skin) -> ברירת המחדל הרגילה
    damage_calls.clear()
    skin_type_calls.clear()
    bc._pending_diagnose_skin.clear()
    bc.handle_update(make_photo_update())
    check(
        "handle_update: no pending flag at all -> falls back to skin-type handler",
        len(skin_type_calls) == 1 and len(damage_calls) == 0,
        f"-> damage={damage_calls} skin_type={skin_type_calls}",
    )

bc._pending_diagnose_skin.clear()


# ---------------------------------------------------------------------
# 4) handle_skin_damage_photo — סיווג תקין, mild: כתיבה נכונה ל-DB
# ---------------------------------------------------------------------
def fake_select_rows_with_session(table, params):
    assert table == "exposure_log"
    return [{"id": 777}]


def fake_classify_ok_mild(image_bytes, mime_type="image/jpeg"):
    return {"detected": True, "severity": "mild", "confidence": "medium", "reasoning": "אודם קל בזרוע."}


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", fake_select_rows_with_session), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "download_telegram_photo", fake_download_telegram_photo), \
     patch.object(bc, "classify_skin_damage_from_image", fake_classify_ok_mild):

    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_skin_damage_photo(123, "gil612", "photo_abc")

    check("skin_damage_photo: exactly one row inserted", len(inserted_rows) == 1, f"-> {inserted_rows}")
    if inserted_rows:
        table, row = inserted_rows[0]
        check("skin_damage_photo: inserted into skin_damage_log", table == "skin_damage_log", f"-> {table}")
        check("skin_damage_photo: severity stored", row.get("severity") == "mild", f"-> {row}")
        check("skin_damage_photo: confidence stored", row.get("confidence") == "medium", f"-> {row}")
        check("skin_damage_photo: reasoning stored", row.get("reasoning") == "אודם קל בזרוע.", f"-> {row}")
        check("skin_damage_photo: telegram_username stored", row.get("telegram_username") == "gil612", f"-> {row}")
        check("skin_damage_photo: session_id linked to most recent session", row.get("session_id") == 777, f"-> {row}")
    check(
        "skin_damage_photo: mild severity message sent, no doctor-referral needed",
        len(sent_messages) == 1 and "אודם קל" in sent_messages[0],
        f"-> {sent_messages}",
    )
    check(
        "skin_damage_photo: reply always includes non-medical-advice reminder",
        sent_messages and "לא אבחנה רפואית" in sent_messages[0],
        f"-> {sent_messages}",
    )


# ---------------------------------------------------------------------
# 5) חומרה severe -> ההודעה חייבת לכלול המלצה מפורשת לפנות לרופא/מיון
# ---------------------------------------------------------------------
def fake_classify_severe(image_bytes, mime_type="image/jpeg"):
    return {"detected": True, "severity": "severe", "confidence": "high", "reasoning": "אודם עז וסימני שלפוחיות."}


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", fake_select_rows_with_session), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "download_telegram_photo", fake_download_telegram_photo), \
     patch.object(bc, "classify_skin_damage_from_image", fake_classify_severe):

    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_skin_damage_photo(123, "gil612", "photo_severe")

    check(
        "skin_damage_photo: severe severity -> explicit doctor/ER referral in message",
        sent_messages and "לפנות לרופא" in sent_messages[0],
        f"-> {sent_messages}",
    )
    check("skin_damage_photo: severe severity still saved to DB", len(inserted_rows) == 1, f"-> {inserted_rows}")


def fake_classify_moderate(image_bytes, mime_type="image/jpeg"):
    return {"detected": True, "severity": "moderate", "confidence": "medium", "reasoning": "אודם משמעותי נראה לעין."}


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", fake_select_rows_with_session), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "download_telegram_photo", fake_download_telegram_photo), \
     patch.object(bc, "classify_skin_damage_from_image", fake_classify_moderate):

    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_skin_damage_photo(123, "gil612", "photo_moderate")

    check(
        "skin_damage_photo: moderate severity -> explicit doctor/ER referral in message",
        sent_messages and "לפנות לרופא" in sent_messages[0],
        f"-> {sent_messages}",
    )


# ---------------------------------------------------------------------
# 6) סיווג נדחה (detected=false, לא זוהה עור) -> הודעת נפילה, בלי DB
# ---------------------------------------------------------------------
def fake_classify_not_detected(image_bytes, mime_type="image/jpeg"):
    return {"detected": False, "reasoning": "לא ניתן לזהות עור אנושי בבירור בתמונה."}


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", fake_select_rows_with_session), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "download_telegram_photo", fake_download_telegram_photo), \
     patch.object(bc, "classify_skin_damage_from_image", fake_classify_not_detected):

    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_skin_damage_photo(123, "gil612", "photo_bad")

    check(
        "skin_damage_photo: rejected classification -> no DB write",
        len(inserted_rows) == 0,
        f"-> {inserted_rows}",
    )
    check(
        "skin_damage_photo: rejected classification -> fallback message sent",
        len(sent_messages) == 1 and "לא הצלחתי להעריך" in sent_messages[0],
        f"-> {sent_messages}",
    )


# ---------------------------------------------------------------------
# 7) חריגה מ-classify_skin_damage_from_image (למשל כשל רשת/Gemini)
#    -> הודעת נפילה ידידותית, בלי DB, בלי exception שדולפת החוצה
# ---------------------------------------------------------------------
def fake_classify_raises(image_bytes, mime_type="image/jpeg"):
    raise RuntimeError("Gemini timeout")


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", fake_select_rows_with_session), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "download_telegram_photo", fake_download_telegram_photo), \
     patch.object(bc, "classify_skin_damage_from_image", fake_classify_raises):

    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_skin_damage_photo(123, "gil612", "photo_err")

    check("skin_damage_photo: classifier exception -> no DB write", len(inserted_rows) == 0, f"-> {inserted_rows}")
    check(
        "skin_damage_photo: classifier exception -> friendly fallback message",
        len(sent_messages) == 1 and "לא הצלחתי לנתח" in sent_messages[0],
        f"-> {sent_messages}",
    )


# ---------------------------------------------------------------------
# 8) אין session פתוח/סגור בכלל למשתמש -> session_id=None, לא קורס
# ---------------------------------------------------------------------
def fake_select_rows_no_session(table, params):
    assert table == "exposure_log"
    return []


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", fake_select_rows_no_session), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "download_telegram_photo", fake_download_telegram_photo), \
     patch.object(bc, "classify_skin_damage_from_image", fake_classify_ok_mild):

    sent_messages.clear()
    inserted_rows.clear()
    bc.handle_skin_damage_photo(123, "gil612", "photo_no_session")

    check("skin_damage_photo: no session found -> still inserts with session_id=None", len(inserted_rows) == 1, f"-> {inserted_rows}")
    if inserted_rows:
        check("skin_damage_photo: session_id is None when no session exists", inserted_rows[0][1].get("session_id") is None, f"-> {inserted_rows[0][1]}")


# ---------------------------------------------------------------------
# 9) דיווח טוקנים ל-admin — _extract_usage (skin_damage_classifier.py)
#    + _notify_admin_token_usage (bot_commands.py) + אינטגרציה מלאה
# ---------------------------------------------------------------------

# 9a) _extract_usage: usage_metadata תקין -> dict עם 3 המפתחות הנכונים
fake_response_with_usage = SimpleNamespace(
    usage_metadata=SimpleNamespace(prompt_token_count=1024, candidates_token_count=42, total_token_count=1066)
)
usage = skin_damage_classifier._extract_usage(fake_response_with_usage)
check(
    "_extract_usage: valid usage_metadata -> correct dict",
    usage == {"prompt_tokens": 1024, "output_tokens": 42, "total_tokens": 1066},
    f"-> {usage}",
)

# 9b) _extract_usage: אין usage_metadata בכלל -> None, לא קורס
fake_response_no_usage = SimpleNamespace(usage_metadata=None)
check("_extract_usage: missing usage_metadata -> None", skin_damage_classifier._extract_usage(fake_response_no_usage) is None)

# 9c-9f) _notify_admin_token_usage — ישירות, בלי לעבור דרך handle_skin_damage_photo
admin_sent = []


def fake_send_message_capture(chat_id, text, reply_markup=None):
    admin_sent.append((chat_id, text))


def fake_send_message_raises(chat_id, text, reply_markup=None):
    raise RuntimeError("Telegram API down")


with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "ADMIN_CHAT_ID", None):
    admin_sent.clear()
    bc._notify_admin_token_usage("skin_type", "gil612", {"prompt_tokens": 100, "output_tokens": 10, "total_tokens": 110})
    check("_notify_admin_token_usage: ADMIN_CHAT_ID unset -> no send", len(admin_sent) == 0, f"-> {admin_sent}")

with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"):
    admin_sent.clear()
    bc._notify_admin_token_usage("skin_type", "gil612", None)
    check("_notify_admin_token_usage: usage=None -> no send even with ADMIN_CHAT_ID set", len(admin_sent) == 0, f"-> {admin_sent}")

    admin_sent.clear()
    bc._notify_admin_token_usage("diagnose_skin", "gil612", {"prompt_tokens": 1024, "output_tokens": 42, "total_tokens": 1066})
    check("_notify_admin_token_usage: sends exactly one message when usage present", len(admin_sent) == 1, f"-> {admin_sent}")
    if admin_sent:
        chat_id, text = admin_sent[0]
        check("_notify_admin_token_usage: sent to ADMIN_CHAT_ID, not the user's chat", chat_id == "999999", f"-> {chat_id}")
        check("_notify_admin_token_usage: message contains feature name + username + token counts", "diagnose_skin" in text and "gil612" in text and "1024" in text and "1066" in text, f"-> {text}")

with patch.object(bc, "send_message", fake_send_message_raises), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"):
    try:
        bc._notify_admin_token_usage("skin_type", "gil612", {"prompt_tokens": 1, "output_tokens": 1, "total_tokens": 2})
        raised = False
    except Exception:
        raised = True
    check("_notify_admin_token_usage: send_message failure does not propagate", raised is False)

# 9g) אינטגרציה מלאה: handle_skin_damage_photo, ADMIN_CHAT_ID מוגדר,
#     הסיווג מחזיר "_usage" -> גם המשתמש וגם ה-admin מקבלים הודעה,
#     ו-"_usage" לא דולף לשורת ה-DB.
def fake_classify_with_usage(image_bytes, mime_type="image/jpeg"):
    return {
        "detected": True,
        "severity": "mild",
        "confidence": "medium",
        "reasoning": "אודם קל.",
        "_usage": {"prompt_tokens": 1030, "output_tokens": 40, "total_tokens": 1070},
    }


with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"), \
     patch.object(bc, "select_rows", fake_select_rows_with_session), \
     patch.object(bc, "insert_row", fake_insert_row), \
     patch.object(bc, "download_telegram_photo", fake_download_telegram_photo), \
     patch.object(bc, "classify_skin_damage_from_image", fake_classify_with_usage):

    admin_sent.clear()
    inserted_rows.clear()
    bc.handle_skin_damage_photo(123, "gil612", "photo_with_usage")

    check("integration: exactly 2 messages sent (user reply + admin token report)", len(admin_sent) == 2, f"-> {admin_sent}")
    user_msgs = [t for c, t in admin_sent if c == 123]
    admin_msgs = [t for c, t in admin_sent if c == "999999"]
    check("integration: one message went to the user's own chat_id", len(user_msgs) == 1, f"-> {admin_sent}")
    check("integration: one message went to ADMIN_CHAT_ID with token counts", len(admin_msgs) == 1 and "1070" in admin_msgs[0], f"-> {admin_sent}")
    check(
        "integration: '_usage' key does not leak into the DB row",
        inserted_rows and "_usage" not in inserted_rows[0][1],
        f"-> {inserted_rows}",
    )

# 9h) אינטגרציה: handle_skin_type_photo — אותו עיקרון, לזרימה המקבילה
def fake_classify_skin_type_with_usage(image_bytes, mime_type="image/jpeg"):
    return {
        "detected": True,
        "skin_type": 3,
        "confidence": "medium",
        "reasoning": "גוון עור בינוני.",
        "_usage": {"prompt_tokens": 1010, "output_tokens": 30, "total_tokens": 1040},
    }


with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"), \
     patch.object(bc, "download_telegram_photo", fake_download_telegram_photo), \
     patch.object(bc, "classify_skin_type_from_image", fake_classify_skin_type_with_usage):

    admin_sent.clear()
    bc.handle_skin_type_photo(123, "gil612", "photo_skin_type_usage")

    check("integration (skin_type): exactly 2 messages sent (user reply + admin token report)", len(admin_sent) == 2, f"-> {admin_sent}")
    admin_msgs = [t for c, t in admin_sent if c == "999999"]
    check(
        "integration (skin_type): admin message carries the right feature label + token counts",
        len(admin_msgs) == 1 and "skin_type" in admin_msgs[0] and "1040" in admin_msgs[0],
        f"-> {admin_sent}",
    )


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
else:
    print("All checks passed.")
