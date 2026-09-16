// SunSafe — Dashboard Session CRUD Edge Function: pure logic
// (בלי Deno.serve/Deno.env/fetch — קל לבדוק בלי תלות ברשת/סביבה).
//
// אותה קונבנציה כמו dashboard-data/logic.ts ו-submit-offline-session/logic.ts:
// כל Edge Function self-contained, בלי ספריית קוד משותפת בין הפונקציות.
// הכפילות (CORS_HEADERS, jsonResponse, calculateExposureScore,
// weightedAverageUv, pastDaysFor) מכוונת ועקבית עם מה שכבר קיים כאן.
//
// נוצרה 2026-09-12: הוספה/עריכה/מחיקה של sessions עברו מהבוט לדשבורד
// (/add_session, /edit_session, /delete_session הוסרו מ-bot_commands.py).
// החוזה מכוון להיות *התאמה מדויקת* להתנהגות שהייתה בפקודות האלה —
// במיוחד: שעות מתפרשות כשעון מקומי של העיר (לא UTC ולא שעון הדפדפן),
// וה-UV והציון מחושבים תמיד בשרת ולעולם לא מתקבלים מהלקוח.

export const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type",
};

export type ErrorCode =
  | "invalid_body"
  | "missing_token"
  | "invalid_token"
  | "expired_token"
  | "not_onboarded"
  | "city_not_found"
  | "uv_unavailable"
  | "session_not_found"
  | "invalid_session"
  | "server_error";

const ERROR_STATUS: Record<ErrorCode, number> = {
  invalid_body: 400,
  missing_token: 400,
  invalid_token: 404,
  expired_token: 410,
  not_onboarded: 404,
  city_not_found: 422,
  uv_unavailable: 422,
  session_not_found: 404,
  invalid_session: 400,
  server_error: 500,
};

export function jsonResponse(
  body: unknown,
  status: number,
  extraHeaders: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...CORS_HEADERS,
      ...extraHeaders,
    },
  });
}

export function errorResponse(code: ErrorCode): Response {
  return jsonResponse({ error: code }, ERROR_STATUS[code]);
}

/**
 * true אם ה-token פג תוקף. `now` מוזרק (לא Date.now() פנימי) כדי שהבדיקה
 * תישאר דטרמיניסטית — זהה ל-dashboard-data/logic.ts.
 */
export function isTokenExpired(expiresAtIso: string, now: Date): boolean {
  return new Date(expiresAtIso).getTime() <= now.getTime();
}

// -----------------------------------------------------------------------
// Exposure score — פורט מדויק של calculate_exposure_score ב-geo_uv_core.py.
// כל שינוי כאן חייב להסתנכרן גם שם וגם ב-submit-offline-session/logic.ts.
// -----------------------------------------------------------------------

export const SKIN_TYPE_FACTOR: Record<number, number> = {
  1: 0.5, 2: 0.75, 3: 1.0, 4: 1.5, 5: 2.5, 6: 4.0,
};

export function effectiveSpf(labeledSpf: number | null | undefined): number {
  if (!labeledSpf) return 1;
  return 1 + (labeledSpf - 1) * 0.4;
}

/**
 * עיגול בסגנון Python — חצי-לזוגי (banker's rounding), לא חצי-למעלה.
 * Math.round(0.5) הוא 1 ו-Math.round(2.5) הוא 3, אבל round() בפייתון
 * מחזיר 0 ו-2. הבוט מחשב את הניקוד בפייתון והדשבורד חישב אותו ב-JS,
 * ומתוך 108,360 קומבינציות שנבדקו 626 (0.58%) יצאו שונות בנקודה אחת —
 * כלומר ניקוד שהמשתמש רואה בדשבורד וסותר את מה שהבוט אמר לו בטלגרם.
 * אותו מימוש יושב ב-cloudflare/sunset-worker/src/logic.ts.
 */
export function pythonRound(value: number): number {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

export function calculateExposureScore(
  uvIndex: number,
  durationMinutes: number,
  skinType: number,
  spf: number | null | undefined,
): number {
  // UV=0 תקין לגמרי (session לילי) — 200/uvIndex עם 0 היה נותן Infinity/NaN.
  if (uvIndex <= 0) return 0;
  const factor = SKIN_TYPE_FACTOR[skinType] ?? 1.0;
  const protection = effectiveSpf(spf);
  const safeMinutes = (200 / uvIndex) * factor * protection;
  return pythonRound((durationMinutes / safeMinutes) * 100);
}

// -----------------------------------------------------------------------
// UV היסטורי — פורט מדויק מ-submit-offline-session/logic.ts (שם הרציונל
// המלא, כולל תקלת מצפה רמון מ-2026-09-08 שהחליפה דגימה בודדת
// בממוצע-משוקלל-משך).
// -----------------------------------------------------------------------

/** כמה ימים אחורה (past_days) צריך לבקש מ-Open-Meteo כדי לכסות startTimeIso. */
export function pastDaysFor(startTimeIso: string, now: Date = new Date()): number | null {
  const start = new Date(startTimeIso);
  const diffDays = Math.ceil((now.getTime() - start.getTime()) / (24 * 60 * 60 * 1000)) + 1;
  if (diffDays < 0) return 0;
  if (diffDays > 92) return null; // מעבר לטווח שה-forecast API מחזיק
  return diffDays;
}

/** ממוצע UV משוקלל-משך על פני [startTimeIso, endTimeIso). */
export function weightedAverageUv(
  hourlyTimes: string[],
  hourlyUv: (number | null)[],
  startTimeIso: string,
  endTimeIso: string,
): number | null {
  if (!hourlyTimes || !hourlyUv || hourlyTimes.length === 0) return null;
  const start = new Date(startTimeIso).getTime();
  const end = new Date(endTimeIso).getTime();
  const hourMs = 60 * 60 * 1000;

  let totalWeight = 0;
  let weightedSum = 0;
  for (let i = 0; i < hourlyTimes.length; i++) {
    const uv = hourlyUv[i];
    if (typeof uv !== "number") continue;
    const bucketStart = new Date(hourlyTimes[i]).getTime();
    const bucketEnd = bucketStart + hourMs;
    const overlapStart = Math.max(bucketStart, start);
    const overlapEnd = Math.min(bucketEnd, end);
    const overlapMs = overlapEnd - overlapStart;
    if (overlapMs <= 0) continue;
    weightedSum += uv * overlapMs;
    totalWeight += overlapMs;
  }
  if (totalWeight <= 0) return null;
  return weightedSum / totalWeight;
}

// -----------------------------------------------------------------------
// Geocoding helper — פורט של _text_matches מ-geo_uv_core.py, לשכבת
// "עיר + רמז מדינה" (ראו geocodeCity ב-index.ts).
// -----------------------------------------------------------------------

/** התאמת טקסט "רכה" בין רמז-מדינה שהמשתמש הקליד לשדה country מ-Open-Meteo. */
export function textMatches(hint: string, value: string | null | undefined): boolean {
  if (!value || !hint) return false;
  const normalize = (s: string) => s.trim().replace(/-/g, " ").replace(/["״]/g, "");
  const hintN = normalize(hint);
  const valueN = normalize(value);
  return Boolean(hintN) && (valueN.includes(hintN) || hintN.includes(valueN));
}

// -----------------------------------------------------------------------
// המרת שעון מקומי -> UTC
// -----------------------------------------------------------------------

/**
 * מקבלת תאריך ושעה כפי שהמשתמש הקליד אותם (שעון מקומי *של העיר*), ואת
 * ה-utcOffsetSeconds של אותה עיר, ומחזירה ISO ב-UTC.
 *
 * זו בדיוק ההתנהגות של /add_session ו-/edit_session אחרי התיקון מ-
 * 2026-09-08 (fetch_utc_offset_seconds ב-bot_commands.py): מי שכותב
 * "מצפה רמון 05:00" מתכוון לחמש בבוקר *שם*, לא ב-UTC ולא בשעון הדפדפן
 * שלו. הדשבורד בכוונה לא משתמש ב-timezone של הדפדפן — משתמש שמדווח
 * על טיול בתאילנד מהסלון שלו בארץ היה מקבל אחרת שעות שגויות.
 */
export function localWallClockToUtcIso(
  dateStr: string,
  timeStr: string,
  utcOffsetSeconds: number,
): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) return null;
  if (!/^\d{1,2}:\d{2}$/.test(timeStr)) return null;

  const [year, month, day] = dateStr.split("-").map(Number);
  const [hour, minute] = timeStr.split(":").map(Number);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  if (hour > 23 || minute > 59) return null;

  // Date.UTC מפרש את המספרים כ-UTC; מחסרים את ה-offset כדי לקבל את
  // הרגע האמיתי (local = UTC + offset  =>  UTC = local - offset).
  const asIfUtc = Date.UTC(year, month - 1, day, hour, minute, 0, 0);
  const real = new Date(asIfUtc - utcOffsetSeconds * 1000);
  if (Number.isNaN(real.getTime())) return null;
  return real.toISOString();
}

// -----------------------------------------------------------------------
// Request shapes + validation
// -----------------------------------------------------------------------

export type SessionAction = "read" | "create" | "update" | "delete";

export interface SessionPayload {
  /** שם עיר חופשי, כמו הארגומנט של /add_session. */
  city?: string;
  /** YYYY-MM-DD — התאריך בשעון המקומי של העיר. */
  date?: string;
  /** HH:MM — שעון מקומי של העיר. */
  start?: string;
  /** HH:MM — שעון מקומי של העיר. */
  end?: string;
  /** SPF כמספר, או null/חסר אם לא נעשה שימוש בקרם הגנה. */
  spf?: number | null;
}

export interface RequestBody {
  token?: string;
  action?: SessionAction;
  /** id של שורה ב-exposure_log — נדרש ל-update/delete בלבד. */
  id?: number;
  session?: SessionPayload;
}

/** ולידציה זולה של גוף הבקשה, לפני כל I/O (DB/geocoding/Open-Meteo). */
export function validateRequest(body: RequestBody): ErrorCode | null {
  if (!body || typeof body !== "object") return "invalid_body";
  if (typeof body.token !== "string" || !body.token) return "missing_token";
  const actions: SessionAction[] = ["read", "create", "update", "delete"];
  if (!actions.includes(body.action as SessionAction)) {
    return "invalid_body";
  }

  // read/delete עובדים על id בלבד, בלי גוף session.
  if (body.action === "delete" || body.action === "read") {
    return Number.isInteger(body.id) ? null : "invalid_body";
  }
  if (body.action === "update" && !Number.isInteger(body.id)) {
    return "invalid_body";
  }

  const s = body.session;
  if (!s || typeof s !== "object") return "invalid_body";
  return validateSessionPayload(s);
}

/**
 * ולידציה של תוכן ה-session עצמו. create ו-update שניהם דורשים את כל
 * השדות: העריכה בדשבורד היא טופס מלא (הערכים הקיימים כבר טעונים בו),
 * לא patch חלקי — כך אין מצב עמום של "איזה שדה באמת השתנה", וה-UV
 * והציון תמיד מחושבים מחדש משורה שלמה ועקבית.
 */
export function validateSessionPayload(s: SessionPayload): ErrorCode | null {
  if (typeof s.city !== "string" || !s.city.trim()) return "invalid_session";
  if (typeof s.date !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(s.date)) return "invalid_session";
  if (typeof s.start !== "string" || !/^\d{1,2}:\d{2}$/.test(s.start)) return "invalid_session";
  if (typeof s.end !== "string" || !/^\d{1,2}:\d{2}$/.test(s.end)) return "invalid_session";
  if (s.spf !== null && s.spf !== undefined) {
    if (typeof s.spf !== "number" || !Number.isInteger(s.spf) || s.spf < 1 || s.spf > 100) {
      return "invalid_session";
    }
  }
  return null;
}

/**
 * בדיקות שדורשות את הזמנים אחרי ההמרה ל-UTC (כלומר אחרי geocoding).
 * מחזירה הודעת שגיאה בעברית להצגה למשתמש, או null אם תקין.
 *
 * חציית חצות מטופלת *לפני* הקריאה לכאן (ראו resolveTimes ב-index.ts):
 * end מוקדם מ-start פירושו שה-session נמשך אל תוך היום הבא, בדיוק כמו
 * ב-/add_session.
 */
export function validateResolvedTimes(
  startIso: string,
  endIso: string,
  now: Date = new Date(),
): string | null {
  const start = new Date(startIso).getTime();
  const end = new Date(endIso).getTime();
  if (Number.isNaN(start) || Number.isNaN(end)) return "התאריך או השעות לא תקינים";
  if (end <= start) return "שעת הסיום חייבת להיות אחרי שעת ההתחלה";

  const durationMs = end - start;
  if (durationMs > 24 * 60 * 60 * 1000) {
    return "session ארוך מדי (מעל 24 שעות) — כנראה טעות בנתונים";
  }
  // חלון סבילות קטן לשעון לא מסונכרן בין הדפדפן לשרת.
  if (end > now.getTime() + 5 * 60 * 1000) {
    return "שעת הסיום לא יכולה להיות בעתיד";
  }
  if (pastDaysFor(startIso, now) === null) {
    return "אפשר לרשום רק sessions מ-92 הימים האחרונים (זה הטווח שבו יש נתוני UV היסטוריים)";
  }
  return null;
}

/** חלון סבילות לשעון לא מסונכרן בין המכשיר לשרת. */
const CLOCK_SKEW_MS = 5 * 60 * 1000;

/**
 * מקבלת את שני הזמנים אחרי ההמרה ל-UTC ומחזירה את הטווח הסופי, או
 * הודעת שגיאה בעברית להצגה למשתמש.
 *
 * כאן מרוכזות שלוש ההחלטות שהיו מפוזרות קודם (בדיקת עתיד, חציית חצות,
 * ולידציה) — כי הן תלויות זו בזו, ובגרסה הראשונה הסדר ביניהן הפיק
 * הודעה שמצביעה על השדה הלא נכון. המקרה האמיתי שחשף את זה, 2026-09-12:
 * משתמש הזין בטלפון start=10:00 PM ו-end=12:29 PM (התכוון ל-10:00 AM —
 * בלבול נפוץ בבוחר השעה של אנדרואיד). הקוד הסיק "חצה חצות", הזיז את
 * הסיום ליום הבא, וענה "שעת הסיום לא יכולה להיות בעתיד" — נכון
 * טכנית, אבל שולח את המשתמש לתקן בדיוק את השדה שלא היה בעייתי.
 *
 * לכן: קודם בודקים את *ההתחלה* (אם היא בעתיד, זו הבעיה האמיתית והיא
 * מוסברת ישירות), ורק אחר כך שוקלים חציית חצות — ומיישמים אותה רק אם
 * התוצאה לא נופלת בעתיד. session שאחרי ההזזה מסתיים "מחר" הוא כמעט
 * תמיד טעות AM/PM ולא שהייה שנמשכה אל תוך הלילה.
 */
export function resolveSessionTimes(
  startIso: string,
  endRawIso: string,
  now: Date = new Date(),
): { startIso: string; endIso: string } | { message: string } {
  const start = new Date(startIso).getTime();
  const endRaw = new Date(endRawIso).getTime();
  if (Number.isNaN(start) || Number.isNaN(endRaw)) {
    return { message: "התאריך או השעות לא תקינים" };
  }

  const latestAllowed = now.getTime() + CLOCK_SKEW_MS;

  if (start > latestAllowed) {
    return {
      message: "שעת ההתחלה שהזנתם היא בעתיד. בדקו את התאריך, " +
        "ואם בוחר השעה שלכם מציג AM/PM — שגם זה נבחר נכון.",
    };
  }

  let endIso = endRawIso;
  if (endRaw <= start) {
    const shifted = endRaw + 24 * 60 * 60 * 1000;
    if (shifted > latestAllowed) {
      return {
        message: "שעת הסיום מוקדמת משעת ההתחלה. אם ה-session לא חצה חצות, " +
          "בדקו את AM/PM בשתי השעות.",
      };
    }
    endIso = new Date(shifted).toISOString();
  }

  const error = validateResolvedTimes(startIso, endIso, now);
  if (error) return { message: error };
  return { startIso, endIso };
}

/**
 * ההפך של localWallClockToUtcIso — ממירה רגע UTC בחזרה לשעון הקיר
 * המקומי של אותה עיר, ומחזירה {date, time} כמחרוזות לטופס.
 *
 * זה מה שמשרת את action="read": כשפותחים עריכה, הטופס חייב להציג את
 * אותן שעות שהמשתמש היה מקליד בעצמו (שעון העיר), אחרת עריכה של שדה
 * אחד הייתה מזיזה בשקט את שאר השעות לפי ה-timezone של הדפדפן — בדיוק
 * סוג הבאג ש-fetch_utc_offset_seconds תיקן ב-/add_session ב-2026-09-08.
 */
export function utcIsoToLocalWallClock(
  iso: string,
  utcOffsetSeconds: number,
): { date: string; time: string } | null {
  const ms = new Date(iso).getTime();
  if (Number.isNaN(ms)) return null;
  const shifted = new Date(ms + utcOffsetSeconds * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    date: `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}`,
    time: `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}`,
  };
}
