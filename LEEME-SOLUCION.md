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


---

# Tiendas rastreadas y cómo añadir más

Sondeé una docena de candidatas y estas son las que se dejan leer, con el
adaptador que usa cada una:

| Tienda | País | Plataforma | Adaptador |
|---|---|---|---|
| Steelcase Oficial | ES | Shopify | `shopify_tienda` |
| The Office Crowd | ES | Shopify | `shopify_tienda` |
| The Office Crowd | UK | Shopify | `shopify_tienda` |
| Chair Smith | UK | WooCommerce | `woocommerce_tienda` |
| Barkham Office Furniture | UK | ASP clásico | `jsonld_generico` |
| Office Logix Shop | EE. UU. | Shopify | `shopify_tienda` |
| Oficinas Montiel | ES | PrestaShop | `prestashop_busqueda` |
| eBay | ES | API oficial | `ebay_api` |

Descartadas por rechazar a los robots: **Corporate Spec** y **PcComponentes**
(reto de Cloudflare incluso desde una IP doméstica), **Wallapop** y
**Milanuncios** (su API exige cabeceras firmadas), **spacio.es** (403 a todo)
y **2ndhnd.com** (el dominio no resuelve). Las cuatro primeras aparecen en la
app como enlaces para mirarlas a mano, con la búsqueda ya hecha.

## Añadir una tienda

Una línea en la lista `FUENTES` de `scraper.py`. Para saber qué adaptador toca,
mira el código fuente de su portada:

- Si pone `cdn.shopify.com` → `shopify_tienda` con el `handle` de la ficha
  (lo que va detrás de `/products/`). Si no sabes el handle, no lo pongas: se
  usará el buscador de la tienda.
- Si pone `woocommerce` → `woocommerce_tienda`.
- Si no, prueba `jsonld_generico` con la URL de la ficha. Funciona en cualquier
  tienda que publique `schema.org/Product`, que hoy son casi todas.

```python
(
    "Mi tienda (reacond. UK)",
    shopify_tienda,
    {"dominio": "https://mitienda.com", "handle": "steelcase-gesture", "moneda": "GBP"},
),
```

El filtro de títulos descarta solo (fundas, ruedas, taburetes, pistones de gas)
y el conversor pasa libras y dólares a euros con el cambio del día.

# Buscar ahora desde la app

El robot pasa cada mañana, pero la app trae un botón **Buscar ahora**:

- **Sin token** (por defecto): abre la página de Actions del repo para que
  pulses *Run workflow* tú mismo, ya identificado.
- **Con token**: lanza el workflow sin salir de la app, te enseña el progreso
  y recarga los precios en cuanto termina (en torno a un minuto).

Para lo segundo, en *Ajustes* hay un campo de token. Créalo en GitHub como
**fine-grained personal access token** con el alcance más estrecho posible:
solo el repositorio `ofertas-gesture` y solo el permiso **Actions: read and
write**. Ponle caducidad. Se guarda únicamente en el `localStorage` de tu
navegador y solo se manda a `api.github.com`; si usas la app desde un
dispositivo que no es tuyo, no lo guardes.


---

# Zona de entrega: por qué el precio más bajo no siempre vale

El rastreador encontraba sillas a 520 € y la app las ponía de titular. Al
comprobar las políticas de envío de cada tienda, resulta que buena parte de
esas gangas no te las pueden mandar:

| Tienda | Entrega | Comprobado en |
|---|---|---|
| Steelcase Oficial | España | tienda española |
| Oficinas Montiel | España | tienda española |
| The Office Crowd (ES y UK) | España, con importación | "Realizamos envíos a […] España"; los aranceles e impuestos se cobran en el checkout |
| Office Logix Shop | con importación | "International Shipping is now available at additional fees" |
| **Chair Smith** | **no** | su página de entregas solo ofrece "FREE SHIPPING WITHIN LONDON M25" |
| **Barkham Office Furniture** | **no** | "Free Chair Delivery to UK Mainland", sin envíos fuera |
| eBay | según el anuncio | el país sale de `itemLocation.country` de cada anuncio |

En la app, cada oferta lleva ahora su distintivo, las que no llegan a España
caen al final con el borde punteado, y **el precio destacado solo tiene en
cuenta lo que está en stock y además te pueden entregar**.

## Y un efecto secundario que no esperaba

Las tiendas Shopify cotizan según el mercado de la sesión. El robot corre en
un centro de datos de EE. UU., así que Office Logix le respondía **620 $**
mientras que a ti, desde España, te ofrece **560,95 €**. Comprobado fijando la
cookie `localization`:

```
localization=US -> USD 620.00
localization=ES -> EUR 560.95
localization=GB -> USD 620.00
```

El scraper manda ahora `localization=ES` en todas las peticiones, así que los
precios que publica son los que te aplican a ti, no los del país del servidor.


---

# Gastos de envío: el precio puesto en casa

Comparar precios de escaparate entre países no sirve de nada. Shopify permite
pedir la tarifa real de envío a una dirección concreta, así que el robot la
pide y la app compara **lo que acabas pagando**:

```
Office Logix Shop   560,95 €  +  316,95 € de envío  +  21 % de IVA  =  1.062,26 €
Steelcase Oficial  1169,00 €  +  envío gratis                      =  1.169,00 €
```

Una reacondicionada de Ohio sale 107 € más barata que una nueva con garantía
entregada en España. Visto así, la decisión es otra.

## Cómo se obtiene

`/cart/shipping_rates.json` de Shopify calcula la tarifa sobre el carrito, así
que el robot mete la silla en uno, pide la tarifa y lo vacía. Un carrito es
efímero y vive en la sesión del propio robot: no encarga ni compra nada.

Solo hay tarifa si la variante está en stock. Con el producto agotado la app
dice «envío sin calcular» en lugar de inventarse una cifra.

El destino por defecto es Madrid capital (28013). Cámbialo con las variables
`CP_DESTINO`, `PROVINCIA_DESTINO` y `PAIS_DESTINO` si vives lejos, porque
algunas tiendas cobran por zona.

## Lo que el cálculo NO incluye

El 21 % es el IVA de importación español, que se aplica sobre el valor en
aduana (mercancía + transporte). **No** están contados los aranceles ni los
gastos de despacho que cobra el transportista, que en un envío desde EE. UU.
suelen ser entre 15 y 40 € más. Es decir: la cifra es un suelo, no un techo.


---

# Historial y gráfica

El robot escribe `historico.json`: un registro por día con lo más barato que
**podías comprar de verdad** ese día, es decir en stock y con entrega en
España, usando el precio puesto en casa. Si el robot pasa varias veces en una
jornada se queda con el mínimo de esa jornada. Guarda 365 días; el archivo
crece unos 100 bytes al día.

```json
{ "fecha": "2026-10-01", "mejor": 1061.05,
  "tienda": "Office Logix Shop (reacond. EE. UU.)",
  "tiendas": { "Office Logix Shop (reacond. EE. UU.)": 1061.05,
               "Steelcase Oficial (ES)": 1169.0 } }
```

En la app aparece una sección nueva con tres cifras —mínimo registrado, media
de 30 días y cuánto estás por encima del mínimo ahora— y la curva.

## Decisiones de la gráfica

- **La curva sale a partir del tercer día.** Con uno o dos puntos una línea no
  dice nada, así que hasta entonces se ven solo las cifras y un aviso de
  cuántos días llevan medidos.
- **El SVG se dibuja a la medida real del contenedor**, no con un `viewBox`
  fijo que luego se escala: así el texto de los ejes mide once píxeles de
  verdad también en el móvil. Se redibuja al cambiar el tamaño de la ventana.
- **Color validado, no elegido a ojo.** El azul de la interfaz suspendía el
  umbral de saturación para una línea de datos, así que la serie usa `#0b6e9c`
  en claro y `#3f9fd4` en oscuro: ambos pasan banda de luminosidad, suelo de
  saturación y contraste contra su superficie.
- **Etiquetas selectivas**: solo el valor de hoy y el mínimo. El resto lo
  cuentan el eje, el globo al pasar el dedo o el ratón, y la tabla de
  «Ver los datos», que está ahí para que ningún dato viva solo en la gráfica.
- Rejilla de línea fina y continua, relleno al 12 %, línea de 2 px y puntos
  con anillo del color del fondo.


---

# Aviso cuando baje de 750 €

El robot compara el umbral contra **lo más barato que puedes comprar de
verdad**: en stock, con entrega en España y al precio puesto en casa (envío e
IVA de importación incluidos). No contra el precio de escaparate.

## Canales

- **Incidencia en GitHub** — funciona desde ya, sin configurar nada. El robot
  abre una incidencia en el repositorio y GitHub te la manda por correo y por
  su app móvil. Usa el `GITHUB_TOKEN` que el propio workflow ya tiene.
- **Telegram** — opcional, dos minutos de preparación:
  1. En Telegram, habla con **@BotFather** y manda `/newbot`. Te da un token.
  2. Escríbele algo a tu bot recién creado.
  3. Abre `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y copia el
     `chat.id` que aparece.
  4. En el repo, *Settings → Secrets and variables → Actions*, crea
     `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.

  Si no los pones, ese canal simplemente no se usa.

## Cuándo avisa y cuándo se calla

Guarda en `avisos.json` el último precio avisado, con estas reglas:

| Situación | Qué hace |
|---|---|
| Baja del umbral por primera vez | **avisa** |
| Sigue por debajo, pero igual o más caro que el último aviso | calla |
| Baja todavía más | **avisa** |
| Vuelve a subir por encima del umbral | calla y se rearma |
| Vuelve a bajar después de rearmarse | **avisa** |

Comprobado con esa secuencia exacta: tres avisos en los tres momentos
correctos, cada uno por los dos canales.

## Cambiar el umbral

Está en `.github/workflows/robot.yml`, variable `UMBRAL_AVISO`. La app enseña
el valor configurado debajo de la gráfica, leyéndolo de `estado.json`.

## Qué cuenta para el aviso

Solo lo que puedes comprar y además te llega:

| Caso | ¿Avisa? |
|---|---|
| Tienda española, con o sin tarifa de envío | sí |
| Desde la UE | sí |
| De fuera de la UE **con** tarifa real de envío | sí (el total ya lleva envío e IVA) |
| De fuera de la UE **sin** tarifa de envío | **no** |
| Solo entrega local (Londres M25, Reino Unido peninsular) | **no** |
| Agotada | **no** |

La cuarta fila es la importante. Office Logix anuncia 560 € de escaparate y
cuesta 1.062 € puesta en casa: sin la tarifa real de envío, avisar de esos
560 € sería mentir. Si la tienda es española y no da tarifa, sí se avisa con
el precio a secas, pero el mensaje lo dice.

## Por qué 750 €

Las Gesture de particular que aparecen en Wallapop rondan los 710-800 €, así
que 750 € puestos en casa es el filo de lo que es una buena compra aquí. Con
500 € el aviso no habría saltado nunca.


---

# Dos productos: la silla y los auriculares

El robot ya no sigue un solo artículo. En `scraper.py` hay una lista
`PRODUCTOS`, y cada uno trae sus tiendas, su filtro de títulos y su umbral:

| Producto | Umbral | Tiendas |
|---|---|---|
| Steelcase Gesture | 750 € | 8 (España, UK, EE. UU., eBay) |
| Beyerdynamic TYGR 300 R | 140 € | 3, **solo UE y solo nuevos** |

Los TYGR llevan `"solo_ue": True`: si alguna fuente devolviera una oferta de
fuera de la Unión Europea, se descarta antes de publicarla. Nada de aduanas.

Fuentes de los auriculares, todas comprobadas:

- **Thomann (DE)** — 158 €. Publica el precio en microdatos (`itemprop`), no en
  JSON-LD, así que se añadió ese lector al extractor genérico.
- **Beyerdynamic oficial (UE)** — 159 €.
- **Beyerdynamic B-Stock (UE)** — **109 €**, reacondicionados de la propia marca.

En la app aparece un selector arriba para cambiar de producto; cada uno tiene
su precio destacado, su lista, su curva, su umbral y sus enlaces manuales.

**Pendiente**: ninguna de las tres tiendas de auriculares publica una tarifa de
envío consultable, así que ahí la app dice «envío sin calcular». Beyerdynamic
anuncia "Free Shipping*" con asterisco y no me fío de dar por hecho el cero.

## Formato de los archivos

`datos.json` gana un campo `Articulo`. `historico.json` y `avisos.json` pasan a
ser diccionarios por producto. Los formatos antiguos se migran solos: una lista
pelada se interpreta como la silla.

# Avisos en rebajas

Además del umbral, hay un segundo motivo de aviso: **que el precio caiga
bastante por debajo de su precio habitual**, calculado como la mediana de los
últimos 30 días (mediana y no media, para que un día raro no mueva la
referencia). Hacen falta al menos 5 días medidos.

- Todo el año: avisa con una caída del **12 %**.
- En **Black Friday** (17 nov – 2 dic) y **Navidad y Reyes** (18 dic – 7 ene):
  basta un **7 %**, y el aviso lleva el nombre de la temporada en el título.

Y como en rebajas los precios duran horas, el workflow cambia de ritmo solo:

```yaml
- cron: '0 6 * * *'          # todo el año, una vez al día
- cron: '0 */4 * 11,12 *'    # noviembre y diciembre, cada 4 horas
- cron: '0 */4 1-7 1 *'      # primera semana de enero
```

La app enseña una banda arriba cuando está dentro de una de esas ventanas.


---

# Los auriculares: solo nuevos y solo donde se deja

`"solo_ue": True` descarta cualquier oferta de fuera de la Unión. Y el filtro
de títulos descarta además `b-stock`, `refurbished`, `reacondicionado`,
`segunda mano`, `usado` y `open box`: solo producto de primera mano. Por eso
se retiró la fuente de B-Stock de la propia Beyerdynamic (109 €) y la de eBay.

| Tienda | Estado | Por qué |
|---|---|---|
| **Thomann (DE)** | rastreada, 158 € | microdatos `itemprop` |
| **Amazon.es** | rastreada, 158 € | su robots.txt permite `/dp/<ASIN>`; solo prohíbe subrutas como `/dp/rate-this-item/` |
| **Beyerdynamic oficial (UE)** | rastreada, 159 € | JSON-LD; es el precio de referencia del fabricante |
| **Madrid Hifi** | enlace manual | reto de Cloudflare incluso desde una conexión doméstica |
| **PcComponentes** | enlace manual | lo mismo |

Amazon va marcada como **tolerante**: corta el paso a menudo cuando la
petición no sale de una conexión doméstica, así que su fallo se informa en el
estado del robot pero no pone el workflow en rojo. Las demás sí lo ponen.

Madrid Hifi y PcComponentes rechazan clientes automáticos de forma explícita,
así que no las fuerzo: aparecen en la app como enlace con la búsqueda hecha.
