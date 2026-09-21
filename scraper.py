import cloudscraper
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime

# Creamos el scraper avanzado para saltar protecciones
scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True})

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
        # Usamos el scraper avanzado en lugar de requests normal
        respuesta = scraper.get(tienda["url"], timeout=15)
        sopa = BeautifulSoup(respuesta.text, 'html.parser')
        precio_elem = sopa.select_one(tienda["selector"])
        
        if precio_elem:
            texto = precio_elem.text.lower().replace('€', '').replace('eur', '').replace('£', '')
            texto = texto.replace('.', '').replace(',', '.').strip()
            
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
        print(f"Error en {tienda['nombre']}: {e}")

with open('datos.json', 'w') as f:
    json.dump(resultados, f)
