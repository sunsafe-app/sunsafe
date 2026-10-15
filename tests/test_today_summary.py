"""
בדיקה ידנית (לא pytest) ל-/today — אנליזה יומית + "מה היה קורה עם קרם
הגנה" (השוואה מול DAILY_SUMMARY_REFERENCE_SPF קבוע). מדמים
send_message/select_rows — לא נוגעים ברשת/DB אמיתיים. ראו handle_today
ב-bot_commands.py לרציונל המלא (הוחלט עם המשתמש ב-2026-09-08: פקודה
חדשה על-פי-דרישה, השוואה מול SPF 30 קבוע).
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

# tests/ נמצא רמה אחת מתחת לשורש הריפו — מוסיפים את שורש הריפו ל-sys.path
# כדי ש-import bot_commands (ומודולים אחיים אחרים) ימשיך לעבוד גם כשמריצים
# מ-tests/ ולא משורש הריפו.
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bot_commands as bc

FAILURES = []


def check(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


TODAY = datetime.now(timezone.utc).date()
YESTERDAY = TODAY - timedelta(days=1)


def _session(id_, day, start_hm, end_hm, uv, spf=None, city="תל אביב", exposure_score="auto", lat=None, lon=None):
    """בונה שורת exposure_log מדומה. exposure_score='auto' -> מחושב לפי הנוסחה האמיתית (כמו שהיה קורה ב-/end_session בפועל).
    lat/lon=None כברירת מחדל -> מדמה session ישן/לפני ה-migration (בלי lat/lon בכלל), עקבי עם session.get("lat") ב-bot_commands.py."""
    start_h, start_m = start_hm
    end_h, end_m = end_hm
    start_dt = datetime(day.year, day.month, day.day, start_h, start_m, tzinfo=timezone.utc)
    end_dt = datetime(day.year, day.month, day.day, end_h, end_m, tzinfo=timezone.utc)
    duration = (end_dt - start_dt).total_seconds() / 60
    if exposure_score == "auto":
        exposure_score = bc.calculate_exposure_score(uv, duration, 3, spf)
    return {
        "id": id_,
        "telegram_username": "gil612",
        "city": city,
        "country": "IL",
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat() if end_hm else None,
        "uv_index": uv,
        "lat": lat,
        "lon": lon,
        "spf": spf,
        "exposure_score": exposure_score,
    }


# ---------------------------------------------------------------------
# 1) _sessions_on_date — פונקציה טהורה
# ---------------------------------------------------------------------
s_today = _session(1, TODAY, (9, 0), (10, 0), 5.0)
s_yesterday = _session(2, YESTERDAY, (9, 0), (10, 0), 5.0)
filtered = bc._sessions_on_date([s_today, s_yesterday], TODAY)
check("_sessions_on_date: keeps only today's session", filtered == [s_today], f"-> {filtered}")

filtered_empty = bc._sessions_on_date([s_yesterday], TODAY)
check("_sessions_on_date: empty when nothing matches", filtered_empty == [], f"-> {filtered_empty}")

# ---------------------------------------------------------------------
# 2) _daily_session_summary — פונקציה טהורה: ציון בפועל מול היפותטי
# ---------------------------------------------------------------------
no_spf_session = _session(10, TODAY, (9, 0), (11, 0), 7.0, spf=None)  # 2h, UV7, בלי הגנה
summary = bc._daily_session_summary(no_spf_session, skin_type=3, reference_spf=30)
expected_hypothetical = bc.calculate_exposure_score(7.0, 120, 3, 30)
check("_daily_session_summary: actual_score matches stored value", summary["actual_score"] == no_spf_session["exposure_score"], f"-> {summary}")
check("_daily_session_summary: hypothetical_score uses reference SPF", summary["hypothetical_score"] == expected_hypothetical, f"-> {summary} vs expected={expected_hypothetical}")
check("_daily_session_summary: hypothetical is much lower than actual (SPF30 protects)", summary["hypothetical_score"] < summary["actual_score"], f"-> {summary}")

# ---------------------------------------------------------------------
# 3) handle_today — אין שום session היום בכלל
# ---------------------------------------------------------------------
sent_messages = []


def fake_send_message(chat_id, text, reply_markup=None, **kwargs):
    sent_messages.append(text)


def make_fake_select_rows(users_row, sessions):
    def fake_select_rows(table, params):
        if table == "users":
            return [users_row] if users_row else []
        if table == "exposure_log":
            return sessions
        raise AssertionError(f"unexpected table in test: {table}")
    return fake_select_rows


with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "")
    check("handle_today: no sessions at all -> friendly empty message", len(sent_messages) == 1 and "אין לך" in sent_messages[0], f"-> {sent_messages}")

# ---------------------------------------------------------------------
# 4) handle_today — session סגור אחד היום, בלי SPF
# ---------------------------------------------------------------------
session_no_spf = _session(11, TODAY, (9, 0), (11, 0), 7.0, spf=None, city="תל אביב")
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [session_no_spf])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "")
    check("handle_today: sends exactly one message", len(sent_messages) == 1, f"-> {sent_messages}")
    msg = sent_messages[0] if sent_messages else ""
    check("handle_today: mentions 1 session", "1 sessions" in msg, f"-> {msg}")
    check("handle_today: shows total exposure score", f'{session_no_spf["exposure_score"]}%' in msg, f"-> {msg}")
    check("handle_today: shows the SPF-30 counterfactual total", "SPF 30" in msg, f"-> {msg}")
    check("handle_today: per-session line shows 'בלי קרם הגנה'", "בלי קרם הגנה" in msg, f"-> {msg}")
    check("handle_today: no open-session note when nothing is open", "session פתוח" not in msg, f"-> {msg}")

# ---------------------------------------------------------------------
# 5) handle_today — session שכבר השתמש ב-SPF: עדיין מציג השוואה מול הקבוע
# ---------------------------------------------------------------------
session_with_spf = _session(12, TODAY, (9, 0), (11, 0), 7.0, spf=15, city="חיפה")
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [session_with_spf])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "")
    msg = sent_messages[0] if sent_messages else ""
    check("handle_today: shows the SPF actually used (15)", "SPF 15" in msg, f"-> {msg}")
    check("handle_today: still shows SPF-30 comparison even though SPF was already used", "עם SPF 30:" in msg, f"-> {msg}")

# ---------------------------------------------------------------------
# 6) handle_today — 2 sessions סגורים היום -> סכימת שני הציונים
# ---------------------------------------------------------------------
sA = _session(13, TODAY, (8, 0), (9, 0), 4.0, spf=None, city="עיר א")
sB = _session(14, TODAY, (14, 0), (15, 0), 6.0, spf=30, city="עיר ב")
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [sB, sA])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "")
    msg = sent_messages[0] if sent_messages else ""
    expected_total = sA["exposure_score"] + sB["exposure_score"]
    check("handle_today: 2 sessions -> total is the sum of both actual scores", f"{expected_total}%" in msg, f"-> {msg} (expected total={expected_total})")
    check("handle_today: mentions 2 sessions", "2 sessions" in msg, f"-> {msg}")
    check("handle_today: both cities appear", "עיר א" in msg and "עיר ב" in msg, f"-> {msg}")

# ---------------------------------------------------------------------
# 7) handle_today — session מאתמול לא נכלל בכלל
# ---------------------------------------------------------------------
old_session = _session(15, YESTERDAY, (9, 0), (10, 0), 8.0, city="עיר ישנה")
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [old_session])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "")
    check("handle_today: yesterday-only sessions -> treated as 'no sessions today'", len(sent_messages) == 1 and "אין לך" in sent_messages[0], f"-> {sent_messages}")

# ---------------------------------------------------------------------
# 8) handle_today — session פתוח כרגע (בלי end_time) -> מוזכר בנפרד, לא נספר בסכימה
# ---------------------------------------------------------------------
open_session = dict(_session(16, TODAY, (9, 0), (10, 0), 5.0, city="אילת"))
open_session["end_time"] = None
open_session["exposure_score"] = None
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [session_no_spf, open_session])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "")
    msg = sent_messages[0] if sent_messages else ""
    check("handle_today: open session mentioned by city name", "אילת" in msg, f"-> {msg}")
    check("handle_today: open session note points to /end_session", "/end_session" in msg, f"-> {msg}")
    check("handle_today: closed session total still just the one closed session", "1 sessions" in msg, f"-> {msg}")

# ---------------------------------------------------------------------
# 9) handle_today — משתמש בלי שורת users (סוג עור לא ידוע) -> לא קורס
# ---------------------------------------------------------------------
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", make_fake_select_rows(None, [session_no_spf])):
    sent_messages.clear()
    try:
        bc.handle_today(123, "gil612", "")
        crashed = False
    except Exception as e:
        crashed = True
        crash_detail = str(e)
    check("handle_today: missing users row -> falls back gracefully, no crash", crashed is False, "" if not crashed else crash_detail)

# ---------------------------------------------------------------------
# 10) COMMAND_HANDLERS: /today רשום נכון
# ---------------------------------------------------------------------
check("COMMAND_HANDLERS: /today registered", bc.COMMAND_HANDLERS.get("/today") is bc.handle_today)


# ---------------------------------------------------------------------
# 11) fetch_day_uv_curve — עקומת UV מלאה ליום קלנדרי (הגרף היומי החדש
# ל-/today, הוחלט עם המשתמש ב-2026-09-08: "עקומת UV של היום + חלונות
# ה-sessions מסומנים עליה"). מדמים את Open-Meteo עם FakeClient — לא
# נוגעים ברשת אמיתית.
# ---------------------------------------------------------------------
class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload):
        self._payload = payload
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.last_params = params
        return FakeResponse(self._payload)


chart_day = datetime(2026, 8, 20, tzinfo=timezone.utc).date()
prev_day = datetime(2026, 8, 19, tzinfo=timezone.utc).date()
next_day = datetime(2026, 8, 21, tzinfo=timezone.utc).date()
two_day_payload = {
    "hourly": {
        "time": (
            [f"{prev_day.isoformat()}T{h:02d}:00" for h in range(24)]
            + [f"{chart_day.isoformat()}T{h:02d}:00" for h in range(24)]
            + [f"{next_day.isoformat()}T{h:02d}:00" for h in range(24)]
        ),
        "uv_index": [0.0] * 24 + [float(h) for h in range(24)] + [0.0] * 24,
    }
}

times, uvs = bc.fetch_day_uv_curve(FakeClient(two_day_payload), 30.61, 34.80, chart_day)
check("fetch_day_uv_curve: returns exactly 24 hours", len(times) == 24 and len(uvs) == 24, f"-> {len(times)} hours")
check("fetch_day_uv_curve: all times belong to the requested day only", all(t.startswith(chart_day.isoformat()) for t in times), f"-> {times[:2]}...{times[-2:]}")
check("fetch_day_uv_curve: hour-of-day matches uv_index value (no off-by-one)", uvs == [float(h) for h in range(24)], f"-> {uvs}")

# past_days/forecast_days: יום שכבר עבר (מול "today" האמיתי בזמן הרצת הבדיקה)
today_real = datetime.now(timezone.utc).date()
past_target = today_real - timedelta(days=5)
fake_client_past = FakeClient({"hourly": {"time": [f"{past_target.isoformat()}T{h:02d}:00" for h in range(24)], "uv_index": [1.0] * 24}})
bc.fetch_day_uv_curve(fake_client_past, 30.61, 34.80, past_target)
check("fetch_day_uv_curve: past_days computed from (today - target_date)", fake_client_past.last_params["past_days"] == 5 and fake_client_past.last_params["forecast_days"] == 1, f"-> {fake_client_past.last_params}")

# מעבר לטווח הנתמך (92+ יום אחורה) -> ([], []) בלי לקרוא לרשת בכלל
far_target = today_real - timedelta(days=200)
times_far, uvs_far = bc.fetch_day_uv_curve(FakeClient({}), 30.61, 34.80, far_target)
check("fetch_day_uv_curve: too far in the past -> ([], [])", times_far == [] and uvs_far == [], f"-> {times_far}, {uvs_far}")

# היום המבוקש בכלל לא מופיע בתשובה (קצה מוזר של ה-API) -> ([], []), לא קורס
missing_day_payload = {"hourly": {"time": [f"{prev_day.isoformat()}T{h:02d}:00" for h in range(24)], "uv_index": [1.0] * 24}}
times_missing, uvs_missing = bc.fetch_day_uv_curve(FakeClient(missing_day_payload), 30.61, 34.80, chart_day)
check("fetch_day_uv_curve: requested day absent from response -> ([], [])", times_missing == [] and uvs_missing == [], f"-> {times_missing}")


# ---------------------------------------------------------------------
# 12) render_daily_exposure_chart — בדיקת עשן (PNG תקין), לא בודקים
# פיקסלים. מוודאים שזה לא קורס עם session פתוח/סגור/רשימה ריקה.
# ---------------------------------------------------------------------
demo_times = [f"{chart_day.isoformat()}T{h:02d}:00" for h in range(24)]
demo_uv = [0, 0, 0, 0, 0, 0.5, 1, 3, 5, 6.5, 7.5, 8, 8.2, 7.8, 6.5, 5, 3, 1, 0.3, 0, 0, 0, 0, 0]

png_empty = bc.render_daily_exposure_chart(demo_times, demo_uv, [], "מצפה רמון", chart_day)
check("render_daily_exposure_chart: no sessions -> still returns a valid PNG", png_empty[:8] == b"\x89PNG\r\n\x1a\n" and len(png_empty) > 1000, f"-> {len(png_empty)} bytes")

demo_sessions = [
    {"start_time": f"{chart_day.isoformat()}T06:15:00+00:00", "end_time": f"{chart_day.isoformat()}T09:40:00+00:00", "exposure_score": 42},
    {"start_time": f"{chart_day.isoformat()}T13:00:00+00:00", "end_time": None, "exposure_score": None},  # session פתוח
]
png_full = bc.render_daily_exposure_chart(demo_times, demo_uv, demo_sessions, "מצפה רמון", chart_day)
check("render_daily_exposure_chart: closed+open sessions -> still returns a valid PNG", png_full[:8] == b"\x89PNG\r\n\x1a\n" and len(png_full) > 1000, f"-> {len(png_full)} bytes")

# session שכולו מחוץ לטווח הגרף (למשל טעות נתונים) -> לא קורס, פשוט מדולג
png_out_of_range = bc.render_daily_exposure_chart(demo_times, demo_uv, [{"start_time": f"{next_day.isoformat()}T06:00:00+00:00", "end_time": f"{next_day.isoformat()}T07:00:00+00:00", "exposure_score": 5}], "מצפה רמון", chart_day)
check("render_daily_exposure_chart: session entirely outside the day -> no crash", png_out_of_range[:8] == b"\x89PNG\r\n\x1a\n", f"-> {len(png_out_of_range)} bytes")

# תוקן ב-2026-09-09: תקלה אמיתית שדווחה — משתמש בקריית ים (UTC+3) ראה
# את "עכשיו" מסומן על הגרף סביב 02:00-03:00 בזמן שהשעון אצלו הראה
# 05:00, כי ציר-השעות הוצג לפי UTC גולמי בלי שום סימון. utc_offset_seconds
# (חדש) מזיז רק את *התוויות* המוצגות על ציר ה-X לזמן המקומי.
# קודם בודקים את נוסחת ההזחה ישירות (אותה נוסחה בדיוק כמו בפנים
# הפונקציה) — 02:00 UTC + הזחה של 3 שעות אמור לצאת 05:00, בדיוק
# התקלה שדווחה בפועל.
sample_utc_time = f"{chart_day.isoformat()}T02:00"
shifted_label = (datetime.fromisoformat(sample_utc_time) + timedelta(seconds=10800)).strftime("%H:%M")
check(
    "hour-label shift formula: 02:00 UTC + 3h offset -> 05:00 local (the exact reported bug)",
    shifted_label == "05:00",
    f"-> {shifted_label}",
)

# ואז בדיקת-עשן שהפונקציה עצמה לא קורסת עם utc_offset_seconds!=0 (רינדור אמיתי)
png_with_offset = bc.render_daily_exposure_chart(demo_times, demo_uv, demo_sessions, "קריית ים", chart_day, utc_offset_seconds=10800)
check(
    "render_daily_exposure_chart: non-zero utc_offset_seconds -> still returns a valid PNG",
    png_with_offset[:8] == b"\x89PNG\r\n\x1a\n" and len(png_with_offset) > 1000,
    f"-> {len(png_with_offset)} bytes",
)


# ---------------------------------------------------------------------
# 13) send_daily_exposure_chart — best-effort: בוחר את ה-session המוקדם
# ביותר עם lat/lon כמיקום-הייחוס; מדלג בשקט כש-אין sessions עם lat/lon,
# כש-fetch_day_uv_curve מחזיר ריק, וכש-משהו זורק חריגה.
# ---------------------------------------------------------------------
sent_photos = []


def fake_send_photo(chat_id, photo_bytes, caption=None):
    sent_photos.append((chat_id, caption))


def make_fake_fetch_day_uv_curve(hourly_uv):
    calls = []

    def _fake(client, lat, lon, target_date):
        calls.append((lat, lon, target_date))
        return (demo_times, hourly_uv) if hourly_uv else ([], [])
    return _fake, calls


# תוקן ב-2026-09-09 יחד עם utc_offset_seconds ב-render_daily_exposure_chart:
# send_daily_exposure_chart קוראת עכשיו גם ל-fetch_utc_offset_seconds
# (אחרי fetch_day_uv_curve, כשיש בכלל עקומה) — צריך לדמות גם אותה בכל
# בדיקה שבאמת מגיעה לשלב render (לא רק בודקת דילוג/כשל מוקדם), אחרת
# היא תנסה קריאת רשת אמיתית. offset=0 שומר על ההתנהגות הקודמת בדיוק
# (ראו בדיקה נפרדת למטה על ההזחה בפועל).
def fake_fetch_utc_offset_seconds_zero(client, lat, lon):
    return 0


# session אחד בלבד, בלי lat/lon בכלל (session ישן/לפני migration) -> מדלגים על הגרף כליל
with patch.object(bc, "send_photo", fake_send_photo):
    sent_photos.clear()
    bc.send_daily_exposure_chart(123, [_session(20, TODAY, (9, 0), (10, 0), 5.0)], TODAY)
    check("send_daily_exposure_chart: no session has lat/lon -> no photo sent", len(sent_photos) == 0, f"-> {sent_photos}")

# שני sessions: המוקדם יותר בלי lat/lon, המאוחר יותר עם lat/lon -> עדיין
# צריך לבחור לפי lat/lon זמין, לא לפי סדר-זמן גרידא כש-האחד לא שמיש
early_no_latlon = _session(21, TODAY, (6, 0), (7, 0), 3.0, city="עיר בלי מיקום")
later_with_latlon = _session(22, TODAY, (9, 0), (10, 0), 5.0, city="עיר עם מיקום", lat=30.61, lon=34.80)
fake_fetch, fetch_calls = make_fake_fetch_day_uv_curve(demo_uv)
with patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds_zero):
    sent_photos.clear()
    fetch_calls.clear()
    bc.send_daily_exposure_chart(123, [early_no_latlon, later_with_latlon], TODAY)
    check("send_daily_exposure_chart: falls back to the only session that has lat/lon", len(fetch_calls) == 1 and fetch_calls[0][:2] == (30.61, 34.80), f"-> {fetch_calls}")
    check("send_daily_exposure_chart: sends exactly one photo", len(sent_photos) == 1, f"-> {sent_photos}")

# שני sessions עם lat/lon -> בוחרים את המוקדם ביותר (start_time), לא את הראשון ברשימה
early_with_latlon = _session(23, TODAY, (6, 0), (7, 0), 3.0, city="עיר מוקדמת", lat=1.0, lon=1.0)
late_with_latlon = _session(24, TODAY, (15, 0), (16, 0), 5.0, city="עיר מאוחרת", lat=2.0, lon=2.0)
fake_fetch2, fetch_calls2 = make_fake_fetch_day_uv_curve(demo_uv)
with patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch2), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds_zero):
    sent_photos.clear()
    fetch_calls2.clear()
    bc.send_daily_exposure_chart(123, [late_with_latlon, early_with_latlon], TODAY)  # בכוונה בסדר הפוך
    check("send_daily_exposure_chart: picks the earliest-starting session as the reference location", fetch_calls2[0][:2] == (1.0, 1.0), f"-> {fetch_calls2}")

# fetch_day_uv_curve מחזיר ריק (מחוץ לטווח הנתמך וכו') -> לא שולחים תמונה
fake_fetch_empty, _ = make_fake_fetch_day_uv_curve(None)
with patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch_empty):
    sent_photos.clear()
    bc.send_daily_exposure_chart(123, [later_with_latlon], TODAY)
    check("send_daily_exposure_chart: empty UV curve -> no photo sent", len(sent_photos) == 0, f"-> {sent_photos}")

# חריגה כלשהי (רשת נפלה, matplotlib חסר וכו') -> לא קורס, best-effort בלבד
def fake_fetch_raises(client, lat, lon, target_date):
    raise RuntimeError("simulated failure")


with patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch_raises):
    sent_photos.clear()
    try:
        bc.send_daily_exposure_chart(123, [later_with_latlon], TODAY)
        crashed = False
    except Exception as e:
        crashed = True
        crash_detail = str(e)
    check("send_daily_exposure_chart: internal failure does not propagate", crashed is False, "" if not crashed else crash_detail)

# תוקן ב-2026-09-09: בודקים את ה"חיווט" המלא של התיקון — send_daily_exposure_chart
# קוראת ל-fetch_utc_offset_seconds עם lat/lon של ה-reference (לא של
# session אחר), ומעבירה את מה שחוזר בפועל ל-render_daily_exposure_chart
# (לא מתעלמת מהתוצאה, לא משאירה 0 קשיח). עוטפים את render_daily_exposure_chart
# האמיתי (לא מזייפים PNG) כדי גם לוודא שהוא לא קורס עם ההזחה שהתקבלה.
offset_calls = []


def fake_fetch_utc_offset_spy(client, lat, lon):
    offset_calls.append((lat, lon))
    return 10800  # +3h, כמו ישראל — אותה תקלה שדווחה בפועל


render_calls = []
_original_render_daily_exposure_chart = bc.render_daily_exposure_chart


def fake_render_daily_exposure_chart_spy(*args, **kwargs):
    render_calls.append(kwargs.get("utc_offset_seconds"))
    return _original_render_daily_exposure_chart(*args, **kwargs)


session_kiryat_yam = _session(25, TODAY, (9, 0), (10, 0), 5.0, city="קריית ים", lat=32.85, lon=35.07)
fake_fetch_wiring, _ = make_fake_fetch_day_uv_curve(demo_uv)
with patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch_wiring), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_spy), \
     patch.object(bc, "render_daily_exposure_chart", fake_render_daily_exposure_chart_spy):
    sent_photos.clear()
    offset_calls.clear()
    render_calls.clear()
    bc.send_daily_exposure_chart(123, [session_kiryat_yam], TODAY)
    check(
        "send_daily_exposure_chart: calls fetch_utc_offset_seconds with the reference session's lat/lon",
        offset_calls == [(32.85, 35.07)],
        f"-> {offset_calls}",
    )
    check(
        "send_daily_exposure_chart: passes the resolved utc_offset_seconds through to render_daily_exposure_chart",
        render_calls == [10800],
        f"-> {render_calls}",
    )
    check("send_daily_exposure_chart: still sends the photo after the wiring change", len(sent_photos) == 1, f"-> {sent_photos}")


# ---------------------------------------------------------------------
# 14) handle_today (אינטגרציה) — הגרף היומי נשלח אוטומטית יחד עם הטקסט
# כש-יש session עם lat/lon; לא נשלח בכלל כש-כל ה-sessions בלי lat/lon
# (sessions ישנים/לפני migration) — נשאר בדיוק כמו ההתנהגות הקודמת.
# ---------------------------------------------------------------------
session_with_latlon = _session(30, TODAY, (9, 0), (11, 0), 7.0, spf=None, city="מצפה רמון", lat=30.61, lon=34.80)
fake_fetch3, _ = make_fake_fetch_day_uv_curve(demo_uv)
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch3), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds_zero), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [session_with_latlon])):
    sent_messages.clear()
    sent_photos.clear()
    bc.handle_today(123, "gil612", "")
    check("handle_today: sends both the text summary and the daily chart photo", len(sent_messages) == 1 and len(sent_photos) == 1, f"-> messages={sent_messages}, photos={sent_photos}")

# ללא lat/lon בכלל (sessions ישנים) -> טקסט בלבד, בלי תמונה — כמו לפני התוספת הזו
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [session_no_spf])):
    sent_messages.clear()
    sent_photos.clear()
    bc.handle_today(123, "gil612", "")
    check("handle_today: legacy sessions without lat/lon -> text only, no photo", len(sent_messages) == 1 and len(sent_photos) == 0, f"-> messages={sent_messages}, photos={sent_photos}")


# ---------------------------------------------------------------------
# 15) _peak_exposure_session — "המיקום איפה שמד החשיפה היה הגבוה ביותר"
# (הוחלט עם המשתמש ב-2026-09-08). פונקציה טהורה.
# ---------------------------------------------------------------------
check("_peak_exposure_session: empty list -> None", bc._peak_exposure_session([]) is None)

only_open = [{"exposure_score": None, "city": "פתוח"}]
check("_peak_exposure_session: only an open session (no score) -> None", bc._peak_exposure_session(only_open) is None)

sA = {"exposure_score": 40, "city": "עיר א"}
sB = {"exposure_score": 210, "city": "עיר ב"}
sC = {"exposure_score": 15, "city": "עיר ג"}
peak = bc._peak_exposure_session([sA, sB, sC])
check("_peak_exposure_session: picks the highest score among several closed sessions", peak is sB, f"-> {peak}")

open_and_closed = [{"exposure_score": None, "city": "פתוח"}, sB]
check("_peak_exposure_session: ignores the open session, picks the closed one", bc._peak_exposure_session(open_and_closed) is sB)


# ---------------------------------------------------------------------
# 16) handle_today: date=D.M — "הוסף chart_date ככה שאוכל לראות את
# החשיפה לפי תאריך" (הוחלט עם המשתמש ב-2026-09-08). אותו parsing/
# convention בדיוק כמו date= ב-/add_session (כולל גלגול-שנה).
# ---------------------------------------------------------------------
PAST_DAY = TODAY - timedelta(days=10)
session_past_day = _session(40, PAST_DAY, (9, 0), (11, 0), 6.0, spf=None, city="ירושלים")
past_day_field = f"{PAST_DAY.day}.{PAST_DAY.month}"

with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [session_past_day])):
    sent_messages.clear()
    sent_photos.clear()
    bc.handle_today(123, "gil612", f"date={past_day_field}")
    msg = sent_messages[0] if sent_messages else ""
    check("handle_today date=: shows the requested (past) date's session, not today's", "ירושלים" in msg, f"-> {msg}")
    check("handle_today date=: summary header reflects the requested date", PAST_DAY.strftime("%d.%m.%Y") in msg, f"-> {msg}")

# אותו יום, אבל כש-אין sessions באותו תאריך -> הודעה שמזכירה את התאריך המבוקש, לא "היום"
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", f"date={past_day_field}")
    check("handle_today date=: no sessions on that date -> mentions the date, not 'today'", PAST_DAY.strftime("%d.%m.%Y") in sent_messages[0] and "היום" not in sent_messages[0], f"-> {sent_messages}")

# date= בפורמט לא תקין -> הודעת שגיאה, לא קורס
with patch.object(bc, "send_message", fake_send_message):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "date=abc")
    check("handle_today date=: invalid format -> error message", "פורמט תאריך" in sent_messages[0], f"-> {sent_messages}")

# ארגומנט שאינו date= בכלל -> הודעת שימוש
with patch.object(bc, "send_message", fake_send_message):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "משהו אחר")
    check("handle_today: unrecognized argument -> usage message", "שימוש" in sent_messages[0], f"-> {sent_messages}")

# date= עם גלגול שנה (D.M "עתידי" ביחס להיום) -> אותה מוסכמה כמו /add_session
future_date = TODAY + timedelta(days=10)
future_date_field = f"{future_date.day}.{future_date.month}"
expected_rolled_back = future_date.replace(year=future_date.year - 1)
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", f"date={future_date_field}")
    check("handle_today date=: future D.M rolls back a year, same as /add_session", expected_rolled_back.strftime("%d.%m.%Y") in sent_messages[0], f"-> {sent_messages}")


# ---------------------------------------------------------------------
# 17) handle_today: שורת "מדד החשיפה הגבוה ביותר" בטקסט + בכיתוב התמונה
# ---------------------------------------------------------------------
sA2 = _session(41, TODAY, (6, 0), (7, 0), 3.0, spf=None, city="עיר נמוכה", lat=1.0, lon=1.0)
sB2 = _session(42, TODAY, (10, 0), (13, 0), 8.0, spf=None, city="עיר גבוהה", lat=2.0, lon=2.0)
fake_fetch4, _ = make_fake_fetch_day_uv_curve(demo_uv)
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch4), \
     patch.object(bc, "fetch_utc_offset_seconds", fake_fetch_utc_offset_seconds_zero), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [sA2, sB2])):
    sent_messages.clear()
    sent_photos.clear()
    bc.handle_today(123, "gil612", "")
    msg = sent_messages[0] if sent_messages else ""
    _, caption = sent_photos[0] if sent_photos else (None, "")
    expected_peak_city = "עיר גבוהה" if sB2["exposure_score"] >= sA2["exposure_score"] else "עיר נמוכה"
    check("handle_today: text mentions the highest-exposure city", f"מדד החשיפה הגבוה ביותר: {expected_peak_city}" in msg, f"-> {msg}")
    check("handle_today: chart caption also mentions the highest-exposure city", expected_peak_city in caption, f"-> {caption}")

# session בודד (אין באמת "הגבוה ביותר" משמעותי, אבל לא אמור לקרוס/להכפיל שורות)
with patch.object(bc, "send_message", fake_send_message), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "select_rows", make_fake_select_rows({"telegram_username": "gil612", "skin_type": 3, "chat_id": 123}, [session_no_spf])):
    sent_messages.clear()
    bc.handle_today(123, "gil612", "")
    msg = sent_messages[0] if sent_messages else ""
    check("handle_today: single session -> still shows a highest-exposure line (itself)", "מדד החשיפה הגבוה ביותר" in msg, f"-> {msg}")


# ---------------------------------------------------------------------
# 18) _notify_admin_chart_skip + אינטגרציה עם send_daily_exposure_chart —
# נוסף ב-2026-09-08 אחרי שהמשתמש דיווח "איפה הגרף" (קיבל טקסט תקין
# בלי תמונה בכלל): send_daily_exposure_chart בולעת כל דילוג/כשל בכוונה
# (best-effort), מה שהפך את הסיבה האמיתית ל"שקטה" לגמרי בלי גישה
# ללוגים של ה-Space. עכשיו כל דילוג/כשל גם מדווח ל-ADMIN_CHAT_ID (אם
# מוגדר) — ראו _notify_admin_chart_skip.
# ---------------------------------------------------------------------
admin_sent = []


def fake_send_message_capture(chat_id, text, reply_markup=None, **kwargs):
    admin_sent.append((chat_id, text))


def fake_send_message_raises(chat_id, text, reply_markup=None, **kwargs):
    raise RuntimeError("Telegram API down")


with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "ADMIN_CHAT_ID", None):
    admin_sent.clear()
    bc._notify_admin_chart_skip(TODAY, "some reason")
    check("_notify_admin_chart_skip: ADMIN_CHAT_ID unset -> no send", len(admin_sent) == 0, f"-> {admin_sent}")

with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"):
    admin_sent.clear()
    bc._notify_admin_chart_skip(TODAY, "some reason")
    check("_notify_admin_chart_skip: sends exactly one message when ADMIN_CHAT_ID is set", len(admin_sent) == 1, f"-> {admin_sent}")
    if admin_sent:
        chat_id, text = admin_sent[0]
        check("_notify_admin_chart_skip: sent to ADMIN_CHAT_ID", chat_id == "999999", f"-> {chat_id}")
        check("_notify_admin_chart_skip: message includes the date and the reason", str(TODAY) in text and "some reason" in text, f"-> {text}")

logged_warnings = []


def fake_logger_warning(*args):
    # מדמה logger.warning(msg, *fmt_args) — מרכיבים ל-string אחד כדי
    # שיהיה קל לבדוק תוכן, בדיוק כמו ש-logging עצמו היה עושה עם %s.
    logged_warnings.append(args[0] % args[1:] if len(args) > 1 else args[0])


with patch.object(bc, "send_message", fake_send_message_raises), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"), \
     patch.object(bc.logger, "warning", fake_logger_warning):
    logged_warnings.clear()
    try:
        bc._notify_admin_chart_skip(TODAY, "some reason")
        crashed = False
    except Exception as e:
        crashed = True
        crash_detail = str(e)
    check("_notify_admin_chart_skip: send_message failure does not propagate", crashed is False, "" if not crashed else crash_detail)
    # תוקן ב-2026-09-08: ה-except הישן בלע את e בלי להדפיס אותו, בדיוק
    # התקלה שגרמה ל"WARNING: Failed to send admin chart-skip notification
    # for target_date=2026-09-06" בלוג האמיתי בלי שום פירוט על הסיבה.
    check(
        "_notify_admin_chart_skip: the actual exception text reaches the log (not swallowed silently)",
        len(logged_warnings) == 1 and "Telegram API down" in logged_warnings[0],
        f"-> {logged_warnings}",
    )

# אינטגרציה: אין session עם lat/lon -> גם מדווחים ל-admin (לא רק מדלגים בשקט)
no_latlon_session = _session(50, TODAY, (9, 0), (10, 0), 5.0, city="ללא מיקום")
with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"):
    admin_sent.clear()
    sent_photos.clear()
    bc.send_daily_exposure_chart(123, [no_latlon_session], TODAY)
    check("send_daily_exposure_chart: no lat/lon -> no photo, but admin is notified why", len(sent_photos) == 0 and len(admin_sent) == 1, f"-> photos={sent_photos}, admin={admin_sent}")
    if admin_sent:
        check("send_daily_exposure_chart: admin message explains the reason (no lat/lon)", "lat/lon" in admin_sent[0][1], f"-> {admin_sent}")

# אינטגרציה: fetch_day_uv_curve מחזיר ריק -> גם מדווחים ל-admin
with_latlon_session = _session(51, TODAY, (9, 0), (10, 0), 5.0, city="עם מיקום", lat=30.61, lon=34.80)
fake_fetch_empty2, _ = make_fake_fetch_day_uv_curve(None)
with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch_empty2), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"):
    admin_sent.clear()
    sent_photos.clear()
    bc.send_daily_exposure_chart(123, [with_latlon_session], TODAY)
    check("send_daily_exposure_chart: empty UV curve -> no photo, but admin is notified why", len(sent_photos) == 0 and len(admin_sent) == 1, f"-> photos={sent_photos}, admin={admin_sent}")

# אינטגרציה: חריגה כלשהי -> גם מדווחים ל-admin, ועדיין לא קורס
def fake_fetch_raises2(client, lat, lon, target_date):
    raise RuntimeError("simulated failure")


with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "fetch_day_uv_curve", fake_fetch_raises2), \
     patch.object(bc, "ADMIN_CHAT_ID", "999999"):
    admin_sent.clear()
    sent_photos.clear()
    try:
        bc.send_daily_exposure_chart(123, [with_latlon_session], TODAY)
        crashed = False
    except Exception as e:
        crashed = True
        crash_detail = str(e)
    check("send_daily_exposure_chart: exception -> does not crash", crashed is False, "" if not crashed else crash_detail)
    if not crashed:
        check("send_daily_exposure_chart: exception -> admin is notified with the error text", len(admin_sent) == 1 and "simulated failure" in admin_sent[0][1], f"-> {admin_sent}")

# ADMIN_CHAT_ID לא מוגדר -> אותה התנהגות כמו קודם (בלי שום שינוי, בלי קריסה)
with patch.object(bc, "send_message", fake_send_message_capture), \
     patch.object(bc, "send_photo", fake_send_photo), \
     patch.object(bc, "ADMIN_CHAT_ID", None):
    admin_sent.clear()
    sent_photos.clear()
    bc.send_daily_exposure_chart(123, [no_latlon_session], TODAY)
    check("send_daily_exposure_chart: ADMIN_CHAT_ID unset -> no admin message, no crash", len(admin_sent) == 0 and len(sent_photos) == 0, f"-> admin={admin_sent}, photos={sent_photos}")


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):", FAILURES)
    raise SystemExit(1)
else:
    print("All checks passed.")
