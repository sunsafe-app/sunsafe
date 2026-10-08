// SunSafe — Dashboard Session CRUD Edge Function ("dashboard-sessions")
// -------------------------------------------------------------------------
// הוספה / עריכה / מחיקה של sessions מתוך הדשבורד (docs/dashboard/index.html).
// נוצרה 2026-09-12, כשהפעולות האלה עברו מהבוט לדשבורד — /add_session,
// /edit_session ו-/delete_session הוסרו מ-bot_commands.py.
//
// אימות: אותו Magic Link Token של dashboard-data (טבלת magic_links,
// נוצר ב-/dashboard בבוט, בתוקף 24 שעות). בשונה מ-dashboard-data הטוקן
// מגיע בגוף ה-POST ולא ב-query string — טוקן ב-URL דולף בקלות ללוגים,
// להיסטוריית דפדפן ול-Referer, ופה הוא כבר מאפשר *כתיבה* ולא רק קריאה.
//
// שני עקרונות אבטחה שמנחים את כל הקובץ:
//   1. never trust the client עם מספרים — UV ו-exposure_score *תמיד*
//      מחושבים כאן מחדש (Open-Meteo + הנוסחה), אף פעם לא מתקבלים מהדף.
//      אותו עיקרון כמו submit-offline-session ו-dashboard-data.
//   2. ownership — לפני כל update/delete נבדק ששורת ה-exposure_log
//      באמת שייכת ל-telegram_username שה-token מזהה. בלי זה, טוקן של
//      משתמש אחד היה מאפשר לגעת בשורות של אחרים לפי ניחוש id.
//
// פריסה: supabase functions deploy dashboard-sessions --no-verify-jwt
// (בלי הדגל, Supabase דוחה כל בקשה בלי Auth JWT עוד לפני שהקוד רץ —
// הדף הסטטי לא מחובר ל-Supabase Auth, הטוקן עצמו הוא האימות.)
// לא צריך secrets ידניים: SUPABASE_URL ו-SUPABASE_SERVICE_ROLE_KEY
// מוזרקים אוטומטית לכל Edge Function.

import {
  calculateExposureScore,
  CORS_HEADERS,
  errorResponse,
  isTokenExpired,
  jsonResponse,
  localWallClockToUtcIso,
  pastDaysFor,
  localizeMessage,
  normalizeLang,
  resolveSessionTimes,
  textMatches,
  utcIsoToLocalWallClock,
  validateRequest,
  weightedAverageUv,
} from "./logic.ts";
import type { RequestBody, SessionPayload } from "./logic.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const REST_HEADERS = {
  apikey: SERVICE_ROLE_KEY,
  Authorization: `Bearer ${SERVICE_ROLE_KEY}`,
  "Content-Type": "application/json",
};

const OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast";
const GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search";
const NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search";
const NOMINATIM_USER_AGENT = "SunSafe-Bot/1.0 (student course project)";

interface GeoResult {
  name: string;
  country: string | null;
  latitude: number;
  longitude: number;
}

// -----------------------------------------------------------------------
// PostgREST
// -----------------------------------------------------------------------

async function restGet(path: string): Promise<unknown[]> {
  const response = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, { headers: REST_HEADERS });
  if (!response.ok) {
    throw new Error(`PostgREST GET ${path} -> ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

async function restWrite(
  path: string,
  method: "POST" | "PATCH" | "DELETE",
  body?: unknown,
): Promise<void> {
  const response = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    method,
    headers: { ...REST_HEADERS, Prefer: "return=minimal" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`PostgREST ${method} ${path} -> ${response.status}: ${await response.text()}`);
  }
}

// -----------------------------------------------------------------------
// Geocoding — פורט של geocode_city מ-geo_uv_core.py, כולל שלוש השכבות:
// התאמה ישירה -> פיצול "עיר + מדינה" -> Nominatim. ראו שם את הרציונל
// המלא (כולל המקרה האמיתי של "סן חוסה קוסטה ריקה" מ-2026-09-08).
// -----------------------------------------------------------------------

async function rawGeocodeSearch(name: string, count: number, lang = "he"): Promise<GeoResult[]> {
  const url = new URL(GEOCODING_URL);
  url.searchParams.set("name", name);
  url.searchParams.set("count", String(count));
  url.searchParams.set("language", lang);
  url.searchParams.set("format", "json");

  const response = await fetch(url.toString());
  if (!response.ok) return [];
  const data = await response.json();
  const results = data?.results ?? [];
  return results.map((r: Record<string, unknown>) => ({
    name: r.name as string,
    country: (r.country as string) ?? null,
    latitude: r.latitude as number,
    longitude: r.longitude as number,
  }));
}

async function nominatimForwardGeocode(cityName: string, lang = "he"): Promise<GeoResult | null> {
  try {
    const url = new URL(NOMINATIM_SEARCH_URL);
    url.searchParams.set("q", cityName);
    url.searchParams.set("format", "json");
    url.searchParams.set("accept-language", lang);
    url.searchParams.set("limit", "1");
    url.searchParams.set("addressdetails", "1");

    const response = await fetch(url.toString(), { headers: { "User-Agent": NOMINATIM_USER_AGENT } });
    if (!response.ok) return null;
    const results = await response.json();
    if (!Array.isArray(results) || results.length === 0) return null;

    const result = results[0];
    const address = result?.address ?? {};
    const name = address.city || address.town || address.village || address.municipality ||
      address.county || result?.name;
    const latitude = Number(result?.lat);
    const longitude = Number(result?.lon);
    if (!name || Number.isNaN(latitude) || Number.isNaN(longitude)) return null;
    return { name, country: address.country ?? null, latitude, longitude };
  } catch {
    return null;
  }
}

async function geocodeCity(cityName: string, lang = "he"): Promise<GeoResult | null> {
  const direct = await rawGeocodeSearch(cityName, 1, lang);
  if (direct.length > 0) return direct[0];

  const tokens = cityName.trim().split(/\s+/);
  for (const suffixLen of [1, 2, 3]) {
    if (tokens.length <= suffixLen) break;
    const cityPart = tokens.slice(0, -suffixLen).join(" ");
    const countryHint = tokens.slice(-suffixLen).join(" ");
    const candidates = await rawGeocodeSearch(cityPart, 10, lang);
    const match = candidates.find((c) => textMatches(countryHint, c.country));
    if (match) return match;
  }

  return await nominatimForwardGeocode(cityName, lang);
}

// -----------------------------------------------------------------------
// Open-Meteo
// -----------------------------------------------------------------------

/**
 * ה-utc_offset_seconds של lat/lon לפי ה-timezone שזוהה אוטומטית —
 * פורט של fetch_utc_offset_seconds מ-bot_commands.py. best-effort:
 * כשל -> 0 (UTC), אותה נפילה-בטוחה כמו שם.
 */
async function fetchUtcOffsetSeconds(lat: number, lon: number): Promise<number> {
  try {
    const url = new URL(OPEN_METEO_URL);
    url.searchParams.set("latitude", String(lat));
    url.searchParams.set("longitude", String(lon));
    url.searchParams.set("hourly", "uv_index");
    url.searchParams.set("forecast_days", "1");
    url.searchParams.set("timezone", "auto");

    const response = await fetch(url.toString());
    if (!response.ok) return 0;
    const data = await response.json();
    return typeof data?.utc_offset_seconds === "number" ? data.utc_offset_seconds : 0;
  } catch {
    return 0;
  }
}

async function fetchHistoricalUv(
  lat: number,
  lon: number,
  startTimeIso: string,
  endTimeIso: string,
): Promise<number | null> {
  const pastDays = pastDaysFor(startTimeIso);
  if (pastDays === null) return null;

  const url = new URL(OPEN_METEO_URL);
  url.searchParams.set("latitude", String(lat));
  url.searchParams.set("longitude", String(lon));
  url.searchParams.set("hourly", "uv_index");
  url.searchParams.set("past_days", String(pastDays));
  url.searchParams.set("forecast_days", "1");

  const response = await fetch(url.toString());
  if (!response.ok) return null;
  const data = await response.json();
  const times: string[] = data?.hourly?.time ?? [];
  const uvs: number[] = data?.hourly?.uv_index ?? [];
  return weightedAverageUv(times, uvs, startTimeIso, endTimeIso);
}

// -----------------------------------------------------------------------
// בניית שורת exposure_log מתוך קלט המשתמש — משותף ל-create ול-update.
// -----------------------------------------------------------------------

interface BuiltRow {
  row: Record<string, unknown>;
}

async function buildSessionRow(
  session: SessionPayload,
  username: string,
  skinType: number,
  lang: string = "he",
): Promise<BuiltRow | { error: "city_not_found" | "uv_unavailable" } | { message: string }> {
  const geo = await geocodeCity(session.city!.trim(), lang);
  if (!geo) return { error: "city_not_found" };

  const utcOffsetSeconds = await fetchUtcOffsetSeconds(geo.latitude, geo.longitude);
  const startRawIso = localWallClockToUtcIso(session.date!, session.start!, utcOffsetSeconds);
  const endRawIso = localWallClockToUtcIso(session.date!, session.end!, utcOffsetSeconds);
  if (!startRawIso || !endRawIso) return { message: "התאריך או השעות לא תקינים" };

  // בדיקת עתיד + חציית חצות + ולידציה, במקום אחד ובסדר הנכון —
  // ראו resolveSessionTimes ב-logic.ts לרציונל (כולל תקלת ה-AM/PM).
  const resolved = resolveSessionTimes(startRawIso, endRawIso);
  if ("message" in resolved) return { message: resolved.message };
  const { startIso, endIso } = resolved;

  const uvIndex = await fetchHistoricalUv(geo.latitude, geo.longitude, startIso, endIso);
  if (uvIndex === null) return { error: "uv_unavailable" };

  const durationMinutes = (new Date(endIso).getTime() - new Date(startIso).getTime()) / 60000;
  const spf = session.spf ?? null;
  const score = calculateExposureScore(uvIndex, durationMinutes, skinType, spf);

  return {
    row: {
      telegram_username: username,
      city: geo.name,
      country: geo.country,
      start_time: startIso,
      end_time: endIso,
      uv_index: uvIndex,
      lat: geo.latitude,
      lon: geo.longitude,
      spf,
      exposure_score: score,
      skin_type: skinType, // כמו _begin_session בבוט — הציון מחושב לפיו
    },
  };
}

// -----------------------------------------------------------------------
// Handler
// -----------------------------------------------------------------------

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return errorResponse("invalid_body");
  }

  let body: RequestBody;
  try {
    body = await req.json();
  } catch {
    return errorResponse("invalid_body");
  }

  const validationError = validateRequest(body);
  if (validationError) return errorResponse(validationError);

  try {
    // --- אימות הטוקן, בדיוק כמו dashboard-data ---
    const links = (await restGet(
      `magic_links?token=eq.${encodeURIComponent(body.token!)}&select=telegram_username,expires_at`,
    )) as { telegram_username: string; expires_at: string }[];

    if (links.length === 0) return errorResponse("invalid_token");
    const { telegram_username: username, expires_at } = links[0];
    if (isTokenExpired(expires_at, new Date())) return errorResponse("expired_token");

    const encodedUsername = encodeURIComponent(username);

    // --- קריאה: מחזירה את ה-session בשעון המקומי *של העיר*, לטופס העריכה ---
    // בלי זה, הדף היה צריך להמיר את ה-UTC השמור לפי ה-timezone של
    // הדפדפן — ולערוך שדה אחד היה מזיז בשקט את כל השעות כשהמשתמש נמצא
    // באזור זמן אחר מזה של ה-session (למשל session בתאילנד שנערך מהארץ).
    if (body.action === "read") {
      const rows = (await restGet(
        `exposure_log?id=eq.${body.id}&telegram_username=eq.${encodedUsername}` +
          `&select=id,city,start_time,end_time,spf,lat,lon`,
      )) as {
        id: number; city: string; start_time: string; end_time: string;
        spf: number | null; lat: number | null; lon: number | null;
      }[];
      if (rows.length === 0) return errorResponse("session_not_found");
      const row = rows[0];

      // שורות ישנות מלפני migration ה-lat/lon נופלות בבטחה ל-UTC,
      // בדיוק כמו fetch_utc_offset_seconds ב-bot_commands.py.
      const utcOffsetSeconds = (row.lat !== null && row.lon !== null)
        ? await fetchUtcOffsetSeconds(row.lat, row.lon)
        : 0;

      const start = utcIsoToLocalWallClock(row.start_time, utcOffsetSeconds);
      const end = utcIsoToLocalWallClock(row.end_time, utcOffsetSeconds);
      if (!start || !end) return errorResponse("server_error");

      return jsonResponse({
        id: row.id,
        city: row.city,
        date: start.date,
        start: start.time,
        end: end.time,
        spf: row.spf,
      }, 200);
    }

    // --- מחיקה: רק בדיקת בעלות, בלי geocoding/UV ---
    if (body.action === "delete") {
      const owned = (await restGet(
        `exposure_log?id=eq.${body.id}&telegram_username=eq.${encodedUsername}&select=id`,
      )) as { id: number }[];
      if (owned.length === 0) return errorResponse("session_not_found");

      await restWrite(
        `exposure_log?id=eq.${body.id}&telegram_username=eq.${encodedUsername}`,
        "DELETE",
      );
      return jsonResponse({ ok: true, id: body.id }, 200);
    }

    // --- create/update: צריך את סוג העור לחישוב הציון ---
    const users = (await restGet(
      `users?telegram_username=eq.${encodedUsername}&select=skin_type`,
    )) as { skin_type: number }[];
    if (users.length === 0) return errorResponse("not_onboarded");
    const skinType = users[0].skin_type;

    // update: מוודאים בעלות *לפני* כל עבודה חיצונית (geocoding/Open-Meteo).
    if (body.action === "update") {
      const owned = (await restGet(
        `exposure_log?id=eq.${body.id}&telegram_username=eq.${encodedUsername}&select=id`,
      )) as { id: number }[];
      if (owned.length === 0) return errorResponse("session_not_found");
    }

    const lang = normalizeLang(body.lang);
    const built = await buildSessionRow(body.session!, username, skinType, lang);
    if ("error" in built) return errorResponse(built.error);
    if ("message" in built) {
      return jsonResponse({ error: "invalid_session", message: localizeMessage(built.message, lang) }, 400);
    }

    if (body.action === "create") {
      await restWrite("exposure_log", "POST", built.row);
      return jsonResponse({ ok: true }, 200);
    }

    // update — telegram_username לא נכתב מחדש בכוונה: הוא כבר אומת למעלה
    // ואין שום תרחיש שבו עריכה אמורה להעביר session למשתמש אחר.
    const { telegram_username: _ignored, ...updatable } = built.row;
    await restWrite(
      `exposure_log?id=eq.${body.id}&telegram_username=eq.${encodedUsername}`,
      "PATCH",
      updatable,
    );
    return jsonResponse({ ok: true, id: body.id }, 200);
  } catch (err) {
    console.error("dashboard-sessions failed:", err);
    return errorResponse("server_error");
  }
});
