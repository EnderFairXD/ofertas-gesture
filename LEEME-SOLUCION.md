# Rastreador Gesture — diagnóstico y solución

## 1. El diagnóstico no es el que parecía

Antes de tocar código probé las cinco tiendas desde una conexión doméstica
normal (no desde GitHub). Resultado:

| Tienda | Qué pasa de verdad | ¿Es culpa de la IP de GitHub? |
|---|---|---|
| **es.steelcase.com** | HTTP **200**, sin Cloudflare. El selector `.price-item--regular` aparece **0 veces** en el HTML. El precio sí está en el JSON-LD y en `/products/gesture.js`: **1.169,00 €** | **No.** Selector equivocado |
| **theofficecrowd.com** | HTTP **200**. Es Shopify; su buscador JSON funciona. Pero **ahora mismo no tiene ninguna Gesture** (devuelve Orangebox, Humanscale, Aeron…) | **No.** Producto inexistente |
| **oficinasmontiel.com** | La URL del código devuelve **404**. Su buscador real es `/busqueda?controller=search&s=…` y en su catálogo **no hay Gesture** (solo Aeron) | **No.** URL muerta |
| **ebay.es/sch** | HTTP **403** incluso desde casa | Sí, muro anti-bot |
| **corporatespec.com** | HTTP **403** con `cf-mitigated: challenge` incluso desde casa | Sí, muro anti-bot |

Conclusión: **tres de los cinco fallos no tenían nada que ver con GitHub
Actions**. Eran selectores y URLs rotos, y un detalle peor — raspar una página
de resultados con `.price-item` te habría dado el precio de *otra silla
cualquiera*, no de la Gesture. El `datos.json` vacío te estaba ocultando eso.

## 2. La estrategia

En vez de pelearse con Cloudflare, pedir los datos por donde las tiendas ya
los publican para máquinas. Es más rápido, no se rompe al cambiar de tema y no
dispara los anti-bots:

1. **Shopify** (Steelcase y The Office Crowd lo son) → `/products/<handle>.js`
   y `/search/suggest.json`. JSON oficial, sin HTML.
2. **JSON-LD de schema.org** → casi toda tienda moderna (PrestaShop,
   WooCommerce, Magento) incrusta `Product`/`Offer` con el precio. Es el
   adaptador genérico para añadir tiendas nuevas.
3. **eBay** → **Browse API oficial**, gratis (5.000 llamadas/día). Raspar
   `ebay.es/sch` no funciona desde ninguna IP.
4. **Nunca publicar vacío** → si una tienda cae se conserva su último precio
   marcado como `obsoleto`; si caen todas, `datos.json` se queda intacto y el
   workflow se pone en **rojo**.
5. **ScraperAPI como red de reintento** (opcional, 1.000 peticiones/mes
   gratis): si una petición recibe 403/429, se repite a través de ella. Con la
   arquitectura nueva casi no se usa.

Sobre **corporatespec.com**: responde con un reto de Cloudflare incluso desde
una conexión doméstica, o sea que su dueño está rechazando clientes
automáticos de forma explícita. No voy a escribir código para resolver ese
reto. Alternativas: quitarla (es lo que he hecho), mirarla a mano, o
escribirles pidiendo acceso o un feed de precios.

## 3. Pasos

1. **Copia los archivos** al repo `ofertas-gesture`:
   `scraper.py`, `app.py`, `requirements.txt`, `requirements-scraper.txt`,
   `.github/workflows/robot.yml`. Añade `debug/` al `.gitignore`.

2. **Credenciales de eBay** (5 min, gratis):
   `developer.ebay.com` → crea cuenta → *Application Keys* → claves de
   **Production** → copia *App ID (Client ID)* y *Cert ID (Client Secret)*.
   En el repo: *Settings → Secrets and variables → Actions → New secret*:
   - `EBAY_CLIENT_ID`
   - `EBAY_CLIENT_SECRET`
   Sin ellas el script no falla: marca eBay como omitida y sigue.

3. **Opcional, ScraperAPI**: regístrate, copia la clave y guárdala como secret
   `SCRAPERAPI_KEY`. El código la detecta solo.

4. **La hora del cron va en UTC**. `0 8 * * *` eran las 10:00 en España en
   verano. Está puesto `0 6 * * *` → 08:00 en verano, 07:00 en invierno.

5. **Prueba ya**: pestaña *Actions* → *Rastrear Precios* → *Run workflow*.
   Si algo falla, el log dice exactamente qué y `estado.json` queda en el repo
   con el detalle por tienda. El HTML de las respuestas bloqueadas se sube como
   artefacto `html-bloqueado`.

## 4. Añadir una tienda nueva

En la lista `FUENTES` de `scraper.py`:

```python
(
    "Mi tienda",
    lambda cli: jsonld_generico(cli, "Mi tienda", "https://tienda.com/silla-gesture"),
),
```

Si es Shopify (mira si el HTML menciona `cdn.shopify.com`), usa mejor
`shopify_producto` o `shopify_busqueda`: son más fiables.

## 5. Si vuelven los bloqueos: runner propio

La solución definitiva y gratis a los bloqueos por IP es ejecutar el robot
desde tu propia conexión: *Settings → Actions → Runners → New self-hosted
runner*, lo instalas en tu PC o en una Raspberry, y en `robot.yml` cambias
`runs-on: ubuntu-latest` por `runs-on: self-hosted`. Mismo workflow, IP
doméstica, cero bloqueos. Solo requiere que la máquina esté encendida a esa
hora.

## 6. Un par de advertencias honestas

- El camino de **eBay no lo he podido probar** (no tengo credenciales).
  Revisa el primer `Run workflow` tras añadir los secrets.
- Hoy la Gesture **solo aparece en Steelcase oficial** (1.169 €). Las tres
  tiendas de reacondicionado no la tienen en catálogo ahora mismo: el robot
  las seguirá consultando y aparecerán en cuanto la pongan.
- El script espera entre 1,5 y 4 s entre tiendas a propósito. No lo bajes:
  ser educado es lo que mantiene el acceso abierto.

---

# App para el móvil y para el PC

Es **una sola página instalable** (PWA) que lee tu `datos.json` directamente
de GitHub. No hay que compilar nada ni pasar por ninguna tienda de apps.

- **Móvil**: se añade a la pantalla de inicio, con su icono, y se abre a
  pantalla completa sin barra del navegador.
- **PC**: se instala como ventana independiente (Chrome/Edge) y se puede
  anclar a la barra de tareas.
- **Sin cobertura**: guarda los últimos precios y los enseña avisando de que
  son datos guardados.

## Por qué GitHub Pages y no un artifact de Claude

Las páginas publicadas como artifact tienen una política de seguridad que
bloquea cualquier `fetch` a otro dominio, así que no podrían leer tu
`datos.json`. En GitHub Pages sí funciona: `raw.githubusercontent.com`
responde con `access-control-allow-origin: *` (comprobado).

## Pasos

1. Copia la carpeta **`docs/`** entera a la raíz del repo `ofertas-gesture`,
   incluido el archivo oculto `.nojekyll` (evita que GitHub procese la carpeta
   con Jekyll).

2. En el repo: *Settings → Pages → Build and deployment*
   - Source: **Deploy from a branch**
   - Branch: **main**, carpeta **/docs** → *Save*

3. Al minuto estará en
   **https://enderfairxd.github.io/ofertas-gesture/**

4. **Instalarla en el móvil**
   - Android (Chrome): abre la URL → menú ⋮ → *Añadir a pantalla de inicio* /
     *Instalar aplicación*.
   - iPhone (Safari): abre la URL → botón Compartir → *Añadir a pantalla de
     inicio*.

5. **Instalarla en el PC**
   - Chrome o Edge: abre la URL → icono de instalar en la barra de
     direcciones (o menú → *Instalar*). Se abre en su propia ventana y puedes
     anclarla a la barra de tareas.

## Qué muestra

- El **mejor precio** disponible ahora mismo, grande y arriba del todo.
- Una fila por tienda, de más barata a más cara, con el importe en euros y el
  original en libras debajo cuando corresponde.
- Una franja de color por estado: azul normal, ámbar cuando la tienda no
  respondió ese día (precio antiguo) y gris cuando aparece agotada.
- **Cuánto ha cambiado el mejor precio desde la última vez que abriste la
  app** (▼ bajada en verde, ▲ subida en ámbar).
- Un desplegable *Estado del robot* con lo que hizo cada fuente en la última
  ejecución, leído de `estado.json`.
- *Ajustes*, por si cambias el nombre del repo o de la rama.

Si abres la app antes de subir el scraper nuevo verás «Todavía no hay
precios»: es correcto, el `datos.json` del repo sigue con `[]`.

## ¿Quito la app de Streamlit?

Puedes quedártela, no estorba. La PWA la sustituye en los dos sitios y carga
bastante más rápido, porque no levanta un servidor Python para dibujar una
lista.
