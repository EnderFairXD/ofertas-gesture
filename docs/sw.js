// Cachea el armazón de la app para que abra al instante y funcione sin
// cobertura. Los precios NO se cachean aquí: van por red y, si falla, la
// propia página tira de su copia en localStorage.
const CACHE = "gesture-v12";
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

function guardar(peticion, respuesta) {
  if (respuesta.ok) {
    const copia = respuesta.clone();
    caches.open(CACHE).then((c) => c.put(peticion, copia));
  }
  return respuesta;
}

self.addEventListener("fetch", (ev) => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== location.origin) { return; }

  // La página lleva dentro la lógica de la app, así que va por red primero:
  // estando conectado siempre debe verse la última versión publicada. La
  // caché es solo la red de seguridad para cuando no hay cobertura.
  const esPagina = ev.request.mode === "navigate" ||
                   url.pathname.endsWith("/") ||
                   url.pathname.endsWith(".html");
  if (esPagina) {
    // GitHub Pages sirve la página con max-age=600, así que una publicación
    // nueva tardaba hasta diez minutos en verse. Se pide saltándose la caché
    // del navegador: la del service worker sigue cubriendo el modo sin
    // conexión.
    const fresca = new Request(ev.request.url, {
      cache: "no-cache",
      credentials: "same-origin"
    });
    ev.respondWith(
      fetch(fresca)
        .then((resp) => guardar(ev.request, resp))
        .catch(() => caches.match(ev.request, { ignoreSearch: true })
          .then((guardado) => guardado || caches.match("./index.html")))
    );
    return;
  }

  // Iconos y manifiesto: caché primero, refrescando por detrás.
  ev.respondWith(
    caches.match(ev.request, { ignoreSearch: true }).then((guardado) => {
      const red = fetch(ev.request)
        .then((resp) => guardar(ev.request, resp))
        .catch(() => guardado);
      return guardado || red;
    })
  );
});
