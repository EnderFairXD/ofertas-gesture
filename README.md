# Rastreador de precios

Vigila cada día el precio de dos productos en varias tiendas y me avisa cuando bajan:

| Producto | Qué se rastrea |
|---|---|
| **Steelcase Gesture** (silla de oficina) | Tiendas de España, Reino Unido y EE. UU., nuevas y reacondicionadas, y eBay |
| **Beyerdynamic TYGR 300 R** (auriculares) | Tiendas de la UE, solo productos nuevos |

Corre solo en GitHub Actions y publica los datos en una web instalable (PWA).

## Cómo funciona

- **Rastreo programado** (`scraper.py`, workflow `robot.yml`): una vez al día, y cada 4 horas en
  noviembre, diciembre y la primera semana de enero (rebajas).
- **Varios productos**: cada uno trae su lista de tiendas, su filtro de títulos y su precio objetivo.
  Añadir otro es añadir una entrada a la lista `PRODUCTOS`.
- **Fuentes de datos**: JSON de Shopify, JSON-LD y microdatos de schema.org y la Browse API de
  eBay, en vez de raspar HTML frágil.
- **Precio puesto en casa**: compara con envío e IVA de importación incluidos, no con el precio de
  escaparate.
- **Nunca publica vacío**: si una tienda falla conserva su último precio marcado como obsoleto; si
  fallan todas, el workflow se pone en rojo.
- **Avisos** por Telegram y por incidencia de GitHub, cuando baja del precio objetivo o cae mucho
  por debajo de su precio habitual.
- **Panel**: `app.py` (Streamlit) y la web de `docs/`, publicada en GitHub Pages con un selector de
  producto, la gráfica de precio y los enlaces a cada tienda.

El diagnóstico y las decisiones de diseño están en [`LEEME-SOLUCION.md`](LEEME-SOLUCION.md).

## Configuración

Los precios objetivo se guardan como secretos del repositorio (`UMBRAL_GESTURE` y `UMBRAL_TYGR`),
así que no aparecen en el código ni en la web. Sin secreto, ese producto solo avisa por caída de
precio. El resto de secretos (Telegram, eBay) están descritos en el workflow.

## Ejecutar en local

```bash
pip install -r requirements.txt            # panel
pip install -r requirements-scraper.txt    # rastreador
streamlit run app.py
```

## Licencia

MIT © Javier Cortés Vidal
