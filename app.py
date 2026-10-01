import json
import os
from datetime import datetime, timezone

import streamlit as st

st.set_page_config(page_title="Rastreador Gesture", layout="centered")
st.title("🪑 Monitor de Ofertas: Steelcase Gesture")


def leer(nombre):
    if not os.path.exists(nombre):
        return None
    try:
        with open(nombre, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def cuando(iso):
    try:
        fecha = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or "desconocido"
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    horas = (datetime.now(timezone.utc) - fecha).total_seconds() / 3600
    if horas < 1:
        return "hace unos minutos"
    if horas < 48:
        return f"hace {int(horas)} h"
    return f"hace {int(horas // 24)} días"


datos = leer("datos.json") or []
estado = leer("estado.json") or {}

if not datos:
    st.info("El robot aún no ha publicado precios. Vuelve más tarde.")
else:
    ofertas = sorted(datos, key=lambda d: d.get("PrecioEUR") or d.get("Precio") or 1e9)
    mejor = ofertas[0]

    st.metric(
        "Mejor precio ahora",
        f"{mejor.get('PrecioEUR') or mejor['Precio']:,.0f} €".replace(",", "."),
        help=f"En {mejor['Tienda']}",
    )
    st.divider()

    for item in ofertas:
        with st.container(border=True):
            eur = item.get("PrecioEUR")
            precio = f"{eur:,.2f} €".replace(",", "@").replace(".", ",").replace("@", ".")
            cabecera = f"### {precio}" if eur else f"### {item['Precio']} {item.get('Moneda', '')}"
            if eur and item.get("Moneda") not in (None, "EUR"):
                cabecera += f"  ·  <small>{item['Precio']} {item['Moneda']}</small>"
            st.markdown(cabecera, unsafe_allow_html=True)

            st.write(f"**{item['Tienda']}** — {item.get('Producto', '')}")

            marca = item.get("Estado", "ok")
            if marca == "obsoleto":
                st.warning(
                    f"La tienda no respondió hoy. Último precio visto {cuando(item.get('Última actualización'))}.",
                    icon="⚠️",
                )
            elif marca == "sin stock" or item.get("Disponible") is False:
                st.warning("Aparece agotada.", icon="📦")
            else:
                st.caption(f"Comprobado {cuando(item.get('Última actualización'))}")

            st.link_button("Ir a la oferta", item["Enlace"])

if estado:
    with st.expander("Estado del robot"):
        st.caption(f"Última ejecución: {cuando(estado.get('ejecutado'))}")
        if estado.get("conservado_sin_cambios"):
            st.error("La última ejecución no obtuvo ningún precio; se muestran los anteriores.")
        for fuente in estado.get("fuentes", []):
            if fuente["ok"] and fuente["ofertas"]:
                icono = "✅"
            elif fuente.get("omitida"):
                icono = "⏭️"
            elif fuente["ok"]:
                icono = "➖"
            else:
                icono = "❌"
            st.write(f"{icono} **{fuente['nombre']}** — {fuente.get('detalle') or 'ok'}")
