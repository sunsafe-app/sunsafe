"""
בדיקה ידנית (לא pytest) ל-TokenBucket (2026-09-16).

הרקע: העיבוד הטורי ב-poll_forever היה מגבִּיל-קצב בלי כוונה — thread
אחד שמחכה ~10 שניות לכל שאלת AI לא מסוגל להוציא יותר מ-~6 קריאות
Gemini בדקה, מתחת למכסת ה-tier החינמי (~15-30/דקה). עיבוד מקבילי
מבטל את המגבלה המקרית הזו, ולכן הדלי הזה נכנס באותו שינוי.

הבדיקות כאן מודדות זמן אמיתי, אז הן משתמשות בקצבים גבוהים (מאות
לדקה) כדי להישאר תחת שנייה. הלוגיקה זהה.
"""
import os
os.environ.setdefault("BOT_TOKEN", "TEST_TOKEN")

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading
import time

from rate_limit import TokenBucket

FAILURES = []


def check(name, condition, detail=""):
    print(f"[{'OK' if condition else 'FAIL'}] {name} {detail}")
    if not condition:
        FAILURES.append(name)


# ---------------------------------------------------------------------
# 1. הפרץ זמין מיד, ואחריו יש המתנה
# ---------------------------------------------------------------------
bucket = TokenBucket(rate_per_minute=600, burst=3)  # 10/שנייה
t0 = time.monotonic()
immediate = sum(1 for _ in range(3) if bucket.acquire(timeout=0))
check("שלושת אסימוני הפרץ מתקבלים מיד", immediate == 3, f"-> {immediate}")
check("והם באמת היו מיידיים", time.monotonic() - t0 < 0.05)

check("הרביעי לא זמין מיד", bucket.acquire(timeout=0) is False)

t0 = time.monotonic()
check("אבל מתקבל אחרי ההמתנה", bucket.acquire(timeout=1.0) is True)
waited = time.monotonic() - t0
check("וההמתנה בערך 1/10 שנייה, כמו הקצב", 0.05 < waited < 0.3, f"-> {waited:.3f}s")

# ---------------------------------------------------------------------
# 2. timeout שפג מחזיר False ולא זורק
# ---------------------------------------------------------------------
tight = TokenBucket(rate_per_minute=60, burst=1)
tight.acquire(timeout=0)
t0 = time.monotonic()
got = tight.acquire(timeout=0.1)
check("timeout שפג -> False", got is False)
check("ולא חיכה מעבר ל-timeout", time.monotonic() - t0 < 0.25, f"-> {time.monotonic() - t0:.3f}s")

# ---------------------------------------------------------------------
# 3. הקצב נשמר תחת מקביליות — זו כל הנקודה
# ---------------------------------------------------------------------
# 20 threads מנסים לקחת 40 אסימונים בקצב 600/דקה (10/שנייה) עם פרץ 5.
# 40 אסימונים = 5 מהפרץ + 35 בקצב => לא פחות מ-3.5 שניות... יותר מדי
# לבדיקה. אז 1200/דקה (20/שנייה): 5 + 15 בקצב => ~0.75 שניות.
fast = TokenBucket(rate_per_minute=1200, burst=5)
taken = []
taken_lock = threading.Lock()


def worker():
    for _ in range(1):
        if fast.acquire(timeout=5.0):
            with taken_lock:
                taken.append(time.monotonic())


t0 = time.monotonic()
threads = [threading.Thread(target=worker) for _ in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()
elapsed = time.monotonic() - t0

check("כל 20 ה-threads קיבלו אסימון", len(taken) == 20, f"-> {len(taken)}")
# 20 אסימונים = 5 פרץ + 15 בקצב 20/שנייה = 0.75s לפחות
check("והקצב נשמר — לא כולם עברו בבת אחת", elapsed > 0.6, f"-> {elapsed:.3f}s")
check("ולא נחסם יותר מהנדרש", elapsed < 2.0, f"-> {elapsed:.3f}s")

# ---------------------------------------------------------------------
# 4. קצב לא חוקי נדחה בהגדרה, לא בזמן ריצה
# ---------------------------------------------------------------------
for bad in (0, -1):
    raised = False
    try:
        TokenBucket(rate_per_minute=bad)
    except ValueError:
        raised = True
    check(f"rate_per_minute={bad} -> ValueError", raised)

# ---------------------------------------------------------------------
# 5. הדלי לא מצטבר מעל הקיבולת
# ---------------------------------------------------------------------
capped = TokenBucket(rate_per_minute=6000, burst=2)
time.sleep(0.15)  # מספיק זמן ל-15 אסימונים אם לא הייתה תקרה
check("הדלי נעצר בקיבולת", capped.available <= 2.0 + 1e-6, f"-> {capped.available:.2f}")

print()
if FAILURES:
    print(f"{len(FAILURES)} בדיקות נכשלו: {', '.join(FAILURES)}")
    raise SystemExit(1)
print("כל הבדיקות עברו.")
