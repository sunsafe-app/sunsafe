// SunSafe — logic.ts unit tests ל-dashboard-sessions.
//
// logic.ts טהור (בלי Deno.serve/Deno.env/fetch), אז אפשר להריץ תחת Node 22+:
//   node --experimental-strip-types --test logic.test.ts
// או עם Deno:
//   deno test logic.test.ts
//
// הדגש המרכזי כאן הוא localWallClockToUtcIso: השעות שהמשתמש מקליד
// בדשבורד הן שעון מקומי *של העיר*, לא UTC ולא שעון הדפדפן. זו בדיוק
// הסמנטיקה שהייתה ל-/add_session אחרי התיקון מ-2026-09-08, והבדיקות
// כאן נועדו לוודא שההעברה לדשבורד לא איבדה אותה.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  calculateExposureScore,
  effectiveSpf,
  errorResponse,
  isTokenExpired,
  localWallClockToUtcIso,
  pastDaysFor,
  pythonRound,
  resolveSessionTimes,
  textMatches,
  utcIsoToLocalWallClock,
  validateRequest,
  validateResolvedTimes,
  validateSessionPayload,
  weightedAverageUv,
} from "./logic.ts";

// -----------------------------------------------------------------------
// localWallClockToUtcIso
// -----------------------------------------------------------------------

test("localWallClockToUtcIso: Israel summer (UTC+3) — 14:00 local is 11:00 UTC", () => {
  const iso = localWallClockToUtcIso("2026-09-10", "14:00", 3 * 3600);
  assert.equal(iso, "2026-09-10T11:00:00.000Z");
});

test("localWallClockToUtcIso: Thailand (UTC+7) — 09:30 local is 02:30 UTC", () => {
  const iso = localWallClockToUtcIso("2026-09-10", "09:30", 7 * 3600);
  assert.equal(iso, "2026-09-10T02:30:00.000Z");
});

test("localWallClockToUtcIso: negative offset (New York, UTC-4) — 08:00 local is 12:00 UTC", () => {
  const iso = localWallClockToUtcIso("2026-09-10", "08:00", -4 * 3600);
  assert.equal(iso, "2026-09-10T12:00:00.000Z");
});

test("localWallClockToUtcIso: offset can push the UTC date back a day", () => {
  // 01:00 מקומי בתאילנד (UTC+7) = 18:00 UTC של היום הקודם.
  const iso = localWallClockToUtcIso("2026-09-10", "01:00", 7 * 3600);
  assert.equal(iso, "2026-09-09T18:00:00.000Z");
});

test("localWallClockToUtcIso: offset=0 leaves the wall clock untouched (UTC)", () => {
  assert.equal(localWallClockToUtcIso("2026-01-05", "23:45", 0), "2026-01-05T23:45:00.000Z");
});

test("localWallClockToUtcIso: single-digit hour is accepted", () => {
  assert.equal(localWallClockToUtcIso("2026-09-10", "9:05", 0), "2026-09-10T09:05:00.000Z");
});

test("localWallClockToUtcIso: malformed input returns null instead of guessing", () => {
  assert.equal(localWallClockToUtcIso("10.9.2026", "14:00", 0), null);
  assert.equal(localWallClockToUtcIso("2026-09-10", "14", 0), null);
  assert.equal(localWallClockToUtcIso("2026-09-10", "25:00", 0), null);
  assert.equal(localWallClockToUtcIso("2026-09-10", "14:75", 0), null);
  assert.equal(localWallClockToUtcIso("2026-13-10", "14:00", 0), null);
});

// -----------------------------------------------------------------------
// utcIsoToLocalWallClock — הכיוון ההפוך (action="read", טופס העריכה)
// -----------------------------------------------------------------------

test("utcIsoToLocalWallClock: inverts localWallClockToUtcIso exactly", () => {
  const cases: [string, string, number][] = [
    ["2026-09-10", "14:00", 3 * 3600],
    ["2026-09-10", "09:30", 7 * 3600],
    ["2026-09-10", "08:00", -4 * 3600],
    ["2026-09-10", "01:00", 7 * 3600],
    ["2026-09-10", "23:45", 0],
  ];
  for (const [date, time, offset] of cases) {
    const iso = localWallClockToUtcIso(date, time, offset)!;
    assert.deepEqual(utcIsoToLocalWallClock(iso, offset), { date, time });
  }
});

test("utcIsoToLocalWallClock: a Thailand session read from Israel keeps Thai wall-clock", () => {
  // 09:30 בתאילנד (UTC+7) נשמר כ-02:30Z; קריאה עם ה-offset של תאילנד
  // חייבת להחזיר 09:30 ולא 05:30 (מה ששעון ישראל היה מראה).
  const local = utcIsoToLocalWallClock("2026-09-10T02:30:00.000Z", 7 * 3600);
  assert.deepEqual(local, { date: "2026-09-10", time: "09:30" });
});

test("utcIsoToLocalWallClock: rejects a malformed timestamp", () => {
  assert.equal(utcIsoToLocalWallClock("not-a-date", 0), null);
});

// זמן ייחוס קבוע לכל הבדיקות שתלויות ב"עכשיו".
const NOW = new Date("2026-09-12T12:00:00.000Z");

// -----------------------------------------------------------------------
// resolveSessionTimes
// -----------------------------------------------------------------------

test("resolveSessionTimes: a normal past range passes through unchanged", () => {
  const out = resolveSessionTimes("2026-09-12T07:00:00.000Z", "2026-09-12T09:00:00.000Z", NOW);
  assert.deepEqual(out, {
    startIso: "2026-09-12T07:00:00.000Z",
    endIso: "2026-09-12T09:00:00.000Z",
  });
});

test("resolveSessionTimes: a genuine midnight crossing rolls the end into the next day", () => {
  // 20:30 -> 00:45, שניהם בעבר ביחס ל-NOW.
  const out = resolveSessionTimes("2026-09-11T20:30:00.000Z", "2026-09-11T00:45:00.000Z", NOW);
  assert.deepEqual(out, {
    startIso: "2026-09-11T20:30:00.000Z",
    endIso: "2026-09-12T00:45:00.000Z",
  });
});

test("resolveSessionTimes: a start time in the future blames the start, not the end", () => {
  // המקרה האמיתי מ-2026-09-12: start=10:00 PM (22:00) במקום 10:00 AM,
  // end=12:29 PM. הגרסה הראשונה ענתה "שעת הסיום לא יכולה להיות בעתיד",
  // כלומר הפנתה את המשתמש לשדה הלא נכון.
  const out = resolveSessionTimes("2026-09-12T19:00:00.000Z", "2026-09-12T09:29:00.000Z", NOW);
  assert.ok("message" in out);
  assert.match((out as { message: string }).message, /שעת ההתחלה/);
  assert.match((out as { message: string }).message, /AM\/PM/);
});

test("resolveSessionTimes: end before start is NOT shifted when that would land in the future", () => {
  // start בעבר, end מוקדם ממנו, אבל הזזה ליום הבא הייתה יוצרת סיום עתידי.
  const out = resolveSessionTimes("2026-09-12T11:00:00.000Z", "2026-09-12T10:00:00.000Z", NOW);
  assert.ok("message" in out);
  assert.match((out as { message: string }).message, /מוקדמת משעת ההתחלה/);
  assert.match((out as { message: string }).message, /AM\/PM/);
});

test("resolveSessionTimes: still rejects an end in the future without a midnight crossing", () => {
  const out = resolveSessionTimes("2026-09-12T11:00:00.000Z", "2026-09-12T14:00:00.000Z", NOW);
  assert.ok("message" in out);
  assert.match((out as { message: string }).message, /בעתיד/);
});

test("resolveSessionTimes: still enforces the 24h and 92-day limits", () => {
  const tooLong = resolveSessionTimes("2026-09-10T07:00:00.000Z", "2026-09-11T08:00:00.000Z", NOW);
  assert.match((tooLong as { message: string }).message, /ארוך מדי/);
  const tooOld = resolveSessionTimes("2026-01-01T07:00:00.000Z", "2026-01-01T09:00:00.000Z", NOW);
  assert.match((tooOld as { message: string }).message, /92 הימים/);
});

test("resolveSessionTimes: a malformed timestamp is reported, not crashed on", () => {
  const out = resolveSessionTimes("nonsense", "2026-09-12T09:00:00.000Z", NOW);
  assert.match((out as { message: string }).message, /לא תקינים/);
});

// -----------------------------------------------------------------------
// validateResolvedTimes
// -----------------------------------------------------------------------


test("validateResolvedTimes: a normal past session passes", () => {
  const err = validateResolvedTimes("2026-09-12T07:00:00.000Z", "2026-09-12T09:00:00.000Z", NOW);
  assert.equal(err, null);
});

test("validateResolvedTimes: end equal to start is rejected", () => {
  const err = validateResolvedTimes("2026-09-12T07:00:00.000Z", "2026-09-12T07:00:00.000Z", NOW);
  assert.match(err!, /אחרי שעת ההתחלה/);
});

test("validateResolvedTimes: longer than 24h is rejected", () => {
  const err = validateResolvedTimes("2026-09-10T07:00:00.000Z", "2026-09-11T08:00:00.000Z", NOW);
  assert.match(err!, /ארוך מדי/);
});

test("validateResolvedTimes: an end time in the future is rejected", () => {
  const err = validateResolvedTimes("2026-09-12T11:00:00.000Z", "2026-09-12T14:00:00.000Z", NOW);
  assert.match(err!, /בעתיד/);
});

test("validateResolvedTimes: small clock skew within 5 minutes is tolerated", () => {
  const err = validateResolvedTimes("2026-09-12T10:00:00.000Z", "2026-09-12T12:03:00.000Z", NOW);
  assert.equal(err, null);
});

test("validateResolvedTimes: older than the 92-day UV window is rejected", () => {
  const err = validateResolvedTimes("2026-01-01T07:00:00.000Z", "2026-01-01T09:00:00.000Z", NOW);
  assert.match(err!, /92 הימים/);
});

// -----------------------------------------------------------------------
// validateSessionPayload / validateRequest
// -----------------------------------------------------------------------

const GOOD_SESSION = { city: "תל אביב", date: "2026-09-10", start: "14:00", end: "16:30", spf: 30 };

test("validateSessionPayload: a well-formed payload passes", () => {
  assert.equal(validateSessionPayload(GOOD_SESSION), null);
});

test("validateSessionPayload: spf is optional (null and undefined both fine)", () => {
  assert.equal(validateSessionPayload({ ...GOOD_SESSION, spf: null }), null);
  const { spf: _dropped, ...noSpf } = GOOD_SESSION;
  assert.equal(validateSessionPayload(noSpf), null);
});

test("validateSessionPayload: bad fields are rejected", () => {
  assert.equal(validateSessionPayload({ ...GOOD_SESSION, city: "   " }), "invalid_session");
  assert.equal(validateSessionPayload({ ...GOOD_SESSION, date: "10/09/2026" }), "invalid_session");
  assert.equal(validateSessionPayload({ ...GOOD_SESSION, start: "2pm" }), "invalid_session");
  assert.equal(validateSessionPayload({ ...GOOD_SESSION, spf: 0 }), "invalid_session");
  assert.equal(validateSessionPayload({ ...GOOD_SESSION, spf: 4.5 }), "invalid_session");
  assert.equal(validateSessionPayload({ ...GOOD_SESSION, spf: 500 }), "invalid_session");
});

test("validateRequest: create requires a token, an action and a session", () => {
  assert.equal(validateRequest({ token: "t", action: "create", session: GOOD_SESSION }), null);
  assert.equal(validateRequest({ action: "create", session: GOOD_SESSION }), "missing_token");
  assert.equal(validateRequest({ token: "t", session: GOOD_SESSION }), "invalid_body");
  assert.equal(validateRequest({ token: "t", action: "create" }), "invalid_body");
});

test("validateRequest: update additionally requires an integer id", () => {
  assert.equal(validateRequest({ token: "t", action: "update", id: 12, session: GOOD_SESSION }), null);
  assert.equal(validateRequest({ token: "t", action: "update", session: GOOD_SESSION }), "invalid_body");
  assert.equal(
    validateRequest({ token: "t", action: "update", id: 1.5, session: GOOD_SESSION }),
    "invalid_body",
  );
});

test("validateRequest: delete needs only an id, no session payload", () => {
  assert.equal(validateRequest({ token: "t", action: "delete", id: 12 }), null);
  assert.equal(validateRequest({ token: "t", action: "delete" }), "invalid_body");
});

test("validateRequest: read needs only an id, no session payload", () => {
  assert.equal(validateRequest({ token: "t", action: "read", id: 12 }), null);
  assert.equal(validateRequest({ token: "t", action: "read" }), "invalid_body");
});

test("validateRequest: an unknown action is rejected", () => {
  assert.equal(
    validateRequest({ token: "t", action: "drop_everything" as never, id: 1 }),
    "invalid_body",
  );
});

// -----------------------------------------------------------------------
// textMatches
// -----------------------------------------------------------------------

test("textMatches: containment in either direction, ignoring dashes and quote marks", () => {
  assert.equal(textMatches("קוסטה ריקה", "קוסטה ריקה"), true);
  assert.equal(textMatches('ארה"ב', "ארה־ב"), false); // שונה מהותית, לא רק ניקוד
  assert.equal(textMatches("ישראל", "ישראל"), true);
  assert.equal(textMatches("קוסטה", "קוסטה ריקה"), true); // hint מוכל ב-value
  assert.equal(textMatches("", "ישראל"), false);
  assert.equal(textMatches("ישראל", null), false);
});

// -----------------------------------------------------------------------
// exposure score — parity מול geo_uv_core.calculate_exposure_score
// -----------------------------------------------------------------------

test("calculateExposureScore: matches the Python formula on a known case", () => {
  // uv=8, 90 דקות, סוג עור 3, בלי SPF:
  // safe = (200/8) * 1.0 * 1 = 25 דקות -> round(90/25*100) = 360
  assert.equal(calculateExposureScore(8, 90, 3, null), 360);
});

test("calculateExposureScore: SPF lowers the score via effectiveSpf", () => {
  // spf=30 -> 1 + 29*0.4 = 12.6 ; safe = 25*12.6 = 315 -> round(90/315*100) = 29
  // (השוואה בסבילות: 1 + 29*0.4 נותן 12.600000000000001 בנקודה צפה —
  // בדיוק אותו ערך שגם Python מחזיר, כלומר ה-parity נשמר.)
  assert.ok(Math.abs(effectiveSpf(30) - 12.6) < 1e-9);
  assert.equal(calculateExposureScore(8, 90, 3, 30), 29);
});

test("calculateExposureScore: UV=0 is 0, not a division blowup", () => {
  assert.equal(calculateExposureScore(0, 600, 3, null), 0);
});

test("calculateExposureScore: skin type changes the result", () => {
  assert.ok(calculateExposureScore(8, 90, 1, null) > calculateExposureScore(8, 90, 6, null));
});

// -----------------------------------------------------------------------
// weightedAverageUv / pastDaysFor / isTokenExpired / errorResponse
// -----------------------------------------------------------------------

test("weightedAverageUv: weights each hourly bucket by real overlap", () => {
  const times = ["2026-09-10T10:00", "2026-09-10T11:00", "2026-09-10T12:00"];
  const uvs = [2, 8, 4];
  // 11:00-12:00 בלבד -> bucket יחיד בערך 8
  assert.equal(weightedAverageUv(times, uvs, "2026-09-10T11:00Z", "2026-09-10T12:00Z"), 8);
  // 10:30-11:30 -> חצי שעה על 2 וחצי שעה על 8 -> 5
  assert.equal(weightedAverageUv(times, uvs, "2026-09-10T10:30Z", "2026-09-10T11:30Z"), 5);
});

test("weightedAverageUv: no overlap at all returns null", () => {
  const times = ["2026-09-10T10:00"];
  assert.equal(weightedAverageUv(times, [5], "2026-09-11T10:00Z", "2026-09-11T11:00Z"), null);
});

test("pastDaysFor: recent dates are supported, very old ones are not", () => {
  assert.ok((pastDaysFor("2026-09-10T10:00:00Z", NOW) ?? -1) >= 0);
  assert.equal(pastDaysFor("2025-01-01T10:00:00Z", NOW), null);
});

test("isTokenExpired: compares against the injected now", () => {
  assert.equal(isTokenExpired("2026-09-12T13:00:00Z", NOW), false);
  assert.equal(isTokenExpired("2026-09-12T11:00:00Z", NOW), true);
});

test("errorResponse: each code maps to its HTTP status and carries CORS", async () => {
  const cases = [
    ["invalid_body", 400],
    ["missing_token", 400],
    ["invalid_token", 404],
    ["expired_token", 410],
    ["not_onboarded", 404],
    ["city_not_found", 422],
    ["uv_unavailable", 422],
    ["session_not_found", 404],
    ["invalid_session", 400],
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
