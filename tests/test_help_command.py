"""
בדיקה ידנית (לא pytest) ל-/help (2026-09-16).

**למה זה נכתב.** משתמש שלח "/help" וקיבל תשובה סבירה — אבל "/help"
לא היה ב-COMMAND_HANDLERS בכלל. ההודעה עברה בגייטקיפר, סווגה VALID,
ונותבה ל-Agent Loop: Gemini חיבר טקסט עזרה בזמן אמת מתוך ה-prompt
שמתאר את הבוט. שלוש בעיות, וכל אחת מהן לבדה מצדיקה פקודה אמיתית:

  * **לא דטרמיניסטי** — נוסח אחר בכל הרצה, ומודל שמתאר פקודות מתוך
    prompt יכול גם לנקוב בפקודה שלא קיימת.
  * **לא שלם** — התשובה שנצפתה השמיטה את /today, /my_sessions,
    /offline_session ו-/diagnose_skin.
  * **יקר** — הרמת תהליך MCP ושלוש קריאות Gemini מתוך 15 לדקה,
    בשביל מחרוזת שלא משתנה.

הבדיקה האחרונה כאן היא העיקר: **הטקסט חייב לכסות כל פקודה רשומה.**
בלעדיה, כל פקודה חדשה תיווסף ותישכח מהעזרה בשקט.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-key")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import bot_commands as bc
import i18n

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1. הפקודה רשומה, ולכן לא מגיעה לגייטקיפר ולא ל-Agent Loop
# ---------------------------------------------------------------------
check("/help רשום ב-COMMAND_HANDLERS", "/help" in bc.COMMAND_HANDLERS)

sent = []
gatekeeper_calls = []
agent_calls = []
with patch.object(bc, "send_message", lambda chat_id, text, **k: sent.append(text)), \
     patch.object(bc, "classify_message", lambda t: gatekeeper_calls.append(t) or "VALID"), \
     patch.object(bc, "run_agent_via_mcp", lambda t: agent_calls.append(t) or "x"):
    bc.handle_update({
        "update_id": 1,
        "message": {"message_id": 1, "text": "/help",
                    "chat": {"id": 5, "type": "private"},
                    "from": {"id": 5, "username": "tester", "language_code": "he"}},
    })

check("נשלחה תשובה אחת", len(sent) == 1, f"-> {len(sent)}")
check("הגייטקיפר לא נקרא — אפס קריאות Gemini", gatekeeper_calls == [], f"-> {gatekeeper_calls}")
check("וה-Agent Loop לא נקרא", agent_calls == [], f"-> {agent_calls}")

text = sent[0] if sent else ""

# ---------------------------------------------------------------------
# 2. הטקסט זהה בכל קריאה
# ---------------------------------------------------------------------
check("דטרמיניסטי — אותו טקסט בשתי קריאות",
      i18n.t("help", "he") == i18n.t("help", "he"))

# ---------------------------------------------------------------------
# 3. **כל פקודה רשומה מופיעה בעזרה.** זו הבדיקה שמונעת סחיפה.
# ---------------------------------------------------------------------
missing = sorted(c for c in bc.COMMAND_HANDLERS if c not in text)
# /delete_session, /edit_session ו-/add_session עברו לדשבורד ומחזירות
# הודעת הפניה בלבד (handle_moved_to_dashboard) — הן לא פקודות פעילות
# ואין מה לפרסם אותן.
moved = {"/delete_session", "/edit_session", "/add_session"}
# /help עצמו לא מפורט בתוך טקסט העזרה — מי שקורא אותו כבר מצא אותו.
missing = [c for c in missing if c not in moved and c != "/help"]
check("כל פקודה פעילה מופיעה בטקסט העזרה", not missing, f"חסרות: {missing}")

check("והפקודות שעברו לדשבורד *לא* מופיעות",
      not [c for c in moved if c in text], f"-> {[c for c in moved if c in text]}")

# ---------------------------------------------------------------------
# 4. הגבול על המלצת מוצר נאמר במפורש
# ---------------------------------------------------------------------
check("העזרה מפנה לרוקח/רופא עור לבחירת מוצר",
      "רוקח" in text and "SPF מומלץ, לא מוצר" in text, f"-> {text[-120:]!r}")

# ---------------------------------------------------------------------
# 5. ומופיע בתפריט הפקודות של טלגרם
# ---------------------------------------------------------------------
import set_bot_profile
check("/help בתפריט שנרשם ב-setMyCommands",
      any(name == "help" for name, _desc in set_bot_profile.COMMANDS),
      f"-> {[n for n, _ in set_bot_profile.COMMANDS]}")

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
