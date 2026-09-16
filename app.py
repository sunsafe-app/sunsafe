"""
SunSafe — Hugging Face Space entry point
------------------------------------------
עוטף את bot_commands.py (ה-listener האמיתי, polling מול Telegram) בתוך
אפליקציית Gradio מינימלית, כדי שהקוד ירוץ 24/7 על Hugging Face Spaces
(CPU Basic, חינמי) במקום להיות תלוי שהמחשב של המשתמש דלוק. ראו
docs/2026-08-27-hf-spaces-hosting-design.md לרציונל המלא.

ה-Gradio UI עצמו לא עושה שום דבר פונקציונלי — הוא קיים רק כי Spaces
חינמיים "נרדמים" בלי תעבורת HTTP נכנסת, אז צריך *משהו* שאפשר לפנג' אליו
(ראו .github/workflows/keep-alive.yml) כדי לשמור על ה-Space ער. ה-
listener האמיתי של הבוט רץ ב-thread נפרד ברקע, בלתי תלוי לגמרי ב-Gradio
— גם אם אף אחד לעולם לא פותח את העמוד הזה בדפדפן.

חשוב: כל הסודות (BOT_TOKEN, GEMINI_API_KEY, SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY) חייבים להיות מוגדרים כ-Space secrets
(Settings → Variables and secrets) *לפני* ההפעלה הראשונה — bot_commands
קורא os.environ["BOT_TOKEN"] (לא .get) ברמת המודול, אז חוסר סוד יגרום
לקריסה מיידית (KeyError) כבר בעליית האפליקציה.
"""

import logging
import threading

import gradio as gr

from bot_commands import poll_forever

logger = logging.getLogger("sunsafe.hf_space")


# ---------------------------------------------------------------------
# ZeroGPU compliance probe
# ---------------------------------------------------------------------
# ה-Space רץ על חומרת ZeroGPU, ו-HF דורש שכל Space כזה יכיל *לפחות
# פונקציה אחת* מסומנת ב-@spaces.GPU. בלי זה ההרצה נכשלת עם
# "No @spaces.GPU function detected during startup" וה-container נהרג
# תוך שניות מהעלייה — מה שראינו בפועל ב-2026-09-12: הבוט עלה, הספיק
# לקלוט הודעה אחת ("מה ה-UV בהונולולו?"), ונהרג באמצע עיבודה לפני
# שהספיק לענות. מבחוץ זה נראה בדיוק כמו "הבוט לא עובד".
#
# SunSafe עצמו לא צריך GPU בכלל (קריאות רשת + Gemini API חיצוני בלבד,
# בלי שום inference מקומי) — הפונקציה הזו היא no-op שקיימת אך ורק כדי
# לעמוד בדרישה הפורמלית הזו, ולעולם לא נקראת בזרימה האמיתית. הפתרון
# ה"נכון" יותר היה להוריד את ה-Space חזרה ל-CPU basic, אבל HF חוסם
# את השינוי הזה בלי מנוי PRO.
#
# ה-import עטוף ב-try: חבילת spaces מותקנת אוטומטית רק על חומרת ZeroGPU
# ב-HF, ולא קיימת בהרצה מקומית — שם פשוט מדלגים, בלי להפיל את הבוט.
try:
    import spaces

    @spaces.GPU
    def _zerogpu_startup_probe() -> str:
        """no-op — קיימת רק כדי לספק ל-ZeroGPU פונקציה מסומנת. ראו ההערה למעלה."""
        return "ok"

    logger.info("ZeroGPU probe registered (@spaces.GPU)")
except ImportError:
    logger.info("spaces package unavailable — skipping ZeroGPU probe (expected outside HF ZeroGPU hardware)")


# הסגירה האוטומטית בשקיעה עברה ל-Cloudflare Worker (2026-09-16) —
# cloudflare/sunset-worker, Cron Trigger כל 5 דקות. היא הייתה כאן
# כ-thread שני בתוך אותו תהליך, וזו הייתה הצורה הלא נכונה: עבודה
# מתוזמנת בתוך תהליך שחוסם על getUpdates עד 30 שניות בכל סבב, ושנפל
# יחד עם הבוט בכל פריסה מחדש.
_bot_thread_started = False
_start_lock = threading.Lock()


def _start_bot_once() -> None:
    """
    מפעיל את poll_forever() ב-thread נפרד, פעם אחת בלבד לכל תהליך.
    ה-lock+flag מונעים שני listeners מקבילים בטעות (אותה בעיית 409
    Conflict מ-Telegram שכבר נתקלנו בה כששני תהליכים רצו על אותו
    BOT_TOKEN בו-זמנית — ראו docs/2026-08-27-hf-spaces-hosting-design.md).
    """
    global _bot_thread_started
    with _start_lock:
        if _bot_thread_started:
            return
        _bot_thread_started = True
        thread = threading.Thread(target=poll_forever, name="sunsafe-bot-poll", daemon=True)
        thread.start()
        logger.info("SunSafe bot polling thread started")


_start_bot_once()

with gr.Blocks(title="SunSafe Bot Status") as demo:
    gr.Markdown(
        "## ☀️ SunSafe — הבוט פעיל\n\n"
        "זהו עמוד סטטוס בלבד. הבוט האמיתי (`bot_commands.py`) רץ ברקע "
        "כ-thread נפרד ומאזין ל-Telegram — הוא לא תלוי בעמוד הזה בשום "
        "צורה. העמוד קיים רק כדי לתת ל-Space כתובת URL שאפשר \"לפנג'\" "
        "(keep-alive, ראו .github/workflows/keep-alive.yml) כדי שהוא "
        "לא יירדם על החומרה החינמית."
    )

if __name__ == "__main__":
    demo.launch()
