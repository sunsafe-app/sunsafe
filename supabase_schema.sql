-- SunSafe — Supabase schema (first slice: uv_readings + alerts_sent)
-- Run this once in the Supabase project's SQL editor.
-- See docs/superpowers/specs/2026-08-24-supabase-uv-logging-design.md for rationale.

create table if not exists uv_readings (
    id             bigint generated always as identity primary key,
    created_at     timestamptz not null default now(),
    query_city     text not null,
    resolved_city  text not null,
    country        text,
    lat            double precision not null,
    lon            double precision not null,
    uv_index       double precision not null,
    temperature_2m double precision,
    cloud_cover    integer
);

create table if not exists alerts_sent (
    id            bigint generated always as identity primary key,
    created_at    timestamptz not null default now(),
    uv_reading_id bigint references uv_readings(id),
    chat_id       text not null,
    message_text  text not null,
    parse_mode    text,
    status        text not null
);

alter table uv_readings enable row level security;
alter table alerts_sent enable row level security;

alter table alerts_sent add constraint alerts_sent_status_check
    check (status in ('sent', 'failed'));


-- SunSafe — Supabase schema (second slice: personal area — users,
-- exposure_log, magic_links)
-- See docs/2026-08-25-exposure-log-schema-design.md for rationale.

create table if not exists users (
    telegram_username text primary key,
    skin_type          smallint not null check (skin_type between 1 and 6),
    created_at          timestamptz not null default now(),
    chat_id             bigint  -- לשליחת דוחות יזומים (send_uv_report.py --broadcast); NULL עד ההודעה הראשונה מהמשתמש. ראו docs/2026-08-26-multi-user-broadcast-design.md
);

-- הרצה חד-פעמית נוספת אם הטבלה כבר קיימת מלפני העדכון הזה:
-- alter table users add column if not exists chat_id bigint;

create table if not exists exposure_log (
    id                bigint generated always as identity primary key,
    created_at        timestamptz not null default now(),
    telegram_username text not null references users(telegram_username),
    city              text not null,
    country           text,
    start_time        timestamptz not null,
    end_time          timestamptz,        -- NULL = session פתוח כרגע
    uv_index          double precision not null,
    spf               integer,            -- NULL = לא נעשה שימוש בקרם הגנה
    exposure_score    integer             -- NULL עד שה-session נסגר
);

create table if not exists magic_links (
    token              text primary key,
    telegram_username  text not null,
    expires_at         timestamptz not null,
    used               boolean not null default false,
    created_at         timestamptz not null default now()
);

alter table users enable row level security;
alter table exposure_log enable row level security;
alter table magic_links enable row level security;
-- שלוש הטבלאות האלה בלי אף policy בכוונה — גישה רק דרך service_role
-- (הבוט כותב/מעדכן, ה-Edge Function של ה-Magic Link קוראת). דפדפן עם
-- anon key לא יכול לגעת בהן ישירות בשום מצב.


-- SunSafe — Supabase schema (third slice: idempotency key ל-sessions
-- שמסונכרנים מה-Mini App האופליין)
-- See docs/2026-08-29-offline-session-miniapp-design.md סעיף 8.
--
-- client_uuid נוצר בצד הלקוח (crypto.randomUUID(), אופליין, בלי רשת)
-- ברגע שסוגרים session ב-Mini App. ה-Edge Function submit-offline-session
-- כותבת עם on_conflict=client_uuid + Prefer: resolution=ignore-duplicates
-- (ראו index.ts) — כדי שאם הלקוח מנסה sync שוב אחרי שהתשובה הקודמת
-- אבדה ברשת, לא נוצרת שורה כפולה. NULL עבור כל שאר השורות (sessions
-- שנוצרו דרך /start_session ו-/end_session הרגילים, לא ה-Mini App).

alter table exposure_log add column if not exists client_uuid text;

-- בלי WHERE חלקי בכוונה: PostgREST מתרגם on_conflict=client_uuid ל-
-- "ON CONFLICT (client_uuid)" בלי predicate, וזה לא תואם לאינדקס חלקי
-- (Postgres דורש ON CONFLICT...WHERE תואם בדיוק לאינדקס partial, אחרת
-- זורק "no unique or exclusion constraint matching"). אינדקס ייחודי רגיל
-- לא באמת בעייתי כאן: PostgreSQL מטבעו לא אוכף ייחודיות בין ערכי NULL
-- מרובים (כל שורה מה-בוט הרגיל, בלי client_uuid, נשארת NULL ותמיד מותרת).
create unique index if not exists exposure_log_client_uuid_key
    on exposure_log (client_uuid);


-- SunSafe — Supabase schema (fourth slice: skin_damage_log ל-/diagnose_skin)
-- ראו skin_damage_classifier.py לרציונל המלא ולדיון על הסיכון (הוחלט
-- במפורש עם המשתמש להמשיך, "גרסה מלאה", אחרי שהוצג הסיכון בפרטיות/
-- אחריות רפואית מסיכום הפגישה). התמונה עצמה **לעולם** לא נשמרת כאן
-- ולא בשום מקום אחר — רק תוצאת ההערכה הטקסטואלית.

create table if not exists skin_damage_log (
    id                 bigint generated always as identity primary key,
    created_at         timestamptz not null default now(),
    telegram_username  text not null references users(telegram_username),
    session_id         bigint references exposure_log(id),  -- NULL אם לא נמצא session רלוונטי בזמן הבדיקה
    severity           text not null check (severity in ('none', 'mild', 'moderate', 'severe')),
    confidence         text not null check (confidence in ('low', 'medium', 'high')),
    reasoning          text not null  -- ההסבר החזותי-בלבד מה-מודל, כולל המלצת "פנו לרופא" ב-moderate/severe
);

alter table skin_damage_log enable row level security;
-- בלי policies בכוונה — גישה רק דרך service_role (הבוט כותב), אותו
-- דפוס בדיוק כמו exposure_log/users/magic_links למעלה.


-- SunSafe — Supabase schema (fifth slice: lat/lon ב-exposure_log,
-- תיקון "דגימת UV בודדת" — 2026-09-08)
--
-- באג אמיתי: /start_session+/end_session (וגם /add_session ו-Mini App
-- האופליין) שמרו UV Index אחד בלבד (בזמן תחילת ה-session) והחילו אותו
-- על כל משך ה-session, ללא קשר לאורכו. עבור session ארוך (למשל 11 שעות
-- במצפה רמון) זה נתן תוצאה שגויה לגמרי — UV=0.0 (דגימה שנפלה על שעת
-- לילה) במקום שיא אמיתי מעל 7 בצהריים. התיקון: /end_session שולף מחדש
-- ממוצע-UV משוקלל-משך על פני כל טווח [start_time, end_time] בפועל
-- (ראו weighted_average_uv / fetch_historical_uv ב-bot_commands.py) —
-- וכדי לעשות זאת צריך את ה-lat/lon שנשמרו בתחילת ה-session, שלא היו
-- קיימים בשורה בכלל עד כה.
--
-- nullable בכוונה: שורות היסטוריות (לפני ה-migration הזה) יישארו בלי
-- lat/lon ולא ניתן לתקן אותן רטרואקטיבית — /end_session נופל בחזרה
-- בבטחה לדגימה המקורית כש-lat/lon חסרים (ראו הערה ב-handle_end_session).

alter table exposure_log add column if not exists lat double precision;
alter table exposure_log add column if not exists lon double precision;
-- SunSafe — Supabase schema (sixth slice: skin_type ב-exposure_log —
-- 2026-09-16)
--
-- באג אמיתי בפרודקשן, והוא נראה כך: ב-11:55:25 עלה ל-HF Space קוד
-- שמוסיף "skin_type" ל-payload של ה-insert ל-exposure_log (שני מקומות
-- ב-bot_commands.py — _begin_session ונתיב ה-session הידני). העמודה
-- מעולם לא נוספה לטבלה, וב-11:56:15 הבקשה הראשונה נפלה:
--
--   /start_session חיפה
--   httpx.HTTPStatusError: Client error '400 Bad Request' for url
--   '.../rest/v1/exposure_log'
--
-- PostgREST דוחה insert שמזכיר עמודה שלא קיימת (PGRST204). ב-09:40
-- באותו בוקר session זהה נפתח בהצלחה — הקוד הוא שהשתנה, לא הנתונים.
--
-- **למה העמודה נחוצה בכלל:** עד כה /end_session שלף את סוג העור מ-
-- users בזמן הסגירה. אם המשתמש שינה את סוג העור *באמצע* ה-session,
-- הציון חושב לפי הסוג החדש על חשיפה שנמדדה לפי הישן. שמירת סוג העור
-- על השורה בזמן הפתיחה נועלת את הקלט שלפיו הציון יחושב.
--
-- nullable בכוונה: 176 השורות ההיסטוריות נשארות בלי הערך, ו-
-- handle_end_session נופל בחזרה ל-users כשהוא חסר. אותו check
-- constraint כמו ב-users, כדי ששתי הטבלאות לא יסכימו על טווח שונה.

alter table exposure_log add column if not exists skin_type smallint;
alter table exposure_log drop constraint if exists exposure_log_skin_type_check;
alter table exposure_log add constraint exposure_log_skin_type_check
    check (skin_type is null or skin_type between 1 and 6);
