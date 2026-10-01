// Cachea el armazón de la app para que abra al instante y funcione sin
// cobertura. Los precios NO se cachean aquí: van por red y, si falla, la
// propia página tira de su copia en localStorage.
const CACHE = "gesture-v2";
const ARMAZON = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./apple-touch-icon.png",
  "./favicon-32.png"
];

self.addEventListener("install", (ev) => {
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(ARMAZON)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (ev) => {
  ev.waitUntil(
    caches.keys()
      .then((claves) => Promise.all(claves.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== location.origin) { return; }
  ev.respondWith(
    caches.match(ev.request, { ignoreSearch: true }).then((guardado) => {
      const red = fetch(ev.request)
        .then((resp) => {
          if (resp.ok) {
            const copia = resp.clone();
            caches.open(CACHE).then((c) => c.put(ev.request, copia));
          }
          return resp;
        })
        .catch(() => guardado);
      return guardado || red;
    })
  );
});
