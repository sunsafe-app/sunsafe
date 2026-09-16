// SunSafe — sunset auto-close Worker
// =====================================================================
// Cron Trigger שמחליף את auto_close_expired_sessions_forever
// מ-bot_commands.py: thread שרץ בתוך הבוט בטיק של 5 דקות, בתוך תהליך
// שכבר מריץ לולאת polling יחידה.
//
// כל הלוגיקה הטהורה יושבת ב-logic.ts ונבדקת מול fixture שנוצר
// מהפייתון — ראו logic.test.ts. כאן רק I/O: Supabase, Open-Meteo, טלגרם.
//
// פריסה:
//   wrangler secret put SUPABASE_URL
//   wrangler secret put SUPABASE_SERVICE_ROLE_KEY
//   wrangler secret put BOT_TOKEN
//   wrangler deploy
//
// בדיקה ידנית בלי לחכות ל-cron:
//   wrangler dev --test-scheduled
//   curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"

import {
  buildCompletionMessage,
  dailyExposureScore,
  calculateExposureScore,
  isPastSunset,
  pastDaysFor,
  sunsetToUtc,
  weightedAverageUv,
} from "./logic.ts";

interface Env {
  SUPABASE_URL: string;
  SUPABASE_SERVICE_ROLE_KEY: string;
  BOT_TOKEN: string;
}

// שני הטיפוסים היחידים שאנחנו צריכים מ-@cloudflare/workers-types,
// מוגדרים כאן במקום להתקין את החבילה כולה. היא נותנת *רק* טיפוסים,
// ואנחנו לא מקמפלים: הבדיקות רצות תחת --experimental-strip-types
// שמתעלם מטיפוסים, ו-wrangler deploy מוריד אותם ב-esbuild בלי לבדוק.
// בלי זה, כל עדכון של wrangler עלול לשבור את npm install על התנגשות
// peer dependency (קרה ב-16.9.2026 עם wrangler 4.132 שדרש types@^5).
interface ScheduledEvent {
  readonly scheduledTime: number;
  readonly cron: string;
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

interface SessionRow {
  id: number;
  telegram_username: string;
  city: string;
  start_time: string;
  uv_index: number | null;
  lat: number | null;
  lon: number | null;
  spf: number | null;
}

const OPEN_METEO = "https://api.open-meteo.com/v1/forecast";

// ---------------------------------------------------------------------
// Supabase (PostgREST) — אותה שכבה דקה שיש ב-supabase_client.py
// ---------------------------------------------------------------------
async function sbFetch(env: Env, path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(`${env.SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: env.SUPABASE_SERVICE_ROLE_KEY,
      Authorization: `Bearer ${env.SUPABASE_SERVICE_ROLE_KEY}`,
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

async function selectRows<T>(env: Env, path: string): Promise<T[]> {
  const res = await sbFetch(env, path);
  if (!res.ok) throw new Error(`Supabase GET ${path} -> ${res.status} ${await res.text()}`);
  return (await res.json()) as T[];
}

// ---------------------------------------------------------------------
// Open-Meteo
// ---------------------------------------------------------------------
async function fetchSunsetUtc(lat: number, lon: number): Promise<Date | null> {
  const url = `${OPEN_METEO}?latitude=${lat}&longitude=${lon}&daily=sunset&forecast_days=1&timezone=auto`;
  const res = await fetch(url);
  if (!res.ok) return null;
  const body = (await res.json()) as {
    daily?: { sunset?: string[] };
    utc_offset_seconds?: number;
  };
  const local = body.daily?.sunset?.[0];
  if (!local || body.utc_offset_seconds === undefined) return null;
  return sunsetToUtc(local, body.utc_offset_seconds);
}

/** ממוצע UV משוקלל-משך על פני ה-session. null = לא זמין, ואז נופלים לדגימה השמורה. */
async function fetchWeightedUv(
  lat: number, lon: number, start: Date, end: Date,
): Promise<number | null> {
  const pastDays = pastDaysFor(start.toISOString(), end);
  if (pastDays === null) return null;

  const url = `${OPEN_METEO}?latitude=${lat}&longitude=${lon}&hourly=uv_index`
    + `&past_days=${pastDays}&forecast_days=1&timezone=UTC`;
  const res = await fetch(url);
  if (!res.ok) return null;
  const body = (await res.json()) as { hourly?: { time?: string[]; uv_index?: (number | null)[] } };
  const times = body.hourly?.time;
  const values = body.hourly?.uv_index;
  if (!times || !values) return null;
  return weightedAverageUv(times, values, start, end);
}

async function sendTelegram(env: Env, chatId: number, text: string): Promise<void> {
  const res = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
  if (!res.ok) {
    // בוט לא יכול לכתוב למשתמש שחסם אותו או שמעולם לא התחיל אותו.
    // זה לא אמור למנוע את סגירת ה-session — ראו closeSession.
    console.warn(`sendMessage to ${chatId} failed: ${res.status} ${await res.text()}`);
  }
}

/**
 * הסיכום היומי של המשתמש — סכום מדדי החשיפה של כל ה-sessions הסגורים
 * שהתחילו באותו תאריך UTC. null כשאין נתון, ואז ההודעה נשלחת בלי
 * הבלוק היומי במקום לא להישלח בכלל.
 *
 * "יום" הוא תאריך ה-UTC של start_time, בדיוק כמו _sessions_on_date
 * בפייתון — ה-Worker לא בוחר הגדרה משלו. שולפים 50 אחרונים ומסננים
 * כאן, אותו דפוס כמו handle_today, ולא range filter על start_time.
 */
async function fetchDailySummary(
  env: Env, username: string, now: Date,
): Promise<{ score: number; sessionCount: number; totalMinutes: number;
             peakCity: string | null; peakScore: number | null } | null> {
  const rows = await selectRows<{
    city: string; start_time: string; end_time: string | null; exposure_score: number | null;
  }>(
    env,
    `exposure_log?telegram_username=eq.${encodeURIComponent(username)}`
    + "&select=city,start_time,end_time,exposure_score"
    + "&order=start_time.desc&limit=50",
  );

  const today = now.toISOString().slice(0, 10);
  const closed = rows.filter((r) =>
    r.end_time !== null && r.exposure_score !== null
    && r.start_time.slice(0, 10) === today);
  if (closed.length === 0) return null;

  const totalMinutes = closed.reduce((sum, r) =>
    sum + (new Date(r.end_time!).getTime() - new Date(r.start_time).getTime()) / 60000, 0);

  const peak = closed.reduce((best, r) =>
    (best === null || (r.exposure_score ?? 0) > (best.exposure_score ?? 0)) ? r : best,
    null as typeof closed[number] | null);

  return {
    score: dailyExposureScore(closed.map((r) => r.exposure_score)),
    sessionCount: closed.length,
    totalMinutes,
    peakCity: peak?.city ?? null,
    peakScore: peak?.exposure_score ?? null,
  };
}

// ---------------------------------------------------------------------
// סגירת session בודד
// ---------------------------------------------------------------------
async function closeSession(env: Env, session: SessionRow, now: Date): Promise<boolean> {
  const { id, telegram_username: username, lat, lon } = session;

  if (lat === null || lon === null) {
    // שורות מלפני migration ה-lat/lon: אין דרך לדעת מתי שוקעת השמש.
    console.warn(`session ${id} has no lat/lon — skipping sunset check`);
    return false;
  }

  const sunsetUtc = await fetchSunsetUtc(lat, lon);
  if (!isPastSunset(now, sunsetUtc)) return false;

  // בדיקה חוזרת שה-session עדיין פתוח: אם המשתמש שלח /end_session
  // בדיוק באותו רגע, אסור לסגור פעמיים ולשלוח לו הודעה כפולה.
  const stillOpen = await selectRows<SessionRow>(
    env, `exposure_log?id=eq.${id}&end_time=is.null&select=id`,
  );
  if (stillOpen.length === 0) return false;

  const users = await selectRows<{ chat_id: number | null; skin_type: number | null }>(
    env,
    `users?telegram_username=eq.${encodeURIComponent(username)}&select=chat_id,skin_type`,
  );
  const chatId = users[0]?.chat_id ?? null;
  // 1 ולא 3 (שונה 16.9.2026) — סוג עור 1 נותן את תקציב הזמן הקצר
  // ביותר, וזו ההנחה הנכונה כשאין שורת users. אותו שינוי בוצע ב-
  // handle_end_session בפייתון; שני המימושים חייבים להסכים, אחרת
  // session שנסגר אוטומטית יקבל ציון שונה מאחד שנסגר ידנית.
  const skinType = users[0]?.skin_type ?? 1;

  const start = new Date(session.start_time);
  const durationMinutes = (now.getTime() - start.getTime()) / 60000;

  // ריענון ה-UV לממוצע משוקלל, בדיוק כמו handle_end_session. כשל נופל
  // בחזרה לדגימה השמורה — לעולם לא מונע את הסגירה.
  let uvIndex = session.uv_index ?? 0;
  let uvIsAverage = false;
  try {
    const refreshed = await fetchWeightedUv(lat, lon, start, now);
    if (refreshed !== null) {
      uvIndex = refreshed;
      uvIsAverage = true;
    }
  } catch (err) {
    console.warn(`session ${id}: weighted-UV refresh failed, keeping the snapshot: ${err}`);
  }

  const score = calculateExposureScore(uvIndex, durationMinutes, skinType, session.spf);

  // **הכתיבה ל-DB קודמת לשליחה בטלגרם**, כמו בפייתון. אם נשלח קודם
  // והכתיבה תיכשל, ה-session יישאר פתוח והסבב הבא יסגור אותו שוב —
  // והמשתמש יקבל את אותה הודעה כל 5 דקות עד אינסוף.
  const patch = await sbFetch(env, `exposure_log?id=eq.${id}`, {
    method: "PATCH",
    headers: { Prefer: "return=minimal" },
    body: JSON.stringify({
      end_time: now.toISOString(),
      spf: session.spf,
      exposure_score: score,
      uv_index: uvIndex,
    }),
  });
  if (!patch.ok) {
    throw new Error(`session ${id}: PATCH failed -> ${patch.status} ${await patch.text()}`);
  }

  console.log(
    `auto-closed session ${id} for @${username}: score=${score} `
    + `(sunset ${sunsetUtc?.toISOString()}, now ${now.toISOString()})`,
  );

  if (chatId) {
    // best-effort: ה-session כבר נסגר ונכתב, וכשל בשליפת היום לא אמור
    // למנוע מהמשתמש את התוצאה של עצמו. השליפה *אחרי* ה-PATCH, כך
    // שה-session שנסגר כרגע נכלל בסכום.
    let daily = null;
    try {
      daily = await fetchDailySummary(env, username, now);
    } catch (err) {
      console.warn(`session ${id}: daily summary lookup failed: ${err}`);
    }

    await sendTelegram(env, chatId, buildCompletionMessage({
      durationMinutes, city: session.city, score, uvIndex, uvIsAverage, skinType,
      spf: session.spf, daily,
    }));
  } else {
    console.warn(`session ${id}: no chat_id for @${username} — closed without notifying`);
  }
  return true;
}

// ---------------------------------------------------------------------
// הסבב
// ---------------------------------------------------------------------
export async function runSweep(env: Env, now: Date = new Date()): Promise<{ closed: number; seen: number }> {
  const open = await selectRows<SessionRow>(
    env,
    "exposure_log?end_time=is.null&select=id,telegram_username,city,start_time,uv_index,lat,lon,spf",
  );

  let closed = 0;
  for (const session of open) {
    // כל session בנפרד: שורה חריגה אחת לא מפילה את כל הסבב, בדיוק
    // כמו ב-_auto_close_expired_sessions_once.
    try {
      if (await closeSession(env, session, now)) closed++;
    } catch (err) {
      console.error(`failed to process session ${session.id}: ${err}`);
    }
  }
  return { closed, seen: open.length };
}

export default {
  async scheduled(_event: ScheduledEvent, env: Env, _ctx: ExecutionContext): Promise<void> {
    const { closed, seen } = await runSweep(env);
    console.log(`sunset sweep: ${seen} open session(s), ${closed} closed`);
  },
};
