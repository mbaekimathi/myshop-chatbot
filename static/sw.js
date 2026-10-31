/* Minimal service worker — enables PWA install + auto-update.
   No caching: every request hits the network so the app is never stale.
   VERSION bumps force a re-install when a new build ships. */
var SW_VERSION = "2026-11-01.1";

self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", function () {
  // pass-through — nothing cached
});
