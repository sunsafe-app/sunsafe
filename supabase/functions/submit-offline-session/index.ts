// SunSafe — Offline Session Sync Edge Function ("submit-offline-session")
// -------------------------------------------------------------------------
// מקבלת batch של sessions שנתפסו לגמרי אופליין ב-Mini App
// (docs/session/index.html): timestamps + GPS אמיתי, בלי UV ובלי
// exposure_score (הלקוח לא סומך עליו — הכל מחושב כאן בשרת, אותו עיקרון
// כמו dashboard-data: never trust the client עם מספרים).
//
// לכל session בבקשה:
//   1. משחזר UV היסטורי מ-Open-Meteo (forecast API, past_days — לא
//      archive/historical API, שלא כולל UV בכלל. ראו מסמך העיצוב סעיף 6).
//   2. reverse-geocode (Nominatim) לשם עיר, best-effort.
//   3. מחשב exposure_score (logic.ts, פורט מ-calculate_exposure_score).
//   4. כותב ל-exposure_log עם client_uuid כמפתח idempotency
//      (on_conflict=client_uuid, resolution=ignore-duplicates) — retry
//      בטוח אם הלקוח לא קיבל את התשובה הקודמת.
//
// פריסה: קודם `supabase secrets set BOT_TOKEN=...` (בניגוד ל-
// SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY, זה *לא* מוזרק אוטומטית!),
// ואז `supabase functions deploy submit-offline-session --no-verify-jwt`.
//
// ראה docs/2026-08-29-offline-session-miniapp-design.md לחוזה המלא.

import {
  calculateExposureScore,
  CORS_HEADERS,
  errorResponse,
  jsonResponse,
  localizeReason,
  pastDaysFor,
  validateInitData,
  validateSessionShape,
  weightedAverageUv,
} from "./logic.ts";
import type { OfflineSessionInput, RejectedItem, SubmitRequestBody } from "./logic.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const BOT_TOKEN = Deno.env.get("BOT_TOKEN")!;

const REST_HEADERS = {
  apikey: SERVICE_ROLE_KEY,
  Authorization: `Bearer ${SERVICE_ROLE_KEY}`,
  "Content-Type": "application/json",
};

const OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast";
const NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse";
const NOMINATIM_USER_AGENT = "SunSafe-Bot/1.0 (student course project)";

async function restGet(path: string): Promise<unknown[]> {
  const response = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, { headers: REST_HEADERS });
  if (!response.ok) {
    throw new Error(`PostgREST GET ${path} -> ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

async function fetchHistoricalUv(
  lat: number,
  lon: number,
  startTimeIso: string,
  endTimeIso: string,
): Promise<number | null> {
  // past_days נגזר מ-startTimeIso בלבד (לא מ-endTimeIso) — ה-session
  // כולו כבר בעבר (ולידציה קודמת: session בן יום אחד לכל היותר), ו-
  // forecast_days=1 מכסה את כל שעות "היום" הנוכחי במידה ו-endTimeIso
  // נופל בו. ראו weightedAverageUv/logic.ts לרציונל המלא של השינוי
  // מ-nearestHourlyUv (דגימה בודדת) לממוצע-משוקלל-משך על פני כל הטווח —
  // תיקון לתקלת מצפה רמון, 2026-09-08.
  const pastDays = pastDaysFor(startTimeIso);
  if (pastDays === null) return null; // מעל 92 יום אחורה — לא נתמך

  const url = new URL(OPEN_METEO_URL);
  url.searchParams.set("latitude", String(lat));
  url.searchParams.set("longitude", String(lon));
  url.searchParams.set("hourly", "uv_index");
  url.searchParams.set("past_days", String(pastDays));
  url.searchParams.set("forecast_days", "1");

  const resp = await fetch(url.toString());
  if (!resp.ok) return null;
  const data = await resp.json();
  const times: string[] = data?.hourly?.time ?? [];
  const uvs: number[] = data?.hourly?.uv_index ?? [];
  return weightedAverageUv(times, uvs, startTimeIso, endTimeIso);
}

async function reverseGeocode(lat: number, lon: number, lang = "he"): Promise<{ city: string; country: string | null }> {
  try {
    const url = new URL(NOMINATIM_URL);
    url.searchParams.set("lat", String(lat));
    url.searchParams.set("lon", String(lon));
    url.searchParams.set("format", "json");
    url.searchParams.set("accept-language", lang);
    const resp = await fetch(url.toString(), { headers: { "User-Agent": NOMINATIM_USER_AGENT } });
    if (!resp.ok) return { city: localizeReason("לא ידוע", lang), country: null };
    const data = await resp.json();
    const addr = data?.address ?? {};
    const city = addr.city || addr.town || addr.village || addr.county || localizeReason("לא ידוע", lang);
    return { city, country: addr.country ?? null };
  } catch {
    return { city: localizeReason("לא ידוע", lang), country: null };
  }
}

async function insertSessionRow(row: Record<string, unknown>): Promise<void> {
  const url = `${SUPABASE_URL}/rest/v1/exposure_log?on_conflict=client_uuid`;
  const resp = await fetch(url, {
    method: "POST",
    headers: { ...REST_HEADERS, Prefer: "resolution=ignore-duplicates,return=minimal" },
    body: JSON.stringify(row),
  });
  if (!resp.ok) {
    throw new Error(`PostgREST insert exposure_log -> ${resp.status}: ${await resp.text()}`);
  }
}

async function processSession(
  session: OfflineSessionInput,
  username: string,
  skinType: number,
  lang: string = "he",
): Promise<{ ok: true } | { ok: false; reason: string }> {
  const shapeError = validateSessionShape(session);
  if (shapeError) return { ok: false, reason: shapeError };

  const uvIndex = await fetchHistoricalUv(session.start_lat, session.start_lon, session.start_time, session.end_time);
  if (uvIndex === null) {
    return { ok: false, reason: "לא הצלחנו לשחזר נתוני UV ל-session הזה (ייתכן שהוא ישן מדי, מעל 92 יום)" };
  }

  const { city, country } = await reverseGeocode(session.start_lat, session.start_lon, lang);
  const durationMinutes = (new Date(session.end_time).getTime() - new Date(session.start_time).getTime()) / 60000;
  const score = calculateExposureScore(uvIndex, durationMinutes, skinType, session.spf ?? null);

  await insertSessionRow({
    telegram_username: username,
    city,
    country,
    start_time: session.start_time,
    end_time: session.end_time,
    uv_index: uvIndex,
    lat: session.start_lat,
    lon: session.start_lon,
    spf: session.spf ?? null,
    exposure_score: score,
    skin_type: skinType, // כמו _begin_session בבוט — הציון מחושב לפיו
    client_uuid: session.client_uuid,
  });

  return { ok: true };
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return errorResponse("invalid_body");
  }

  let body: SubmitRequestBody;
  try {
    body = await req.json();
  } catch {
    return errorResponse("invalid_body");
  }
  if (!body || typeof body.initData !== "string" || !Array.isArray(body.sessions)) {
    return errorResponse("invalid_body");
  }

  const validation = await validateInitData(body.initData, BOT_TOKEN);
  if (!validation.valid) {
    return errorResponse("invalid_init_data");
  }
  const username = validation.user?.username;
  if (!username) {
    // המוצר כולו (כל הפקודות הקיימות בבוט) מזהה משתמשים לפי telegram_username —
    // מגבלה קיימת, לא חדשה. משתמש בלי @username ציבורי מוגדר בטלגרם לא יכול
    // להשתמש גם בשאר הבוט, אז זו לא רגרסיה של ה-Mini App.
    return errorResponse("missing_username");
  }

  try {
    const users = (await restGet(
      `users?telegram_username=eq.${encodeURIComponent(username)}&select=skin_type`,
    )) as { skin_type: number }[];

    if (users.length === 0) {
      return errorResponse("not_onboarded");
    }
    const skinType = users[0].skin_type;

    const lang = body.lang === "en" ? "en" : "he";
    const accepted: string[] = [];
    const rejected: RejectedItem[] = [];

    // ברצף, לא מקבילי: Nominatim מוגבל לבקשה/שנייה, וגם ככה אצוות
    // אופליין ריאליות קטנות (כמה sessions בודדים, לא מאות).
    for (const session of body.sessions) {
      try {
        const result = await processSession(session, username, skinType, lang);
        if (result.ok) accepted.push(session.client_uuid);
        else rejected.push({ client_uuid: session.client_uuid, reason: localizeReason(result.reason, lang) });
      } catch (err) {
        console.error("submit-offline-session: session failed:", err);
        rejected.push({ client_uuid: session?.client_uuid ?? "unknown", reason: localizeReason("שגיאת שרת בעיבוד ה-session", lang) });
      }
    }

    return jsonResponse({ accepted, rejected }, 200);
  } catch (err) {
    console.error("submit-offline-session failed:", err);
    return errorResponse("server_error");
  }
});
