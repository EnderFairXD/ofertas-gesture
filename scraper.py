import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime

# Simulamos ser un navegador normal para que no nos bloqueen
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9"
}

# Aquí configuramos todas las tiendas que queremos vigilar
tiendas = [
    {
        "nombre": "Steelcase Oficial (Nueva)",
        "url": "https://es.steelcase.com/products/gesture",
        "selector": ".price-item--regular"
    },
    {
        "nombre": "Oficinas Montiel (Reacondicionada)",
        "url": "https://www.oficinasmontiel.com/sillas-ergonomicas/silla-steelcase-gesture-segunda-mano.html",
        "selector": ".price"
    },
    {
        "nombre": "The Office Crowd (Reacondicionada UK)",
        "url": "https://theofficecrowd.com/search?q=steelcase+gesture",
        "selector": ".price-item"
    },
    {
        "nombre": "Corporate Spec (Reacondicionada UK)",
        "url": "https://corporatespec.com/?s=steelcase+gesture&post_type=product",
        "selector": ".woocommerce-Price-amount"
    },
    {
        "nombre": "eBay Europa (Segunda mano)",
        "url": "https://www.ebay.es/sch/i.html?_nkw=steelcase+gesture+silla",
        "selector": ".s-item__price"
    }
]

resultados = []

for tienda in tiendas:
    try:
        respuesta = requests.get(tienda["url"], headers=headers, timeout=10)
        sopa = BeautifulSoup(respuesta.text, 'html.parser')
        precio_elem = sopa.select_one(tienda["selector"])
        
        if precio_elem:
            # Limpiamos el texto (quitamos el símbolo €, puntos de miles, etc.)
            texto = precio_elem.text.lower().replace('€', '').replace('eur', '')
            texto = texto.replace('.', '').replace(',', '.').strip()
            
            # Extraemos solo los números por si pone cosas como "Desde 500€"
            numeros = re.findall(r'\d+\.?\d*', texto)
            if numeros:
                precio_final = float(numeros[0])
                resultados.append({
                    "Tienda": tienda["nombre"],
                    "Precio": precio_final,
                    "Enlace": tienda["url"],
                    "Última actualización": datetime.now().strftime("%d/%m/%Y %H:%M")
                })
    except Exception as e:
        print(f"No se pudo revisar {tienda['nombre']}")

# Guardamos los resultados
with open('datos.json', 'w') as f:
    json.dump(resultados, f)
