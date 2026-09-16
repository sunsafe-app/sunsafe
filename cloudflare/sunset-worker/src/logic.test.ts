// SunSafe — בדיקות ל-logic.ts של sunset-worker.
//
//   node --experimental-strip-types --test src/logic.test.ts
//
// **הציפיות כאן לא נכתבו ביד.** הן נגזרות מ-src/fixture.json, שנוצר
// ישירות מהמימוש בפייתון (scripts/gen_fixture.py). זו כל הנקודה: ה-Worker
// כותב exposure_score ל-exposure_log, בדיוק כמו הבוט, ואם שני המימושים
// יתפצלו תהיה לאותו משתמש היסטוריית חשיפה חצויה. בדיקה שמשווה מול
// ציפיות שכתבנו ביד לא הייתה תופסת פיצול כזה — היא הייתה מתעדכנת איתו.
//
// כדי לרענן את ה-fixture אחרי שינוי מכוון בנוסחה או בנוסח:
//   python cloudflare/sunset-worker/scripts/gen_fixture.py

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  buildCompletionMessage,
  calculateExposureScore,
  effectiveSpf,
  formatDurationHe,
  isPastSunset,
  pythonRound,
  safeExposureMinutes,
  sunsetToUtc,
  weightedAverageUv,
} from "./logic.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(here, "fixture.json"), "utf8")) as {
  scores: { uv: number; skin: number; spf: number | null; duration: number;
            score: number; safeMinutes: number | null }[];
  durations: { minutes: number; text: string }[];
  messages: { city: string; durationMinutes: number; uvIndex: number; skinType: number;
              spf: number | null; uvIsAverage: boolean; text: string }[];
};

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
  assert.deepEqual(mismatches, [], `${mismatches.length} of ${fixture.scores.length} differ`);
});

test("safe exposure minutes match Python (and null where Python returns None)", () => {
  for (const c of fixture.scores) {
    const got = safeExposureMinutes(c.uv, c.skin, c.spf);
    if (c.safeMinutes === null) {
      assert.equal(got, null, `uv=${c.uv} should have no safe-minutes value`);
    } else {
      assert.ok(got !== null);
      assert.ok(
        Math.abs(got - c.safeMinutes) < 1e-9,
        `uv=${c.uv} skin=${c.skin} spf=${c.spf}: ts=${got} py=${c.safeMinutes}`,
      );
    }
  }
});

test("Hebrew durations match Python exactly", () => {
  for (const d of fixture.durations) {
    assert.equal(formatDurationHe(d.minutes), d.text, `minutes=${d.minutes}`);
  }
});

test("the completion message is byte-identical to what the bot sends", () => {
  for (const m of fixture.messages) {
    const score = calculateExposureScore(m.uvIndex, m.durationMinutes, m.skinType, m.spf);
    const got = buildCompletionMessage({
      durationMinutes: m.durationMinutes,
      city: m.city,
      score,
      uvIndex: m.uvIndex,
      uvIsAverage: m.uvIsAverage,
      skinType: m.skinType,
      spf: m.spf,
    });
    assert.equal(got, m.text, `city=${m.city}`);
  }
});

test("effectiveSpf: no sunscreen is a factor of 1", () => {
  assert.equal(effectiveSpf(null), 1);
  assert.equal(effectiveSpf(undefined), 1);
  assert.equal(effectiveSpf(0), 1);
  assert.equal(effectiveSpf(30), 1 + 29 * 0.4);
});

test("pythonRound follows banker's rounding, unlike Math.round", () => {
  // זה ההפרש שבגללו הפונקציה קיימת: Math.round(0.5)=1, Math.round(2.5)=3.
  assert.equal(pythonRound(0.5), 0);
  assert.equal(pythonRound(1.5), 2);
  assert.equal(pythonRound(2.5), 2);
  assert.equal(pythonRound(3.5), 4);
  assert.equal(pythonRound(2.4), 2);
  assert.equal(pythonRound(2.6), 3);
});

test("weightedAverageUv weights each hour by its overlap with the session", () => {
  const times = ["2026-09-16T10:00", "2026-09-16T11:00", "2026-09-16T12:00"];
  const uv = [2, 8, 4];

  // חצי שעה ב-10 וחצי שעה ב-11 -> ממוצע פשוט של 2 ו-8
  const half = weightedAverageUv(times, uv, new Date("2026-09-16T10:30Z"), new Date("2026-09-16T11:30Z"));
  assert.ok(half !== null);
  assert.ok(Math.abs(half - 5) < 1e-9, `-> ${half}`);

  // שעה שלמה אחת בלבד -> בדיוק הערך שלה
  const one = weightedAverageUv(times, uv, new Date("2026-09-16T11:00Z"), new Date("2026-09-16T12:00Z"));
  assert.ok(one !== null && Math.abs(one - 8) < 1e-9, `-> ${one}`);

  // חלונות null מדולגים ולא נחשבים כאפס
  const withNulls = weightedAverageUv(times, [null, 8, null],
    new Date("2026-09-16T10:00Z"), new Date("2026-09-16T13:00Z"));
  assert.ok(withNulls !== null && Math.abs(withNulls - 8) < 1e-9, `-> ${withNulls}`);

  // אין חפיפה בכלל, או טווח הפוך -> null, לא 0
  assert.equal(weightedAverageUv(times, uv, new Date("2026-09-17T10:00Z"), new Date("2026-09-17T11:00Z")), null);
  assert.equal(weightedAverageUv(times, uv, new Date("2026-09-16T12:00Z"), new Date("2026-09-16T10:00Z")), null);
});

test("sunsetToUtc converts the location's local time, not the server's", () => {
  // ישראל בספטמבר: UTC+3. שקיעה מקומית 18:42 -> 15:42Z.
  const utc = sunsetToUtc("2026-09-16T18:42", 3 * 3600);
  assert.equal(utc?.toISOString(), "2026-09-16T15:42:00.000Z");

  // תאילנד: UTC+7. אותה שעה מקומית -> 11:42Z. אזור זמן אחר, תוצאה אחרת —
  // זו בדיוק הטעות שנפלה פעם בפייתון (הנחה ש-HH:MM הוא כבר UTC).
  const bangkok = sunsetToUtc("2026-09-16T18:42", 7 * 3600);
  assert.equal(bangkok?.toISOString(), "2026-09-16T11:42:00.000Z");

  assert.equal(sunsetToUtc("not-a-date", 0), null);
});

test("isPastSunset never closes a session when the sunset is unknown", () => {
  const now = new Date("2026-09-16T16:00:00Z");
  assert.equal(isPastSunset(now, new Date("2026-09-16T15:42:00Z")), true);
  assert.equal(isPastSunset(now, new Date("2026-09-16T16:30:00Z")), false);
  // null = לא הצלחנו לקבוע את השקיעה. חייב להיות false: session שנסגר
  // בטעות באמצע היום הוא נזק שאין ממנו חזרה.
  assert.equal(isPastSunset(now, null), false);
});
