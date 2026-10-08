-- SunSafe — שפת ממשק לכל משתמש (2026-10-08)
-- להריץ פעם אחת ב-Supabase SQL Editor, *לפני* פריסת הבוט וה-Worker.
-- שורות קיימות מקבלות 'he' מה-default — כל המשתמשים עד היום דוברי עברית.
alter table users add column if not exists language text not null default 'he'
    check (language in ('he', 'en'));
