"""
SunSafe — מגבִּיל קצב לקריאות Gemini
=====================================
נוסף 16.9.2026, יחד עם עיבוד העדכונים המקבילי.

**למה זה נדרש בדיוק עכשיו.** עד לשינוי הזה poll_forever עיבד עדכונים
בטור, אחד אחרי השני. זה היה איטי — אבל זה גם היה מגבִּיל-קצב בלי
שאף אחד תכנן אותו: אם שאלה חופשית לוקחת ~10 שניות (הרמת תהליך MCP
+ כמה סבבי Gemini), thread אחד פשוט לא *מסוגל* להוציא יותר מ-~6
קריאות בדקה. ה-tier החינמי של Gemini מוגבל ל-~15-30 בקשות לדקה, אז
המימוש הטורי נשאר מתחתיו תמיד, במקרה.

ברגע שמעבדים במקביל המגבלה המקרית הזו נעלמת: pool של 8 threads יכול
להוציא עשרות קריאות בדקה, והתוצאה היא 429-ים. כלומר בלי המודול הזה,
המקביליות הופכת "איטי" ל"נכשל" — זו לא תוספת לשינוי, זה חלק ממנו.

GEMINI_RPM מוגדר שמרנית (15) כי זה הקצה הנמוך של מה שראינו בפועל.
מי שיש לו מפתח בתשלום יכול להעלות אותו במשתנה סביבה בלי שינוי קוד.
"""

import logging
import os
import threading
import time

logger = logging.getLogger("sunsafe.rate_limit")


class TokenBucket:
    """
    token bucket פשוט ובטוח ל-threads.

    הדלי מתמלא ברציפות בקצב rate_per_minute, ועד burst אסימונים
    נשמרים לפרץ. acquire() מחזירה True אם התקבל אסימון תוך timeout,
    ו-False אם לא — היא *לא* זורקת, כדי שהקורא יחליט מה לעשות
    (ה-gatekeeper נופל פתוח, ה-Agent Loop מחזיר שגיאה למשתמש).
    """

    def __init__(self, rate_per_minute: float, burst: int | None = None) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute חייב להיות חיובי")
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(burst if burst is not None else max(1, int(rate_per_minute // 3)))
        self._tokens = self._capacity
        # monotonic ולא time(): שינוי שעון מערכת לא אמור לפתוח פרץ
        # או להקפיא את הדלי.
        self._updated = time.monotonic()
        self._condition = threading.Condition()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
        self._updated = now

    def acquire(self, timeout: float | None = None) -> bool:
        """לוקח אסימון אחד. True = התקבל, False = פג ה-timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

                needed = (1.0 - self._tokens) / self._rate_per_second
                if deadline is None:
                    wait = needed
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    wait = min(needed, remaining)
                # wait משחרר את המנעול, אז threads אחרים ממשיכים להתמלא
                # ולהתחרות באופן הוגן-בקירוב על האסימון הבא.
                self._condition.wait(timeout=max(wait, 0.001))

    @property
    def available(self) -> float:
        """כמה אסימונים זמינים כרגע — לדיאגנוסטיקה ולבדיקות."""
        with self._condition:
            self._refill_locked()
            return self._tokens


GEMINI_RPM = float(os.environ.get("GEMINI_RPM", "15"))

# פרץ קטן בכוונה: הוא בולע כמה הודעות שמגיעות יחד בלי לפרוץ את
# החלון של הדקה. דלי גדול היה מאפשר 15 קריאות בשנייה אחת, וזה
# בדיוק מה ש-Gemini מודד.
GEMINI_BUDGET = TokenBucket(GEMINI_RPM, burst=max(2, int(GEMINI_RPM // 3)))

logger.info("Gemini budget: %.0f requests/minute (burst %.0f)", GEMINI_RPM, GEMINI_BUDGET.available)


def spend_gemini(purpose: str, timeout: float) -> bool:
    """
    לוקח אסימון אחד מתקציב Gemini. True = אפשר לקרוא ל-API.

    מה עושים כשלא — תלוי בקורא, ובכוונה:
      * ה-gatekeeper נופל *פתוח* (VALID). הוא שומר-סף, ועדיף שהודעה
        אחת תעבור בלי סינון על פני משתמש אמיתי שנחסם. זו גם ההתנהגות
        שכבר הייתה לו בכל כשל אחר.
      * ה-Agent Loop מחכה ארוך יותר ואז נכשל. המשתמש כבר ממתין
        לתשובה, וחצי דקה עדיפה על תשובה שגויה או על שתיקה.
    """
    if GEMINI_BUDGET.acquire(timeout=timeout):
        return True
    logger.warning(
        "Gemini budget exhausted for %s (%.0f req/min) — waited %.1fs",
        purpose, GEMINI_RPM, timeout,
    )
    return False
