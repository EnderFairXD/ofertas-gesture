# Rastreador de ofertas: Steelcase Gesture

Rastrea cada día el precio de la silla Steelcase Gesture en varias tiendas y avisa cuando baja.
Corre solo en GitHub Actions y publica los datos en una PWA.

## Cómo funciona

- **Rastreo programado** (`scraper.py`, workflow `robot.yml`): una vez al día, y cada 4 horas en
  noviembre, diciembre y la primera semana de enero (rebajas).
- **Fuentes de datos**: JSON de Shopify, JSON-LD de schema.org y la Browse API de eBay, en vez de
  raspar HTML frágil.
- **Nunca publica vacío**: si una tienda falla conserva su último precio marcado como obsoleto; si
  fallan todas, el workflow se pone en rojo.
- **Avisos** por Telegram y por incidencia de GitHub.
- **Panel**: `app.py` (Streamlit) y la PWA de `docs/` publicada en GitHub Pages.

El diagnóstico y las decisiones de diseño están en [`LEEME-SOLUCION.md`](LEEME-SOLUCION.md).

## Ejecutar en local

```bash
pip install -r requirements.txt            # panel
pip install -r requirements-scraper.txt    # rastreador
streamlit run app.py
```

## Licencia

MIT © Javier Cortés Vidal
