// SunSafe — בדיקות ל-runSweep (ה-I/O של ה-Worker).
//
//   node --experimental-strip-types --test src/index.test.ts
//
// global.fetch מוחלף בכפיל שמנתב לפי ה-URL, כך שהגוף האמיתי של
// runSweep/closeSession רץ במלואו — כולל סדר הפעולות, שהוא העיקר כאן.

import { test } from "node:test";
import assert from "node:assert/strict";

import { runSweep } from "./index.ts";

const ENV = {
  SUPABASE_URL: "https://example.supabase.co",
  SUPABASE_SERVICE_ROLE_KEY: "service-role",
  BOT_TOKEN: "bot-token",
};

const NOW = new Date("2026-09-16T17:00:00Z");           // אחרי השקיעה בישראל
const START = new Date("2026-09-16T13:00:00Z");         // 4 שעות קודם

function openSession(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    telegram_username: "omri",
    city: "ירוחם",
    start_time: START.toISOString(),
    uv_index: 6.3,
    lat: 30.99,
    lon: 34.93,
    spf: null,
    ...overrides,
  };
}

interface Call { url: string; method: string; body?: unknown }

/**
 * @param opts.sessions   השורות שמוחזרות כ-sessions פתוחים
 * @param opts.stillOpen  האם הבדיקה החוזרת מוצאת את ה-session פתוח
 * @param opts.sunsetLocal שעת השקיעה המקומית, או null לכשל בשליפה
 * @param opts.patchOk    האם ה-PATCH ל-DB מצליח
 */
function installFakeFetch(opts: {
  sessions?: ReturnType<typeof openSession>[];
  stillOpen?: boolean;
  sunsetLocal?: string | null;
  patchOk?: boolean;
  chatId?: number | null;
  hourlyUv?: (number | null)[] | null;
  dailyRows?: { city: string; start_time: string; end_time: string | null;
                exposure_score: number | null }[];
}) {
  const calls: Call[] = [];
  const {
    sessions = [openSession()],
    stillOpen = true,
    sunsetLocal = "2026-09-16T18:42",
    patchOk = true,
    chatId = 670212669,
    hourlyUv = null,
    dailyRows = [],
  } = opts;

  globalThis.fetch = (async (input: string | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, method: init?.method ?? "GET",
                 body: init?.body ? JSON.parse(String(init.body)) : undefined });

    const json = (data: unknown, ok = true) =>
      new Response(JSON.stringify(data), { status: ok ? 200 : 500 });

    if (url.includes("/rest/v1/exposure_log") && (init?.method ?? "GET") === "GET") {
      // הבדיקה החוזרת מזוהה לפי end_time=is.null יחד עם id=eq.
      if (url.includes("id=eq.")) return json(stillOpen ? [{ id: 7 }] : []);
      // שליפת הסיכום היומי מזוהה לפי select=city,... — שאילתה אחרת
      // לגמרי מזו של הסבב, ומחזירה sessions *סגורים* של אותו יום.
      if (url.includes("select=city")) return json(dailyRows);
      return json(sessions);
    }
    if (url.includes("/rest/v1/exposure_log") && init?.method === "PATCH") {
      // 204 הוא null-body status — Response עם גוף לא-ריק זורק כאן.
      return patchOk
        ? new Response(null, { status: 204 })
        : new Response("boom", { status: 500 });
    }
    if (url.includes("/rest/v1/users")) {
      return json([{ chat_id: chatId, skin_type: 3 }]);
    }
    if (url.includes("daily=sunset")) {
      if (sunsetLocal === null) return json({}, false);
      return json({ daily: { sunset: [sunsetLocal] }, utc_offset_seconds: 3 * 3600 });
    }
    if (url.includes("hourly=uv_index")) {
      if (hourlyUv === null) return json({}, false);
      const times = Array.from({ length: 24 }, (_, h) =>
        `2026-09-16T${String(h).padStart(2, "0")}:00`);
      return json({ hourly: { time: times, uv_index: hourlyUv } });
    }
    if (url.includes("api.telegram.org")) return json({ ok: true });
    throw new Error(`unexpected fetch: ${url}`);
  }) as typeof fetch;

  return calls;
}

test("past sunset: closes the session and notifies the user", async () => {
  const calls = installFakeFetch({});
  const result = await runSweep(ENV, NOW);

  assert.deepEqual(result, { closed: 1, seen: 1 });

  const patch = calls.find((c) => c.method === "PATCH");
  assert.ok(patch, "no PATCH was sent");
  const body = patch!.body as Record<string, unknown>;
  assert.equal(body.end_time, NOW.toISOString());
  assert.ok(typeof body.exposure_score === "number");

  const telegram = calls.find((c) => c.url.includes("api.telegram.org"));
  assert.ok(telegram, "the user was not notified");
  const text = (telegram!.body as { text: string }).text;
  assert.match(text, /דקות בירוחם/);
  assert.match(text, /\/dashboard/);
});

test("the database is written BEFORE Telegram is called", async () => {
  // אם ההודעה נשלחת קודם והכתיבה נכשלת, ה-session נשאר פתוח והסבב
  // הבא ישלח את אותה הודעה שוב — כל 5 דקות, עד אינסוף.
  const calls = installFakeFetch({});
  await runSweep(ENV, NOW);

  const patchAt = calls.findIndex((c) => c.method === "PATCH");
  const telegramAt = calls.findIndex((c) => c.url.includes("api.telegram.org"));
  assert.ok(patchAt >= 0 && telegramAt >= 0);
  assert.ok(patchAt < telegramAt, `PATCH at ${patchAt}, Telegram at ${telegramAt}`);
});

test("a failed DB write means no message is sent at all", async () => {
  const calls = installFakeFetch({ patchOk: false });
  const result = await runSweep(ENV, NOW);

  assert.equal(result.closed, 0);
  assert.equal(calls.filter((c) => c.url.includes("api.telegram.org")).length, 0);
});

test("before sunset: nothing is touched", async () => {
  const earlyAfternoon = new Date("2026-09-16T10:00:00Z");
  const calls = installFakeFetch({});
  const result = await runSweep(ENV, earlyAfternoon);

  assert.deepEqual(result, { closed: 0, seen: 1 });
  assert.equal(calls.filter((c) => c.method === "PATCH").length, 0);
});

test("sunset lookup failure leaves the session alone", async () => {
  // חשוב: כשל ברשת לא אמור לסגור sessions באמצע היום.
  const calls = installFakeFetch({ sunsetLocal: null });
  const result = await runSweep(ENV, NOW);

  assert.equal(result.closed, 0);
  assert.equal(calls.filter((c) => c.method === "PATCH").length, 0);
});

test("a session the user closed a moment ago is not closed twice", async () => {
  const calls = installFakeFetch({ stillOpen: false });
  const result = await runSweep(ENV, NOW);

  assert.equal(result.closed, 0);
  assert.equal(calls.filter((c) => c.url.includes("api.telegram.org")).length, 0);
});

test("a row with no lat/lon is skipped, not crashed on", async () => {
  const calls = installFakeFetch({ sessions: [openSession({ lat: null, lon: null })] });
  const result = await runSweep(ENV, NOW);

  assert.deepEqual(result, { closed: 0, seen: 1 });
  assert.equal(calls.filter((c) => c.url.includes("daily=sunset")).length, 0);
});

test("no chat_id: the session still closes, the message is just skipped", async () => {
  const calls = installFakeFetch({ chatId: null });
  const result = await runSweep(ENV, NOW);

  assert.equal(result.closed, 1);
  assert.ok(calls.some((c) => c.method === "PATCH"));
  assert.equal(calls.filter((c) => c.url.includes("api.telegram.org")).length, 0);
});

test("weighted-UV refresh is used when available, and labelled as an average", async () => {
  const uv = Array.from({ length: 24 }, (_, h) => (h >= 13 && h < 17 ? 8 : 1));
  const calls = installFakeFetch({ hourlyUv: uv });
  await runSweep(ENV, NOW);

  const patch = calls.find((c) => c.method === "PATCH")!.body as Record<string, number>;
  assert.equal(patch.uv_index, 8, "the refreshed average should replace the snapshot");

  const text = (calls.find((c) => c.url.includes("api.telegram.org"))!.body as { text: string }).text;
  assert.match(text, /UV ממוצע 8\.0/);
});

test("refresh failure falls back to the stored snapshot, labelled as plain UV", async () => {
  const calls = installFakeFetch({ hourlyUv: null });
  await runSweep(ENV, NOW);

  const patch = calls.find((c) => c.method === "PATCH")!.body as Record<string, number>;
  assert.equal(patch.uv_index, 6.3, "should keep the snapshot taken at session start");

  const text = (calls.find((c) => c.url.includes("api.telegram.org"))!.body as { text: string }).text;
  assert.match(text, /ב-UV 6\.3/);
  assert.doesNotMatch(text, /ממוצע/);
});

test("one bad row does not abort the whole sweep", async () => {
  const good = openSession({ id: 8 });
  const bad = openSession({ id: 9, start_time: "not-a-date" });
  const calls = installFakeFetch({ sessions: [bad, good] });
  const result = await runSweep(ENV, NOW);

  assert.equal(result.seen, 2);
  // ה-session התקין נסגר למרות שהראשון בתור בעייתי.
  assert.ok(calls.some((c) => c.method === "PATCH"));
});


test("the message carries the cumulative daily bar, not just this session", async () => {
  // שני sessions סגורים קודם באותו יום (40% ו-35%) ועוד אחד שנסגר
  // כרגע. הסכום הוא מה שהמשתמש צריך לראות — נזק UV מצטבר, ו-max היה
  // מציג לו אחד מהשלושה. "היום" הוא תאריך ה-UTC של start_time, כמו
  // ב-_sessions_on_date בפייתון.
  const day = START.toISOString().slice(0, 10);
  const calls = installFakeFetch({
    dailyRows: [
      { city: "אילת", start_time: `${day}T05:00:00Z`, end_time: `${day}T05:25:00Z`, exposure_score: 40 },
      { city: "ירושלים", start_time: `${day}T08:00:00Z`, end_time: `${day}T08:20:00Z`, exposure_score: 35 },
      { city: "ירוחם", start_time: START.toISOString(), end_time: NOW.toISOString(), exposure_score: 30 },
    ],
  });
  await runSweep(ENV, NOW);

  const text = (calls.find((c) => c.url.includes("api.telegram.org"))!.body as { text: string }).text;
  assert.match(text, /105%/, "the day should be 40+35+30, not the max");
  assert.match(text, /חשיפה מלאה/, "over 100% reads as full exposure");
  assert.ok(text.includes("🟥".repeat(10)), "over 100% the bar is all red");
  assert.match(text, /3 sessions/);
  assert.match(text, /הגבוה מביניהם: אילת, 40%/);
  // והתוצאה של ה-session עצמו עדיין שם
  assert.match(text, /דקות בירוחם/);
});

test("a day with no other closed sessions still sends, without a daily block", async () => {
  const calls = installFakeFetch({ dailyRows: [] });
  await runSweep(ENV, NOW);
  const text = (calls.find((c) => c.url.includes("api.telegram.org"))!.body as { text: string }).text;
  assert.match(text, /דקות בירוחם/);
  assert.match(text, /\/dashboard/);
  assert.ok(!text.includes("היום:"), "no daily block when the lookup returned nothing");
});

test("a failed daily lookup does not stop the message", async () => {
  // הסיכום היומי הוא תוספת. אם השליפה נכשלת, המשתמש עדיין מקבל את
  // התוצאה של ה-session שלו.
  const calls = installFakeFetch({});
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL, init?: RequestInit) => {
    if (String(input).includes("select=city")) throw new Error("daily lookup exploded");
    return original(input as never, init as never);
  }) as typeof fetch;

  const result = await runSweep(ENV, NOW);
  assert.equal(result.closed, 1, "the session still closes");
  const telegram = calls.find((c) => c.url.includes("api.telegram.org"));
  assert.ok(telegram, "the user is still notified");
});

