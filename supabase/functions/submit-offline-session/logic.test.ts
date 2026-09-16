// SunSafe — logic.ts unit tests ל-submit-offline-session.
//
// logic.ts טהור (בלי Deno.serve/Deno.env/fetch — crypto.subtle הוא Web
// API סטנדרטי, זמין גם תחת Node), אז אפשר להריץ תחת Node 22+:
//   node --experimental-strip-types --test logic.test.ts
// או עם Deno:
//   deno test logic.test.ts
//
// בדיקת initData מחשבת hash *באופן עצמאי* דרך node:crypto (לא קוראת
// ל-validateInitData כדי ליצור את ה-hash ואז מוודאת שהיא מקבלת את מה
// שהיא עצמה יצרה — זה היה מעגלי) — כדי לוודא שהאלגוריתם תואם בפועל
// לספק הרשמי של Telegram Mini Apps.
// ראה docs/2026-08-29-offline-session-miniapp-design.md סעיף 5-6.

import { test } from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  calculateExposureScore,
  errorResponse,
  nearestHourlyUv,
  pastDaysFor,
  pythonRound,
  validateInitData,
  validateSessionShape,
  weightedAverageUv,
} from "./logic.ts";

function computeInitDataHash(fields: Record<string, string>, botToken: string): string {
  const dataCheckString = Object.keys(fields).sort().map((k) => `${k}=${fields[k]}`).join("\n");
  const secretKey = crypto.createHmac("sha256", "WebAppData").update(botToken).digest();
  return crypto.createHmac("sha256", secretKey).update(dataCheckString).digest("hex");
}

const BOT_TOKEN = "123456:ABC-DEF_test_token";

test("validateInitData: correctly-signed initData is accepted, user is parsed", async () => {
  const user = { id: 999888777, first_name: "Gil", username: "gil612" };
  const fields = { query_id: "AAHdF6IQ", user: JSON.stringify(user), auth_date: "1735000000" };
  const hash = computeInitDataHash(fields, BOT_TOKEN);
  const initData = new URLSearchParams({ ...fields, hash }).toString();

  const result = await validateInitData(initData, BOT_TOKEN);
  assert.equal(result.valid, true);
  assert.equal(result.user?.username, "gil612");
  assert.equal(result.user?.id, 999888777);
});

test("validateInitData: tampered field is rejected", async () => {
  const fields = { auth_date: "1735000000", query_id: "abc" };
  const hash = computeInitDataHash(fields, BOT_TOKEN);
  const tampered = new URLSearchParams({ auth_date: "1735000001", query_id: "abc", hash }).toString();

  const result = await validateInitData(tampered, BOT_TOKEN);
  assert.equal(result.valid, false);
});

test("validateInitData: wrong bot token is rejected", async () => {
  const fields = { auth_date: "1735000000" };
  const hash = computeInitDataHash(fields, BOT_TOKEN);
  const initData = new URLSearchParams({ ...fields, hash }).toString();

  const result = await validateInitData(initData, "a-different-token");
  assert.equal(result.valid, false);
});

test("validateInitData: missing hash is rejected", async () => {
  const result = await validateInitData("auth_date=1735000000", BOT_TOKEN);
  assert.equal(result.valid, false);
});

test("validateInitData: empty initData is rejected", async () => {
  const result = await validateInitData("", BOT_TOKEN);
  assert.equal(result.valid, false);
});

test("calculateExposureScore: matches calculate_exposure_score (Python) — with SPF", () => {
  // factor(skin=3)=1.0, protection=1+(30-1)*0.4=12.6, safe=(200/8)*1*12.6=315, score=round(30/315*100)=10
  assert.equal(calculateExposureScore(8, 30, 3, 30), 10);
});

test("calculateExposureScore: matches calculate_exposure_score (Python) — no SPF", () => {
  // protection=1, safe=(200/8)*1=25, score=round(30/25*100)=120
  assert.equal(calculateExposureScore(8, 30, 3, null), 120);
});

test("calculateExposureScore: uv_index=0 returns 0 instead of dividing by zero", () => {
  assert.equal(calculateExposureScore(0, 120, 3, null), 0);
});

test("calculateExposureScore: unknown skin_type falls back to factor 1.0", () => {
  assert.equal(calculateExposureScore(8, 25, 99, null), calculateExposureScore(8, 25, 3, null));
});

test("pastDaysFor: within 92-day window returns a small positive integer", () => {
  const now = new Date("2026-08-29T12:00:00Z");
  const threeDaysAgo = new Date(now.getTime() - 3 * 86400000).toISOString();
  const result = pastDaysFor(threeDaysAgo, now);
  assert.ok(result !== null && result >= 3 && result <= 5, `expected ~4, got ${result}`);
});

test("pastDaysFor: older than 92 days returns null (unsupported)", () => {
  const now = new Date("2026-08-29T12:00:00Z");
  const tooOld = new Date(now.getTime() - 100 * 86400000).toISOString();
  assert.equal(pastDaysFor(tooOld, now), null);
});

test("pastDaysFor: start_time slightly in the future (clock skew) still returns a usable value", () => {
  const now = new Date("2026-08-29T12:00:00Z");
  const future = new Date(now.getTime() + 3600000).toISOString();
  const result = pastDaysFor(future, now);
  assert.ok(result !== null && result >= 0 && result <= 2, `expected small value, got ${result}`);
});

test("nearestHourlyUv: picks the closest hour's value", () => {
  const times = ["2026-08-26T10:00:00Z", "2026-08-26T11:00:00Z", "2026-08-26T12:00:00Z"];
  const uvs = [3.1, 5.5, 7.2];
  assert.equal(nearestHourlyUv(times, uvs, "2026-08-26T11:20:00Z"), 5.5);
});

test("nearestHourlyUv: empty arrays return null", () => {
  assert.equal(nearestHourlyUv([], [], "2026-08-26T11:20:00Z"), null);
});

// ---------------------------------------------------------------------
// weightedAverageUv — תיקון "דגימת UV בודדת" (מחליף nearestHourlyUv
// בפועל ב-index.ts). ראו logic.ts לרציונל המלא, כולל התקלה האמיתית
// ממצפה רמון ב-2026-09-08.
// ---------------------------------------------------------------------

test("weightedAverageUv: session fully within a single hourly bucket returns that bucket's UV", () => {
  const times = ["2026-08-26T10:00:00", "2026-08-26T11:00:00", "2026-08-26T12:00:00"];
  const uvs = [2.0, 5.0, 4.0];
  const result = weightedAverageUv(times, uvs, "2026-08-26T11:10:00Z", "2026-08-26T11:40:00Z");
  assert.equal(result, 5.0);
});

test("weightedAverageUv: session spanning two buckets weights each by overlap minutes", () => {
  const times = ["2026-08-26T10:00:00", "2026-08-26T11:00:00", "2026-08-26T12:00:00"];
  const uvs = [2.0, 8.0, 4.0];
  // 15 min in the 2.0 bucket + 15 min in the 8.0 bucket -> (2*15 + 8*15) / 30 = 5.0
  const result = weightedAverageUv(times, uvs, "2026-08-26T10:45:00Z", "2026-08-26T11:15:00Z");
  assert.equal(result, 5.0);
});

test("weightedAverageUv: long multi-hour session (Mitzpe Ramon-style) averages across the whole span, not just the start hour", () => {
  // session aligned exactly on hour boundaries (05:00-16:00, 11 full hours) so
  // every bucket contributes an equal 60-minute weight -> plain average of the
  // 11 daytime values. A 04:00 bucket with a very high UV sits just *outside*
  // the session and must be excluded entirely — this is exactly the real bug:
  // the old single-snapshot fetch could land on one hour (e.g. a near-zero
  // night reading) and miss the midday peak completely; the fix must reflect
  // the *whole* session, not overweight a lucky/unlucky single sample.
  const times = [
    "2026-08-26T04:00:00", // outside session -> must be excluded
    "2026-08-26T05:00:00", "2026-08-26T06:00:00", "2026-08-26T07:00:00",
    "2026-08-26T08:00:00", "2026-08-26T09:00:00", "2026-08-26T10:00:00",
    "2026-08-26T11:00:00", "2026-08-26T12:00:00", "2026-08-26T13:00:00",
    "2026-08-26T14:00:00", "2026-08-26T15:00:00",
  ];
  const uvs = [9.9, 0, 0, 1, 3, 6, 8, 7, 5, 2, 0, 0];
  const daytimeUvs = uvs.slice(1); // exclude the 04:00 outlier
  const expected = daytimeUvs.reduce((a, b) => a + b, 0) / daytimeUvs.length;

  const result = weightedAverageUv(times, uvs, "2026-08-26T05:00:00Z", "2026-08-26T16:00:00Z");
  assert.ok(result !== null && Math.abs(result - expected) < 1e-9, `expected ~${expected}, got ${result}`);
  assert.ok(result! > 2.9, "weighted average should reflect the real midday peak, not read near zero");
});

test("weightedAverageUv: null UV buckets are skipped, not treated as zero", () => {
  const times = ["2026-08-26T09:00:00", "2026-08-26T10:00:00"];
  const uvs: (number | null)[] = [null, 5.0];
  // 30 min overlap with each bucket, but the null bucket contributes no weight
  const result = weightedAverageUv(times, uvs, "2026-08-26T09:30:00Z", "2026-08-26T10:30:00Z");
  assert.equal(result, 5.0);
});

test("weightedAverageUv: no overlap between session and any bucket returns null", () => {
  const times = ["2026-08-26T09:00:00"];
  const uvs = [5.0];
  const result = weightedAverageUv(times, uvs, "2026-08-27T09:00:00Z", "2026-08-27T10:00:00Z");
  assert.equal(result, null);
});

test("weightedAverageUv: empty arrays return null", () => {
  assert.equal(weightedAverageUv([], [], "2026-08-26T09:00:00Z", "2026-08-26T10:00:00Z"), null);
});

test("validateSessionShape: valid session passes", () => {
  const session = {
    client_uuid: "abc-123",
    start_time: "2026-08-29T06:00:00Z",
    start_lat: 32.7940, start_lon: 34.9896,
    end_time: "2026-08-29T07:00:00Z",
    end_lat: 32.8000, end_lon: 35.0000,
    spf: 30,
  };
  assert.equal(validateSessionShape(session), null);
});

test("validateSessionShape: end_time before start_time is rejected", () => {
  const session = {
    client_uuid: "abc-123",
    start_time: "2026-08-29T07:00:00Z",
    start_lat: 32.79, start_lon: 34.98,
    end_time: "2026-08-29T06:00:00Z",
    end_lat: 32.80, end_lon: 35.00,
    spf: null,
  };
  assert.equal(typeof validateSessionShape(session), "string");
});

test("validateSessionShape: out-of-range latitude is rejected", () => {
  const session = {
    client_uuid: "abc-123",
    start_time: "2026-08-29T06:00:00Z",
    start_lat: 999, start_lon: 34.98,
    end_time: "2026-08-29T07:00:00Z",
    end_lat: 32.80, end_lon: 35.00,
    spf: null,
  };
  assert.equal(typeof validateSessionShape(session), "string");
});

test("errorResponse: correct status per error code", async () => {
  const cases = [
    ["invalid_body", 400],
    ["invalid_init_data", 401],
    ["missing_username", 400],
    ["not_onboarded", 404],
    ["server_error", 500],
  ] as const;
  for (const [code, status] of cases) {
    const res = errorResponse(code);
    assert.equal(res.status, status);
    assert.deepEqual(await res.json(), { error: code });
    assert.equal(res.headers.get("Access-Control-Allow-Origin"), "*");
  }
});

// -----------------------------------------------------------------------
// parity מול פייתון — לא מול ציפיות שכתבנו ביד
// -----------------------------------------------------------------------
// הבדיקות הידניות למעלה הן בדיוק הסיבה שהבאג הזה שרד: Math.round של JS
// מעגל חצי-למעלה ו-round() של פייתון מעגל חצי-לזוגי, ואף אחד מהמקרים
// שבחרנו ביד לא נפל בדיוק על .5. בפועל 626 מתוך 108,360 הקומבינציות
// (0.58%) יצאו שונות — כלומר משתמש שקיבל מספר אחד מהבוט בטלגרם וראה
// מספר אחר בדשבורד, על אותו session.
//
// ה-fixture נוצר ישירות מ-geo_uv_core.py, ומשותף עם ה-Worker:
//   python cloudflare/sunset-worker/scripts/gen_fixture.py

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(
  readFileSync(join(here, "../../../cloudflare/sunset-worker/src/fixture.json"), "utf8"),
) as { scores: { uv: number; skin: number; spf: number | null; duration: number; score: number }[] };

test("exposure score matches the Python implementation, case for case", () => {
  const mismatches: string[] = [];
  for (const c of fixture.scores) {
    const got = calculateExposureScore(c.uv, c.duration, c.skin, c.spf);
    if (got !== c.score) {
      mismatches.push(
        `uv=${c.uv} skin=${c.skin} spf=${c.spf} dur=${c.duration}: ts=${got} py=${c.score}`,
      );
    }
  }
  assert.deepEqual(mismatches, [], `${mismatches.length} of ${fixture.scores.length} differ from Python`);
});

test("pythonRound rounds halves to even, where Math.round rounds up", () => {
  assert.equal(pythonRound(0.5), 0);      // Math.round -> 1
  assert.equal(pythonRound(1.5), 2);
  assert.equal(pythonRound(2.5), 2);      // Math.round -> 3
  assert.equal(pythonRound(3.5), 4);
  assert.equal(pythonRound(-0.5), 0);
  assert.equal(pythonRound(-1.5), -2);    // Math.round -> -1
  assert.equal(pythonRound(2.500001), 3); // מעל חצי — למעלה, בלי קשר לזוגיות
  assert.equal(pythonRound(7), 7);
});
