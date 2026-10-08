// SunSafe — Service Worker ל-Mini App האופליין (docs/session/index.html)
// -----------------------------------------------------------------------
// תפקיד יחיד: להבטיח שהעמוד וה-SDK של Telegram זמינים ב-cache מקומי אחרי
// ביקור ראשון, כדי שהמכשיר יוכל לפתוח את ה-Mini App גם בלי שום קליטה
// (לא להסתמך על היוריסטיקת ה-HTTP cache הרגילה של הדפדפן/WebView).
// ראו docs/2026-08-29-offline-session-miniapp-design.md סעיף 4.
//
// שינוי גרסה (CACHE_NAME) מכריח רענון cache בפעם הבאה שיש קליטה.

const CACHE_NAME = "sunsafe-session-v2"; // v2 (2026-10-08): אנגלית — מחליף את index.html השמור
const PRECACHE_URLS = [
  "./",
  "./index.html",
  "https://telegram.org/js/telegram-web-app.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // no-cors כדי לתמוך גם ב-URL חוצה-מקור (ה-SDK של טלגרם) — מקבלים
      // תשובה "אטומה" (opaque) אבל היא בהחלט שמישה כשמגישים אותה בחזרה.
      Promise.all(
        PRECACHE_URLS.map((url) =>
          fetch(url, { mode: "no-cors" }).then((resp) => cache.put(url, resp)).catch(() => {})
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Cache-first, עם fallback לרשת (ואם גם זה נכשל — פשוט נכשל, אין עוד מה לעשות).
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => {});
          return resp;
        })
        .catch(() => cached);
    })
  );
});
