import streamlit as st
import pandas as pd
import json
import os

st.set_page_config(page_title="Rastreador Gesture", layout="centered")
st.title("🪑 Monitor de Ofertas: Steelcase Gesture")

if os.path.exists('datos.json'):
    with open('datos.json', 'r') as f:
        datos = json.load(f)
    
    if datos:
        df = pd.DataFrame(datos)
        for _, item in df.iterrows():
            with st.container(border=True):
                st.markdown(f"### {item['Precio']} €")
                st.write(f"**Tienda:** {item['Tienda']}")
                st.caption(f"Actualizado: {item['Última actualización']}")
                st.link_button("Ir a la oferta", item['Enlace'])
    else:
        st.warning("El robot no ha encontrado precios hoy.")
else:
    st.info("El robot aún no ha pasado. Vuelve en un rato.")
