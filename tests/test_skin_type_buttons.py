"""
בדיקה ידנית (לא pytest) לבורר סוג-העור בלחיצה ולקיצור ה-/start
(2026-09-12) — handle_callback_query, _skin_type_keyboard,
_save_skin_type ו-handle_start ב-bot_commands.py.

מדמים את כל קריאות טלגרם (send_message/send_photo/answer_callback_query)
ואת ה-DB (upsert_row) — לא נוגעים ברשת אמיתית.

תרחישי המפתח:
1. /start שולח שתי הודעות בלבד (ולא שלוש), עם הכפתורים מתחת לתמונה.
2. לחיצה על כפתור שומרת את סוג העור ועונה ל-callback (סוגרת את הספינר).
3. callback_data לא מוכר / מחוץ לטווח / עדכון פגום — נכשל בבטחה בלי
   לכתוב ל-DB, ותמיד סוגר את הספינר כשיש query id.
4. handle_update מנתב callback_query לפני כל שאר הלוגיקה.
5. גיבוי הספרה הבודדת ("3") ממשיך לעבוד, כדי שלקוח ישן לא יישאר תקוע.
6. אישור סוג העור מציע את הצעד הבא כלחיצה (שיתוף מיקום), לא כפקודה.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

# tests/ נמצא רמה אחת מתחת לשורש הריפו — מוסיפים את שורש הריפו ל-sys.path
# כדי ש-import bot_commands (ומודולים אחיים אחרים) ימשיך לעבוד גם כשמריצים
# מ-tests/ ולא משורש הריפו.
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


def make_callback(data, username="gil612", chat_id=123, query_id="q1"):
    return {
        "id": query_id,
        "data": data,
        "from": {"username": username},
        "message": {"chat": {"id": chat_id}},
    }


# ---------------------------------------------------------------------
# 1) _skin_type_keyboard
# ---------------------------------------------------------------------
kb = bc._skin_type_keyboard()
rows = kb["inline_keyboard"]
all_buttons = [b for row in rows for b in row]
# שש כפתורי הסוגים עצמם, בנפרד מכפתור "שלחו תמונה" שנוסף מתחתיהם.
flat = [b for b in all_buttons if b["callback_data"].removeprefix("skin:").isdigit()]
check("keyboard has all six skin types", len(flat) == 6, f"-> {len(flat)}")
check("the six types are laid out two per row", [len(r) for r in rows[:3]] == [2, 2, 2])
check(
    "every type button carries a prefixed, in-range callback_data",
    [b["callback_data"] for b in flat] == [f"skin:{n}" for n in range(1, 7)],
    f"-> {[b['callback_data'] for b in flat]}",
)
check(
    "every callback_data fits Telegram's 64-byte limit",
    all(len(b["callback_data"].encode()) <= 64 for b in all_buttons),
)
check(
    "labels pair a plain digit with a Hebrew description",
    [b["text"].split(" · ")[0] for b in flat] == ["1", "2", "3", "4", "5", "6"]
    and all(len(b["text"].split(" · ")) == 2 and b["text"].split(" · ")[1] for b in flat),
    f"-> {[b['text'] for b in flat]}",
)
# ספרות רומיות הוסרו במכוון (2026-09-12) — הן לא מובנות לכל משתמש.
check(
    "no roman numerals anywhere in the labels",
    not any(
        part in b["text"].split(" · ")[0]
        for b in flat
        for part in ("I", "V", "X")
    ),
    f"-> {[b['text'] for b in flat]}",
)

# ---------------------------------------------------------------------
# 2) /start — שתי הודעות, כפתורים מתחת לתמונה
# ---------------------------------------------------------------------
messages = []
photos = []

with patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append((text, reply_markup))), \
     patch.object(bc, "send_photo", lambda cid, img, caption=None, reply_markup=None: photos.append((caption, reply_markup))), \
     patch.object(bc, "_load_fitzpatrick_scale_image", lambda: b"fake-png"):
    bc._pending_skin_type_pick.clear()
    bc.handle_start(123, "gil612", "")

check("/start sends exactly one text message now (was two)", len(messages) == 1, f"-> {len(messages)}")
check("/start sends the scale image", len(photos) == 1)
check("the buttons ride along with the image", photos and photos[0][1] == kb)
check(
    "/start no longer lists the commands",
    not any("/end_session" in text or "/my_sessions" in text for text, _ in messages),
    f"-> {[t[:40] for t, _ in messages]}",
)
check("/start still arms the single-digit fallback", "gil612" in bc._pending_skin_type_pick)

# בלי תמונה (assets חסר) — הכפתורים עדיין נשלחים
messages.clear()
with patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append((text, reply_markup))), \
     patch.object(bc, "_load_fitzpatrick_scale_image", lambda: None):
    bc.handle_start(123, "gil612", "")
check(
    "with no image available the buttons are still offered",
    any(markup == kb for _, markup in messages),
)

# ---------------------------------------------------------------------
# 3) לחיצה על כפתור -> שמירה + סגירת הספינר
# ---------------------------------------------------------------------
upserts = []
answered = []
messages.clear()

with patch.object(bc, "upsert_row", lambda table, row, on_conflict=None: upserts.append((table, row))), \
     patch.object(bc, "answer_callback_query", lambda qid, text=None: answered.append((qid, text))), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append((text, reply_markup))):
    bc._pending_skin_type_pick.clear()
    bc._mark_pending_skin_type_pick("gil612")
    bc.handle_callback_query(make_callback("skin:4"))

check("a tap writes the skin type to the DB", len(upserts) == 1 and upserts[0][0] == "users")
check("it stores the right value", upserts and upserts[0][1]["skin_type"] == 4, f"-> {upserts}")
check("it stores chat_id too (first possible insert point)", upserts and upserts[0][1]["chat_id"] == 123)
check("the callback spinner is answered", len(answered) == 1 and answered[0][0] == "q1")
check("the tap consumes the pending-digit flag", "gil612" not in bc._pending_skin_type_pick)

# ---------------------------------------------------------------------
# 4) האישור מציע את הצעד הבא כלחיצה
# ---------------------------------------------------------------------
check("a confirmation is sent", len(messages) == 1)
confirm_text, confirm_markup = messages[0] if messages else ("", None)
check("the confirmation names the chosen type verbally", "זית" in confirm_text, f"-> {confirm_text[:60]}")
check(
    "the next step is offered as a location-share button, not a command",
    bool(confirm_markup)
    and confirm_markup.get("keyboard", [[{}]])[0][0].get("request_location") is True,
    f"-> {confirm_markup}",
)
# עד 2026-09-14 הבדיקה הזו דרשה שהאישור *לא* יזכיר פקודה בכלל. זה היה
# נכון כשהמטרה הייתה אפס הקלדה — אבל השאיר בלי שום דרך את מי שאין לו
# GPS או שסירב להרשאת המיקום. הכוונה המדויקת יותר: הכפתור הוא המסלול
# הראשי, והפקודה מופיעה רק כגיבוי מסומן.
check(
    "the command is offered only as a labelled no-GPS fallback",
    "אין GPS" in confirm_text and confirm_text.index("אין GPS") < confirm_text.index("/start_session"),
    f"-> ...{confirm_text[-60:]}",
)
check(
    "the primary path is still the tap, not the command",
    confirm_text.index("לחצו למטה") < confirm_text.index("/start_session"),
)

# ---------------------------------------------------------------------
# 5) קלט לא אמין ב-callback_data
# ---------------------------------------------------------------------
for label, data in [
    ("unknown prefix", "delete_everything:1"),
    ("out of range (high)", "skin:9"),
    ("out of range (zero)", "skin:0"),
    ("not a number", "skin:abc"),
    ("empty", ""),
]:
    upserts.clear()
    answered.clear()
    with patch.object(bc, "upsert_row", lambda *a, **k: upserts.append(a)), \
         patch.object(bc, "answer_callback_query", lambda qid, text=None: answered.append(qid)), \
         patch.object(bc, "send_message", lambda *a, **k: None):
        bc.handle_callback_query(make_callback(data))
    check(f"rejected callback_data — {label}: nothing written to the DB", not upserts)
    check(f"rejected callback_data — {label}: spinner still cleared", len(answered) == 1)

# עדכון פגום (בלי id/chat/username) — לא קורס, לא כותב
upserts.clear()
answered.clear()
with patch.object(bc, "upsert_row", lambda *a, **k: upserts.append(a)), \
     patch.object(bc, "answer_callback_query", lambda qid, text=None: answered.append(qid)):
    bc.handle_callback_query({"data": "skin:3"})           # חסר id/chat/from
    bc.handle_callback_query({"id": "q", "data": "skin:3"})  # חסר chat/from
check("a malformed callback_query is ignored without crashing", not upserts and not answered)

# ---------------------------------------------------------------------
# 6) handle_update מנתב callback_query
# ---------------------------------------------------------------------
routed = []
with patch.object(bc, "handle_callback_query", lambda cq: routed.append(cq)), \
     patch.object(bc, "classify_message") as mock_classify:
    bc.handle_update({"callback_query": make_callback("skin:2")})
check("handle_update routes a callback_query to its handler", len(routed) == 1)
check("a callback_query never reaches the LLM gatekeeper", not mock_classify.called)

# ---------------------------------------------------------------------
# 7) גיבוי הספרה הבודדת עדיין עובד
# ---------------------------------------------------------------------
upserts.clear()
with patch.object(bc, "upsert_row", lambda table, row, on_conflict=None: upserts.append(row)), \
     patch.object(bc, "send_message", lambda *a, **k: None), \
     patch.object(bc, "update_rows"), \
     patch.object(bc, "_mirror_incoming_to_admin", lambda *a, **k: None), \
     patch.object(bc, "classify_message") as mock_classify:
    bc._pending_skin_type_pick.clear()
    bc._mark_pending_skin_type_pick("gil612")
    bc.handle_update({
        "message": {"chat": {"id": 123}, "from": {"username": "gil612"}, "text": "3"},
    })
check("typing a single digit still sets the skin type", upserts and upserts[0]["skin_type"] == 3, f"-> {upserts}")
check("the digit fallback still skips the gatekeeper", not mock_classify.called)

# ---------------------------------------------------------------------
# 7b) "לא בטוחים? שלחו תמונה של היד" + אישור ההצעה בלחיצה
# ---------------------------------------------------------------------
# נוסף 2026-09-12: סיווג סוג עור מתמונה קיים בקוד מאז 2026-08-26, אבל
# שום דבר בממשק לא רמז שהוא קיים, והאישור שלו דרש להקליד
# "/set_skin_type 3" — דווקא ממשתמש שהגיע לשם כי הוא *לא* בטוח.
photo_button = [b for row in rows for b in row if b["callback_data"] == "skin:photo"]
check("the picker offers a hand-photo option", len(photo_button) == 1,
      f"-> {photo_button[0]['text'] if photo_button else None}")
check("the photo button sits on its own full-width row",
      any(len(r) == 1 and r[0]["callback_data"] == "skin:photo" for r in rows))

upserts.clear()
answered.clear()
messages.clear()
with patch.object(bc, "upsert_row", lambda *a, **k: upserts.append(a)), \
     patch.object(bc, "answer_callback_query", lambda qid, text=None: answered.append(qid)), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append((text, reply_markup))):
    bc.handle_callback_query(make_callback("skin:photo"))

check("tapping it explains how to take the photo", len(messages) == 1)
check("the instructions mention natural light and the un-tanned skin",
      messages and "אור יום" in messages[0][0] and "משוזף" in messages[0][0],
      f"-> {messages[0][0][:60] if messages else None}")
check("asking for a photo writes nothing to the DB", not upserts)
check("the spinner is cleared for the photo button too", len(answered) == 1)

# הצעה מתמונה -> כפתורי אישור
messages.clear()
with patch.object(bc, "download_telegram_photo", lambda client, fid: b"img"), \
     patch.object(bc, "classify_skin_type_from_image", lambda b: {"skin_type": 3, "confidence": "medium", "reasoning": "גוון בינוני"}), \
     patch.object(bc, "validate_classification", lambda raw: {"ok": True, **raw}), \
     patch.object(bc, "_notify_admin_token_usage", lambda *a: None), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append((text, reply_markup))):
    # httpx.Client() כן נוצר כאן, אבל download_telegram_photo ממוק
    # אז אין שום קריאת רשת בפועל.
    bc.handle_skin_type_photo(123, "gil612", "file-id")

check("a photo suggestion is sent", len(messages) == 1)
sug_text, sug_markup = messages[0] if messages else ("", None)
check("the suggestion no longer asks the user to type a command",
      "/set_skin_type" not in sug_text, f"-> {sug_text[:80]}")
check("it still says the estimate is approximate, not a diagnosis",
      "משוערת" in sug_text)
suggest_buttons = [b for row in (sug_markup or {}).get("inline_keyboard", []) for b in row]
check("confirming the suggestion is one tap on the suggested value",
      any(b["callback_data"] == "skin:3" for b in suggest_buttons),
      f"-> {[b['callback_data'] for b in suggest_buttons]}")
check("there is also a way back to the full picker",
      any(b["callback_data"] == "skin:again" for b in suggest_buttons))

# "בחירה אחרת" -> הבורר המלא חוזר
messages.clear()
answered.clear()
with patch.object(bc, "answer_callback_query", lambda qid, text=None: answered.append(qid)), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append((text, reply_markup))):
    bc.handle_callback_query(make_callback("skin:again"))
check("'choose another' re-sends the full picker", messages and messages[0][1] == kb)

# שערי "צריך סוג עור קודם" מציעים כפתורים ולא פקודה
messages.clear()
with patch.object(bc, "select_rows", lambda *a, **k: []), \
     patch.object(bc, "send_message", lambda cid, text, reply_markup=None: messages.append((text, reply_markup))):
    allowed = bc._can_start_session(123, "newuser")
check("the 'set a skin type first' gate blocks as before", allowed is False)
check("...but offers the picker instead of a command to type",
      messages and messages[0][1] == kb and "/set_skin_type" not in messages[0][0],
      f"-> {messages[0][0] if messages else None}")


# ---------------------------------------------------------------------
# 8) הפונקציות עצמן, בלי למוק אותן — answer_callback_query / send_photo
# ---------------------------------------------------------------------
# זה החלק שהיה חסר ותפס באג אמיתי בפרודקשן (2026-09-12): כל הבדיקות
# למעלה ממוקות את answer_callback_query עצמה, כך שהגוף שלה לא רץ בהן
# אף פעם. בפועל נשארה שם שורה יתומה מ-send_photo
# (_mirror_outgoing_photo_to_admin(chat_id, ...)) שהפילה כל לחיצה על
# כפתור ב-NameError — ו-35 בדיקות עברו בשלום בזמן שהתכונה שבורה לגמרי.
# מכאן והלאה: ממקים את *שכבת ה-HTTP* (httpx.Client), לא את הפונקציות.


class _FakeResponse:
    def __init__(self):
        self.status_calls = 0

    def raise_for_status(self):
        self.status_calls += 1


class _FakeClient:
    """httpx.Client מדומה שמתעד את הקריאות במקום לצאת לרשת."""

    calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        _FakeClient.calls.append((url, kwargs))
        return _FakeResponse()


_FakeClient.calls = []
with patch.object(bc.httpx, "Client", _FakeClient):
    try:
        bc.answer_callback_query("q42", "סוג עור 3")
        check("answer_callback_query runs for real without raising", True)
    except Exception as e:
        check("answer_callback_query runs for real without raising", False, f"-> {e!r}")

calls = _FakeClient.calls
check("answer_callback_query posts to answerCallbackQuery",
      len(calls) == 1 and calls[0][0].endswith("/answerCallbackQuery"),
      f"-> {[c[0].rsplit('/', 1)[-1] for c in calls]}")
check("it sends the callback id and the toast text",
      calls and calls[0][1]["json"].get("callback_query_id") == "q42"
      and calls[0][1]["json"].get("text") == "סוג עור 3",
      f"-> {calls[0][1].get('json') if calls else None}")

# בלי text — לא נשלח שדה text ריק
_FakeClient.calls = []
with patch.object(bc.httpx, "Client", _FakeClient):
    bc.answer_callback_query("q43")
check("with no toast text, no empty text field is sent",
      "text" not in _FakeClient.calls[0][1]["json"])

# send_photo — גם הוא רץ באמת, וגם המראה לאדמין חוזר לעבוד
_FakeClient.calls = []
mirrored = []
with patch.object(bc.httpx, "Client", _FakeClient), \
     patch.object(bc, "_mirror_outgoing_photo_to_admin", lambda *a: mirrored.append(a)):
    bc.send_photo(123, b"png-bytes", caption="כיתוב", reply_markup=kb)

photo_calls = [c for c in _FakeClient.calls if c[0].endswith("/sendPhoto")]
check("send_photo runs for real and posts to sendPhoto", len(photo_calls) == 1)
check("the caption is sent", photo_calls and photo_calls[0][1]["data"].get("caption") == "כיתוב")
check(
    "reply_markup is serialized as a JSON string (multipart requirement)",
    photo_calls and isinstance(photo_calls[0][1]["data"].get("reply_markup"), str),
    f"-> {type(photo_calls[0][1]['data'].get('reply_markup')).__name__ if photo_calls else None}",
)
# זה מה שנשבר בשקט כשהשורה היתומה עברה מקום — send_photo הפסיק למרכז
# לאדמין את כל התמונות (תמונת הסולם ב-/start, גרפי ה-UV).
check("send_photo still mirrors the photo to the admin chat", len(mirrored) == 1, f"-> {mirrored and mirrored[0][0]}")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
print("All checks passed.")
