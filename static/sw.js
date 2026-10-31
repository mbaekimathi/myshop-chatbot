/* Minimal service worker — enables PWA install. No caching:
   every request goes to the network so the app is never stale. */
self.addEventListener("install", function (event) {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", function () {
  // pass-through — nothing cached
});
