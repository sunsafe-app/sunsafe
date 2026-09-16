// SunSafe — sunset auto-close Worker: הלוגיקה הטהורה
// =====================================================================
// כל מה שכאן הוא פונקציות טהורות, בלי fetch/env/Date.now — בדיוק כמו
// logic.ts של ה-Edge Functions הקיימות, ומאותה סיבה: כדי שאפשר יהיה
// להריץ בדיקות תחת Node בלי wrangler ובלי רשת:
//   node --experimental-strip-types --test src/logic.test.ts
//
// למה ה-Worker הזה קיים
// ---------------------
// עד עכשיו הסגירה האוטומטית בשקיעה רצתה כ-thread בתוך הבוט
// (auto_close_expired_sessions_forever ב-bot_commands.py), בטיק של
// 5 דקות. זו בדיוק צורת ה-Cron Trigger, וזה גם מוציא thread שלם
// מתהליך שכבר מריץ לולאת polling יחידה.
//
// **הנוסחה כאן חייבת להיות זהה לפייתון, לא "דומה".**
// המספרים האלה נשמרים ב-exposure_log ומוצגים בדשבורד, ואם שתי
// המימושים יתפצלו יהיו לנו שני היסטוריות חשיפה שונות לאותו משתמש.
// logic.test.ts משווה את הפלט מול fixture שנוצר ישירות מהפייתון
// (scripts/gen_fixture.py) — לא מול ציפיות שכתבנו ביד.
//
// SKIN_TYPE_FACTOR / effectiveSpf / calculateExposureScore /
// weightedAverageUv / pastDaysFor זהים ל-dashboard-sessions/logic.ts.
// המטרה לטווח הקרוב היא שכולם ייבאו מכאן במקום לשכפל; ראו README.

export const SKIN_TYPE_FACTOR: Record<number, number> = {
  1: 0.5, 2: 0.75, 3: 1.0, 4: 1.5, 5: 2.5, 6: 4.0,
};

export function effectiveSpf(labeledSpf: number | null | undefined): number {
  if (!labeledSpf) return 1;
  return 1 + (labeledSpf - 1) * 0.4;
}

/** דקות בשמש ישירה עד סיכון לכוויה. null כשאין משמעות למספר (UV אפס). */
export function safeExposureMinutes(
  uvIndex: number,
  skinType: number,
  spf: number | null | undefined = null,
): number | null {
  if (uvIndex <= 0) return null;
  const factor = SKIN_TYPE_FACTOR[skinType] ?? 1.0;
  return (200 / uvIndex) * factor * effectiveSpf(spf);
}

export function calculateExposureScore(
  uvIndex: number,
  durationMinutes: number,
  skinType: number,
  spf: number | null | undefined,
): number {
  const safeMinutes = safeExposureMinutes(uvIndex, skinType, spf);
  if (safeMinutes === null) return 0;
  // pythonRound ולא Math.round — ראו ההערה שם. הבדיקה מול ה-fixture
  // תפסה 9 מתוך 720 צירופים שנפלו על .5 מדויק והחזירו ציון שונה ב-1
  // מהפייתון. **אותו הפרש קיים כרגע ב-dashboard-sessions/logic.ts**,
  // שמשתמש ב-Math.round — ראו README.
  return pythonRound((durationMinutes / safeMinutes) * 100);
}

// ---------------------------------------------------------------------
// פייתון מעגל עם round() (banker's rounding: 0.5 -> למספר הזוגי),
// ו-JS מעגל עם Math.round (0.5 -> תמיד למעלה). ההפרש נראה רק על .5
// מדויק, אבל הוא כן קיים — ועל מספרים שנשמרים ב-DB לא מהמרים על
// "כנראה לא יקרה". הפונקציה הזו משחזרת את ההתנהגות של פייתון.
// ---------------------------------------------------------------------
export function pythonRound(value: number): number {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** דקות -> טקסט עברי טבעי. זהה ל-format_duration_he בפייתון. */
export function formatDurationHe(minutes: number): string {
  const total = pythonRound(minutes);
  if (total < 60) return `כ-${total} דקות`;

  const hours = Math.floor(total / 60);
  const mins = total % 60;
  const head = hours === 1 ? "כשעה" : hours === 2 ? "כשעתיים" : `כ-${hours} שעות`;
  return mins < 5 ? head : `${head} ו-${mins} דקות`;
}

/**
 * הודעת סיום ה-session — **חייבת להיות זהה למה שהבוט שולח ב-/end_session**.
 *
 * הסגירה בשקיעה נועדה במפורש להיראות למשתמש כמו סגירה ידנית: אותו
 * נוסח בדיוק, כדי שלא ייראה כאילו שני מנגנונים שונים מדברים אליו.
 * הטקסט בפייתון נמצא ב-handle_end_session, ו-logic.test.ts משווה מול
 * fixture שנוצר משם.
 */
// ---------------------------------------------------------------------
// סיכום יומי — פורט מדויק מ-geo_uv_core.py
// ---------------------------------------------------------------------
// נוסף 16.9.2026, יחד עם הבלוק היומי בהודעת הסיום. **הפורט הזה הוא
// חובה ולא נוחות:** ה-Worker שולח את *אותה* הודעה כמו הבוט, ויש כאן
// בדיקה שמשווה אותה תו-בתו מול fixture שנוצר מהפייתון. אם הבוט מוסיף
// בלוק וה-Worker לא, משתמש שה-session שלו נסגר בשקיעה יקבל הודעה אחרת
// ממי שסגר ידנית — וזה בדיוק מה שה-fixture נועד למנוע.
//
// אורך הסרגל והבאנדים (40/70/100) זהים ל-score_to_level ולצבעי
// הדשבורד. מעל 100% הסרגל כולו אדום.

export const DAILY_BAR_CELLS = 10;

const BAR_GREEN_CELLS = 4;
const BAR_YELLOW_CELLS = 7;

export function scoreToLevel(score: number): "good" | "warning" | "serious" | "critical" {
  if (score < 40) return "good";
  if (score < 70) return "warning";
  if (score < 100) return "serious";
  return "critical";
}

export function dailyExposureScore(scores: (number | null | undefined)[]): number {
  return scores.reduce<number>((sum, n) => sum + (n ?? 0), 0);
}

export function exposureBar(score: number, cells: number = DAILY_BAR_CELLS): string {
  if (score >= 100) return "🟥".repeat(cells);

  const filled = score > 0
    ? Math.min(cells, Math.max(1, Math.floor(score / (100 / cells))))
    : 0;

  let out = "";
  for (let i = 0; i < cells; i++) {
    if (i >= filled) out += "⬜";
    else if (i < BAR_GREEN_CELLS) out += "🟩";
    else if (i < BAR_YELLOW_CELLS) out += "🟨";
    else out += "🟧";
  }
  return out;
}

const DAILY_HEADLINES = {
  good: "בטווח הבטוח.",
  warning: "בטווח הבינוני.",
  serious: "בטווח הגבוה.",
  critical: "חשיפה מלאה — עברתם את התקציב היומי.",
} as const;

export function dailySummaryHe(
  dayScore: number,
  sessionCount: number,
  totalMinutes: number,
  peakCity?: string | null,
  peakScore?: number | null,
): string {
  let headline: string = DAILY_HEADLINES[scoreToLevel(dayScore)];
  if (dayScore < 100) headline += ` עוד ${100 - dayScore}% עד חשיפה מלאה.`;

  const minutes = pythonRound(totalMinutes);
  const plural = sessionCount !== 1 ? "sessions" : "session";
  const lines = [
    `${exposureBar(dayScore)}  ${dayScore}%`,
    headline,
    "",
    `היום: ${sessionCount} ${plural} · ${minutes} דקות בשמש`,
  ];
  if (peakCity && peakScore !== null && peakScore !== undefined && sessionCount > 1) {
    lines.push(`הגבוה מביניהם: ${peakCity}, ${peakScore}%`);
  }
  return lines.join("\n");
}

export function buildCompletionMessage(params: {
  durationMinutes: number;
  city: string;
  score: number;
  uvIndex: number;
  uvIsAverage: boolean;
  skinType: number;
  spf: number | null | undefined;
  // אופציונלי: כשה-Worker לא הצליח לשלוף את שאר ה-sessions של היום,
  // ההודעה נשלחת בלי הבלוק היומי במקום לא להישלח בכלל.
  daily?: {
    score: number;
    sessionCount: number;
    totalMinutes: number;
    peakCity?: string | null;
    peakScore?: number | null;
  } | null;
}): string {
  const { durationMinutes, city, score, uvIndex, uvIsAverage, skinType, spf, daily } = params;
  const budget = safeExposureMinutes(uvIndex, skinType, spf);
  const uvLabel = uvIsAverage ? "UV ממוצע" : "UV";

  const budgetPart = budget
    ? `\nמדד חשיפה: ${score}% — ${pythonRound(durationMinutes)} מתוך ` +
      `${pythonRound(budget)} הדקות המותרות לכם ב-${uvLabel} ${uvIndex.toFixed(1)}.`
    : `\nמדד חשיפה: ${score}%.`;

  const dailyPart = daily
    ? "\n\n" + dailySummaryHe(
        daily.score, daily.sessionCount, daily.totalMinutes, daily.peakCity, daily.peakScore,
      )
    : "";

  return (
    `${pythonRound(durationMinutes)} דקות ב${city}.` +
    `${budgetPart}` +
    `${dailyPart}\n\n` +
    "כדי לראות את הנתונים באזור האישי — לחצו\n" +
    "/dashboard"
  );
}

// ---------------------------------------------------------------------
// UV משוקלל-משך — פורט מדויק מ-dashboard-sessions/logic.ts
// ---------------------------------------------------------------------
export function pastDaysFor(startTimeIso: string, now: Date = new Date()): number | null {
  const start = new Date(startTimeIso);
  const diffDays = Math.floor((now.getTime() - start.getTime()) / 86_400_000);
  if (diffDays < 0 || diffDays > 92) return null;
  return diffDays;
}

export function weightedAverageUv(
  hourlyTimes: string[],
  hourlyUv: (number | null)[],
  startTime: Date,
  endTime: Date,
): number | null {
  if (endTime.getTime() <= startTime.getTime()) return null;

  let weightedSum = 0;
  let totalWeight = 0;
  for (let i = 0; i < hourlyTimes.length; i++) {
    const uv = hourlyUv[i];
    if (uv === null || uv === undefined) continue;

    const bucketStart = new Date(`${hourlyTimes[i]}Z`);
    const bucketEnd = new Date(bucketStart.getTime() + 3_600_000);
    const overlapMs =
      Math.min(endTime.getTime(), bucketEnd.getTime()) -
      Math.max(startTime.getTime(), bucketStart.getTime());
    if (overlapMs <= 0) continue;

    weightedSum += uv * overlapMs;
    totalWeight += overlapMs;
  }

  if (totalWeight === 0) return null;
  return weightedSum / totalWeight;
}

// ---------------------------------------------------------------------
// שקיעה
// ---------------------------------------------------------------------
/**
 * האם עבר זמן השקיעה של אותו מקום.
 *
 * Open-Meteo מחזיר sunset בשעון מקומי של המקום (timezone=auto) יחד עם
 * utc_offset_seconds — ההמרה ל-UTC היא באחריותנו. זה בדיוק המקום שבו
 * הפייתון נכשל פעם עם הנחה ש-HH:MM הוא כבר UTC (2026-09-08).
 */
export function sunsetToUtc(sunsetLocalIso: string, utcOffsetSeconds: number): Date | null {
  // "2026-09-16T18:42" — בלי אזור זמן, ולכן מפרשים כ-UTC ומחסרים את ההיסט.
  const asUtc = new Date(`${sunsetLocalIso}Z`);
  if (Number.isNaN(asUtc.getTime())) return null;
  return new Date(asUtc.getTime() - utcOffsetSeconds * 1000);
}

export function isPastSunset(now: Date, sunsetUtc: Date | null): boolean {
  if (sunsetUtc === null) return false; // לא הצלחנו לקבוע — לא נוגעים
  return now.getTime() >= sunsetUtc.getTime();
}
