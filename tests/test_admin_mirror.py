"""
בדיקה ידנית (לא pytest) למתג שיקוף ההודעות לאדמין (2026-09-14).

המראה מעביר תוכן של משתמשים אחרים — טקסט, מיקומים ותמונות של הגוף —
לצ'אט פרטי של המפעיל. הוא נבנה לצפייה בשימוש אמיתי בזמן פיתוח, אבל הוא
לא אמור לרוץ כברירת מחדל כשאנשים אמיתיים משתמשים בבוט. מכאן הבדיקה הזו:
היא נועלת את ברירת המחדל, לא רק את קיום המתג.

התרחיש שהכי חשוב כאן הוא האחרון — הוא מריץ את handle_update דרך
httpx.Client מדומה ולא דרך send_message מדומה. בדיוק הפער הזה הוא מה
שהחביא רגרסיה אמיתית קודם בפרויקט: בדיקות שמדמות את הפונקציה שהן אמורות
לבדוק עוברות גם כשהגוף שלה שבור.
"""
import os

os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import bot_commands as bc

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


ADMIN = 999
USER = 123


class FakeResponse:
    status_code = 200

    def __init__(self, payload=None):
        self._payload = payload or {"ok": True, "result": {}}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeClient:
    """לוכד כל קריאת HTTP יוצאת, כדי שגוף send_message/send_photo באמת ירוץ."""

    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kwargs):
        payload = kwargs.get("json") or kwargs.get("data") or {}
        self.calls.append((url.rsplit("/", 1)[-1], payload))
        return FakeResponse()

    def get(self, url, **kwargs):
        self.calls.append((url.rsplit("/", 1)[-1], kwargs.get("params")))
        return FakeResponse()


# ---------------------------------------------------------------------
# 1) ברירת המחדל: ADMIN_CHAT_ID מוגדר, המתג לא — אין שיקוף
# ---------------------------------------------------------------------
with patch.object(bc, "ADMIN_CHAT_ID", ADMIN), \
     patch.object(bc, "MIRROR_MESSAGES_TO_ADMIN", False):
    check(
        "default: ADMIN_CHAT_ID alone does NOT enable mirroring",
        bc._mirroring_enabled(USER) is False,
    )

sent = []
with patch.object(bc, "ADMIN_CHAT_ID", ADMIN), \
     patch.object(bc, "MIRROR_MESSAGES_TO_ADMIN", False), \
     patch.object(bc, "send_message", lambda cid, *a, **k: sent.append(cid)), \
     patch.object(bc, "send_photo", lambda cid, *a, **k: sent.append(cid)):
    bc._mirror_incoming_to_admin(USER, "someone", "היי", None, None)
    bc._mirror_incoming_to_admin(USER, "someone", "", None, {"latitude": 32.1, "longitude": 34.8})
    bc._mirror_incoming_to_admin(USER, "someone", "", [{"file_id": "x"}], None)
    bc._mirror_outgoing_to_admin(USER, "תשובה")
    bc._mirror_outgoing_photo_to_admin(USER, b"\x89PNG", "כיתוב")
    check("default: nothing is forwarded — text, location or photo", sent == [], f"-> {sent}")

# ---------------------------------------------------------------------
# 2) המתג דלוק -> כן משקף (כדי שהבדיקה למעלה תיכשל אם המראה פשוט נמחק)
# ---------------------------------------------------------------------
sent.clear()
with patch.object(bc, "ADMIN_CHAT_ID", ADMIN), \
     patch.object(bc, "MIRROR_MESSAGES_TO_ADMIN", True), \
     patch.object(bc, "send_message", lambda cid, *a, **k: sent.append(cid)), \
     patch.object(bc, "send_photo", lambda cid, *a, **k: sent.append(cid)):
    bc._mirror_incoming_to_admin(USER, "someone", "היי", None, None)
    bc._mirror_outgoing_to_admin(USER, "תשובה")
    bc._mirror_outgoing_photo_to_admin(USER, b"\x89PNG", "כיתוב")
    check(
        "MIRROR_MESSAGES_TO_ADMIN=1 -> all three mirrors reach the admin chat",
        sent == [ADMIN, ADMIN, ADMIN],
        f"-> {sent}",
    )

# ---------------------------------------------------------------------
# 3) האדמין מדבר עם הבוט בעצמו — לא משקפים לו את עצמו (וזה גם מה שעוצר
#    את הרקורסיה של send_message -> _mirror_outgoing -> send_message)
# ---------------------------------------------------------------------
with patch.object(bc, "ADMIN_CHAT_ID", ADMIN), \
     patch.object(bc, "MIRROR_MESSAGES_TO_ADMIN", True):
    check("admin's own chat is never mirrored back to itself", bc._mirroring_enabled(ADMIN) is False)

# ---------------------------------------------------------------------
# 4) בלי ADMIN_CHAT_ID המתג לבדו לא עושה כלום
# ---------------------------------------------------------------------
with patch.object(bc, "ADMIN_CHAT_ID", None), \
     patch.object(bc, "MIRROR_MESSAGES_TO_ADMIN", True):
    check("no ADMIN_CHAT_ID -> the switch alone does nothing", bc._mirroring_enabled(USER) is False)

# ---------------------------------------------------------------------
# 5) end-to-end: הודעה אמיתית דרך handle_update, עם httpx מדומה בלבד.
#    אף sendMessage לא יוצא לצ'אט האדמין — לא בכיוון הנכנס ולא ביוצא.
# ---------------------------------------------------------------------
http_calls = []
update = {
    "message": {
        "chat": {"id": USER},
        "from": {"username": "someone"},
        "text": "/end_session",
    }
}

with patch.object(bc, "ADMIN_CHAT_ID", ADMIN), \
     patch.object(bc, "MIRROR_MESSAGES_TO_ADMIN", False), \
     patch.object(bc.httpx, "Client", lambda *a, **k: FakeClient(http_calls)), \
     patch.object(bc, "select_rows", lambda *a, **k: []), \
     patch.object(bc, "update_rows", lambda *a, **k: None), \
     patch.object(bc, "upsert_row", lambda *a, **k: None):
    bc.handle_update(update)

recipients = [
    str(payload.get("chat_id"))
    for method, payload in http_calls
    if method.startswith("sendMessage") and isinstance(payload, dict)
]
check(
    "end-to-end: no Telegram message is addressed to the admin chat",
    str(ADMIN) not in recipients,
    f"-> recipients={recipients}",
)
check(
    "end-to-end: the real user still gets their reply",
    str(USER) in recipients,
    f"-> recipients={recipients}",
)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {FAILURES}")
    raise SystemExit(1)
print("All checks passed.")
