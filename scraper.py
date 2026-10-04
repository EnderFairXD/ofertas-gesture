#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rastreador de precios de la silla Steelcase Gesture.

Idea central: en lugar de raspar HTML con selectores CSS (que se rompen al
cambiar el tema de la tienda y obligan a pelearse con Cloudflare), se piden
los datos por las vías que cada tienda ya publica para máquinas:

  * Shopify  -> /products/<handle>.js  y  /search/suggest.json   (JSON oficial)
  * PrestaShop y resto -> JSON-LD de schema.org incrustado en la página
  * eBay     -> Browse API oficial (gratis, 5.000 llamadas/día)

Además, nunca se sobrescribe un datos.json bueno con una lista vacía:
si una fuente falla se conserva el último precio conocido y se marca
como obsoleto.

Variables de entorno (todas opcionales):
  EBAY_CLIENT_ID / EBAY_CLIENT_SECRET  credenciales de la Browse API de eBay
  EBAY_MARKETPLACE                     EBAY_ES por defecto
  SCRAPERAPI_KEY                       clave de ScraperAPI (plan gratis 1.000/mes)
  PROXY_URL                            proxy HTTP(S) alternativo
  DIAS_CADUCIDAD                       descartar ofertas más viejas (14 por defecto)
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

RAIZ = Path(__file__).resolve().parent
ARCHIVO_DATOS = RAIZ / "datos.json"
ARCHIVO_ESTADO = RAIZ / "estado.json"
ARCHIVO_HISTORICO = RAIZ / "historico.json"
ARCHIVO_AVISOS = RAIZ / "avisos.json"
DIR_DEBUG = RAIZ / "debug"

TIMEOUT = 25
REINTENTOS = 3
DIAS_CADUCIDAD = int(os.getenv("DIAS_CADUCIDAD", "14"))
DIAS_HISTORICO = 365

# Con esto puesto a 1 se escribe el aviso en el log pero no se manda a nadie.
AVISO_SIMULADO = os.getenv("AVISO_SIMULADO", "") == "1"
# Con esto a 1 se manda un aviso de prueba, para comprobar que Telegram llega.
AVISO_PRUEBA = os.getenv("AVISO_PRUEBA", "") == "1"
# Por dónde avisar. Quita "incidencia" si no quieres que abra incidencias.
AVISO_CANALES = [
    c.strip() for c in os.getenv("AVISO_CANALES", "telegram,incidencia").split(",")
    if c.strip()
]

# Rebajas: en estas ventanas los precios se mueven en horas, así que el aviso
# por bajada relativa se vuelve más sensible y el mensaje lo dice.
TEMPORADAS = (
    ("Black Friday", (11, 17), (12, 2)),
    ("Navidad y Reyes", (12, 18), (1, 7)),
)
CAIDA_NORMAL = 0.12      # 12 % por debajo de su precio habitual
CAIDA_TEMPORADA = 0.07   # en rebajas basta con un 7 %
DIAS_MINIMOS_PARA_COMPARAR = 5

# Las tiendas Shopify cotizan según el mercado de la sesión: desde el runner
# de GitHub (centro de datos en EE. UU.) la misma silla sale en dólares y a
# otro precio. Esta cookie fija el mercado español, que es el que te aplica.
COOKIES_PAIS = {"localization": "ES"}

# Dirección de entrega para pedir tarifas reales de envío. Cámbiala por la
# tuya si vives lejos de Madrid: algunas tiendas cobran por zona.
DESTINO = {
    "shipping_address[country]": os.getenv("PAIS_DESTINO", "Spain"),
    "shipping_address[province]": os.getenv("PROVINCIA_DESTINO", "Madrid"),
    "shipping_address[zip]": os.getenv("CP_DESTINO", "28013"),
}

# IVA de importación que aplica España a lo que entra de fuera de la UE. Se
# calcula sobre el valor en aduana, que incluye el transporte. No incluye
# aranceles ni los gastos de despacho que cobra el transportista.
IVA_IMPORTACION = 1.21

CABECERAS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "no-cache",
}


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def log(msg: str) -> None:
    print(f"[{ahora():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #


class SinResultados(Exception):
    """La tienda contestó bien, pero no tiene (o ya no tiene) el producto.
    No es un fallo: si antes aparecía, debe retirarse del listado."""


# Qué hace falta para que la silla llegue a tu casa:
#   "es"      tienda española, sin más
#   "ue"      desde la Unión Europea, sin aduanas
#   "importa" fuera de la UE: suma transporte, IVA de importación y aranceles
#   "no"      no entrega en España
ENTREGA_ES = "es"
ENTREGA_UE = "ue"
ENTREGA_IMPORTA = "importa"
ENTREGA_NO = "no"

DESCRIPCION_ENTREGA = {
    ENTREGA_ES: "tienda española",
    ENTREGA_UE: "desde la Unión Europea, sin aduanas",
    ENTREGA_IMPORTA: "de fuera de la UE, con importación ya incluida en el total",
}

PAISES_UE = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "SE",
}


@dataclass
class Oferta:
    tienda: str
    producto: str
    precio: float
    moneda: str
    enlace: str
    disponible: bool = True
    entrega: str = ENTREGA_ES
    envio: float | None = None  # en la misma moneda que el precio
    articulo: str = ""

    def a_dict(self, cambio: dict[str, float]) -> dict:
        tasa = cambio.get(self.moneda, 1.0 if self.moneda == "EUR" else None)
        precio_eur = round(self.precio * tasa, 2) if tasa else None
        envio_eur = (
            round(self.envio * tasa, 2) if (tasa and self.envio is not None) else None
        )
        # Lo que de verdad te cuesta ponerla en casa.
        total_eur = None
        if precio_eur is not None and envio_eur is not None:
            total_eur = precio_eur + envio_eur
            if self.entrega == ENTREGA_IMPORTA:
                total_eur *= IVA_IMPORTACION
            total_eur = round(total_eur, 2)
        return {
            "Articulo": self.articulo,
            "Tienda": self.tienda,
            "Producto": self.producto,
            "Precio": round(self.precio, 2),
            "Moneda": self.moneda,
            "PrecioEUR": precio_eur,
            "Envio": round(self.envio, 2) if self.envio is not None else None,
            "EnvioEUR": envio_eur,
            "TotalEUR": total_eur,
            "Enlace": self.enlace,
            "Disponible": self.disponible,
            "Entrega": self.entrega,
            "Estado": "ok" if self.disponible else "sin stock",
            "Última actualización": ahora().isoformat(timespec="seconds"),
        }


@dataclass
class Resultado:
    """Qué ha pasado con una fuente en esta ejecución."""

    nombre: str
    articulo: str = ""
    tolerante: bool = False
    ofertas: list[Oferta] = field(default_factory=list)
    error: str | None = None
    nota: str | None = None
    omitida: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def limpiar(texto: str) -> str:
    """Quita etiquetas HTML y espacios de más. The Office Crowd ES sirve los
    títulos con marcas de traducción del tipo <tc>Steelcase</tc>."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", texto or "")).strip()


def interesa(titulo: str, producto: dict) -> bool:
    """¿Este título es el producto que buscamos, y no un accesorio suyo?"""
    t = titulo.lower()
    if not all(term in t for term in producto["obligatorios"]):
        return False
    return not any(term in t for term in producto["excluidos"])


# --------------------------------------------------------------------------- #
# Cliente HTTP con reintentos y proxy opcional
# --------------------------------------------------------------------------- #


class Cliente:
    def __init__(self) -> None:
        self.sesion = requests.Session()
        self.sesion.headers.update(CABECERAS_BASE)
        self.clave_scraperapi = os.getenv("SCRAPERAPI_KEY", "").strip()
        self.proxy = os.getenv("PROXY_URL", "").strip()

    # -- envoltorios de proxy ------------------------------------------------ #

    def _url_scraperapi(self, url: str, render: bool) -> str:
        params = {
            "api_key": self.clave_scraperapi,
            "url": url,
            "country_code": "es",
        }
        if render:
            params["render"] = "true"
        return "https://api.scraperapi.com/?" + urllib.parse.urlencode(params)

    def get(
        self,
        url: str,
        *,
        json_esperado: bool = False,
        render: bool = False,
        usar_proxy: bool = False,
    ) -> requests.Response:
        """GET con reintentos. Si recibe 403/429/5xx reintenta y, si hay
        ScraperAPI o PROXY_URL configurados, repite la petición por ahí."""
        ultimo_error: Exception | None = None
        for intento in range(1, REINTENTOS + 1):
            por_proxy = usar_proxy or (intento > 1 and bool(self.clave_scraperapi))
            destino = url
            proxies = None
            if por_proxy and self.clave_scraperapi:
                destino = self._url_scraperapi(url, render)
            elif por_proxy and self.proxy:
                proxies = {"http": self.proxy, "https": self.proxy}

            cabeceras = {}
            if json_esperado:
                cabeceras["Accept"] = "application/json, text/javascript;q=0.9,*/*;q=0.8"
                cabeceras["Sec-Fetch-Dest"] = "empty"
                cabeceras["Sec-Fetch-Mode"] = "cors"
                cabeceras["Sec-Fetch-Site"] = "same-origin"
                cabeceras["Referer"] = f"{urllib.parse.urlsplit(url).scheme}://{urllib.parse.urlsplit(url).netloc}/"

            try:
                r = self.sesion.get(
                    destino,
                    timeout=TIMEOUT,
                    headers=cabeceras,
                    proxies=proxies,
                    cookies=COOKIES_PAIS,
                )
            except requests.RequestException as exc:
                ultimo_error = exc
                log(f"    intento {intento}: error de red ({exc.__class__.__name__})")
            else:
                if r.status_code < 400:
                    return r
                ultimo_error = requests.HTTPError(
                    f"HTTP {r.status_code}"
                    + (" (reto anti-bot)" if r.headers.get("cf-mitigated") else "")
                )
                log(
                    f"    intento {intento}: HTTP {r.status_code}"
                    f"{' vía proxy' if por_proxy else ''}"
                )
                guardar_debug(url, r)
                if r.status_code in (401, 404, 410):
                    break  # no se arregla reintentando

            if intento < REINTENTOS:
                time.sleep(2 * intento + random.uniform(0, 1.5))

        raise RuntimeError(f"no se pudo descargar {url}: {ultimo_error}")

    def json(self, url: str, **kw) -> dict:
        r = self.get(url, json_esperado=True, **kw)
        try:
            return r.json()
        except ValueError as exc:
            raise RuntimeError(
                "la respuesta no es JSON (content-type="
                + r.headers.get("content-type", "?")
                + ", content-encoding="
                + r.headers.get("content-encoding", "-")
                + f"): {r.text[:60]!r}"
            ) from exc


def guardar_debug(url: str, r: requests.Response) -> None:
    """Guarda la respuesta fallida para poder mirarla luego como artefacto
    del workflow en lugar de adivinar por qué falló."""
    try:
        DIR_DEBUG.mkdir(exist_ok=True)
        nombre = re.sub(r"[^a-z0-9]+", "-", urllib.parse.urlsplit(url).netloc.lower())
        (DIR_DEBUG / f"{nombre}-{r.status_code}.html").write_text(
            r.text[:200_000], encoding="utf-8", errors="ignore"
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Utilidades de extracción
# --------------------------------------------------------------------------- #


def bloques_jsonld(html: str) -> list:
    """Devuelve todos los objetos JSON-LD de una página, aplanando @graph."""
    sopa = BeautifulSoup(html, "html.parser")
    salida: list = []
    for script in sopa.find_all("script", type=lambda v: v and "ld+json" in v):
        crudo = (script.string or script.get_text() or "").strip()
        if not crudo:
            continue
        try:
            dato = json.loads(crudo)
        except json.JSONDecodeError:
            # Algunos temas dejan comas finales o varios objetos seguidos.
            try:
                dato = json.loads(re.sub(r",\s*([}\]])", r"\1", crudo))
            except json.JSONDecodeError:
                continue
        pila = [dato]
        while pila:
            actual = pila.pop()
            if isinstance(actual, list):
                pila.extend(actual)
            elif isinstance(actual, dict):
                salida.append(actual)
                if "@graph" in actual:
                    pila.append(actual["@graph"])
    return salida


def precio_desde_jsonld(html: str) -> tuple[str | None, float | None, str, bool]:
    """(nombre, precio, moneda, disponible) a partir del JSON-LD de un producto."""
    for nodo in bloques_jsonld(html):
        tipos = nodo.get("@type", "")
        tipos = tipos if isinstance(tipos, list) else [tipos]
        if not any(str(t).startswith("Product") for t in tipos):
            continue
        ofertas = nodo.get("offers") or []
        ofertas = ofertas if isinstance(ofertas, list) else [ofertas]
        candidatos: list[tuple[float, str, bool]] = []
        for of in ofertas:
            if not isinstance(of, dict):
                continue
            if str(of.get("@type", "")).endswith("AggregateOffer"):
                bruto = of.get("lowPrice") or of.get("price")
            else:
                bruto = of.get("price")
            # Las especificaciones de Shopify anidan ofertas en "offers".
            if bruto is None and isinstance(of.get("offers"), list):
                for sub in of["offers"]:
                    if isinstance(sub, dict) and sub.get("price") is not None:
                        candidatos.append(
                            (
                                float(sub["price"]),
                                sub.get("priceCurrency", "EUR"),
                                "outofstock" not in str(sub.get("availability", "")).lower(),
                            )
                        )
                continue
            if bruto is None:
                continue
            try:
                candidatos.append(
                    (
                        float(str(bruto).replace(",", ".")),
                        of.get("priceCurrency", "EUR"),
                        "outofstock" not in str(of.get("availability", "")).lower(),
                    )
                )
            except ValueError:
                continue
        if candidatos:
            precio, moneda, disponible = min(candidatos, key=lambda c: c[0])
            return nodo.get("name"), precio, moneda, disponible

    # Microdatos de schema.org: muchas tiendas grandes (Thomann, por ejemplo)
    # no publican JSON-LD pero sí marcan el precio con itemprop.
    micro = re.search(r'itemprop="price"[^>]*content="([^"]+)"', html)
    if micro:
        try:
            valor = float(micro.group(1).replace(".", "").replace(",", ".")
                          if "," in micro.group(1) else micro.group(1))
        except ValueError:
            valor = None
        if valor is not None:
            divisa = re.search(r'itemprop="priceCurrency"[^>]*content="([^"]+)"', html)
            dispo = re.search(r'itemprop="availability"[^>]*(?:href|content)="([^"]+)"', html)
            # El <title> nombra el producto; itemprop="name" suele caer en una
            # miga de pan ("Home") en tiendas grandes.
            nombre_micro = re.search(r"<title>(.*?)</title>", html, re.S) or re.search(
                r'itemprop="name"[^>]*content="([^"]+)"', html
            )
            return (
                limpiar(nombre_micro.group(1)) if nombre_micro else None,
                valor,
                divisa.group(1) if divisa else "EUR",
                "outofstock" not in (dispo.group(1).lower() if dispo else ""),
            )

    # Último recurso: metaetiquetas Open Graph.
    sopa = BeautifulSoup(html, "html.parser")
    meta = sopa.find("meta", property=re.compile(r"(product:price|og:price):amount"))
    if meta and meta.get("content"):
        texto = meta["content"].replace(".", "").replace(",", ".")
        try:
            div = sopa.find("meta", property=re.compile(r"price:currency"))
            return None, float(texto), (div or {}).get("content", "EUR"), True
        except ValueError:
            pass
    return None, None, "EUR", True


# --------------------------------------------------------------------------- #
# Adaptadores por tipo de tienda
# --------------------------------------------------------------------------- #


def _origen(url: str) -> str:
    partes = urllib.parse.urlsplit(url)
    return f"{partes.scheme}://{partes.netloc}"


_DIVISAS: dict[str, str] = {}


def shopify_divisa(cli: Cliente, dominio: str, por_defecto: str) -> str:
    """Shopify sirve los precios en la moneda de la sesión, que depende del
    país desde el que se pide: desde España una tienda británica puede
    responder en euros. /cart.js dice cuál está usando de verdad."""
    if dominio in _DIVISAS:
        return _DIVISAS[dominio]
    for ruta, campo in (("/cart.js", "currency"), ("/meta.json", "currency")):
        try:
            valor = cli.json(dominio + ruta).get(campo)
        except Exception:
            continue
        if valor:
            _DIVISAS[dominio] = valor
            return valor
    _DIVISAS[dominio] = por_defecto
    return por_defecto


def tarifa_envio_shopify(
    cli: Cliente, dominio: str, variante: int
) -> float | None:
    """Tarifa real de envío a tu dirección. Shopify la calcula sobre el
    carrito, así que hay que meter la silla en uno (un carrito es efímero y
    vive en nuestra propia sesión: no encarga nada ni compra nada).

    Solo funciona con variantes en stock; con el producto agotado devuelve
    None en vez de inventarse una cifra."""
    try:
        r = cli.sesion.post(
            f"{dominio}/cart/add.js",
            json={"id": variante, "quantity": 1},
            timeout=TIMEOUT,
            cookies=COOKIES_PAIS,
        )
        if r.status_code >= 400:
            return None
        r = cli.sesion.get(
            f"{dominio}/cart/shipping_rates.json",
            params=DESTINO,
            timeout=TIMEOUT + 15,
            cookies=COOKIES_PAIS,
        )
        if r.status_code >= 400:
            return None
        tarifas = r.json().get("shipping_rates") or []
    except Exception as exc:
        log(f"    sin tarifa de envío ({exc.__class__.__name__})")
        return None
    finally:
        try:  # dejar el carrito como estaba
            cli.sesion.post(f"{dominio}/cart/clear.js", timeout=TIMEOUT, cookies=COOKIES_PAIS)
        except Exception:
            pass
    precios = []
    for t in tarifas:
        try:
            precios.append(float(t.get("price")))
        except (TypeError, ValueError):
            continue
    return min(precios) if precios else None


def shopify_producto(
    cli: Cliente, tienda: str, producto: dict, url_producto: str, moneda: str = "EUR"
) -> list[Oferta]:
    """Una ficha concreta de una tienda Shopify. /products/<handle>.js devuelve
    el precio en céntimos y todas las variantes, sin HTML por medio."""
    base = url_producto.split("?")[0].rstrip("/")
    datos = cli.json(f"{base}.js")
    variantes = datos.get("variants") or []
    if not variantes:
        return []
    disponibles = [v for v in variantes if v.get("available")]
    barata = min(disponibles or variantes, key=lambda v: v.get("price") or 10**9)
    dominio = _origen(url_producto)
    envio = (
        tarifa_envio_shopify(cli, dominio, barata["id"])
        if barata.get("available")
        else None
    )
    return [
        Oferta(
            tienda=tienda,
            producto=limpiar(datos.get("title", "Gesture")),
            precio=barata["price"] / 100.0,  # Shopify da céntimos
            moneda=datos.get("price_currency") or shopify_divisa(cli, dominio, moneda),
            enlace=url_producto,
            disponible=bool(barata.get("available")),
            envio=envio,
            articulo=producto["id"],
        )
    ]


def shopify_tienda(
    cli: Cliente,
    tienda: str,
    producto: dict,
    dominio: str,
    moneda: str = "EUR",
    handle: str | None = None,
    consulta: str | None = None,
) -> list[Oferta]:
    """Primero la ficha conocida; si el enlace muere (cambian el handle o
    retiran el producto), se cae al buscador de la propia tienda."""
    if handle:
        try:
            return shopify_producto(
                cli, tienda, producto, f"{dominio}/products/{handle}", moneda
            )
        except Exception as exc:
            log(f"    ficha {handle} no sirve ({exc.__class__.__name__}), pruebo el buscador")
    return shopify_busqueda(
        cli, tienda, producto, dominio, consulta or producto["consulta"], moneda
    )


def shopify_busqueda(
    cli: Cliente, tienda: str, producto: dict, dominio: str, consulta: str, moneda: str
) -> list[Oferta]:
    """Buscador JSON de Shopify. Filtra por título para no colar otra silla."""
    url = (
        f"{dominio}/search/suggest.json?"
        + urllib.parse.urlencode(
            {
                "q": consulta,
                "resources[type]": "product",
                "resources[limit]": "10",
            }
        )
    )
    datos = cli.json(url)
    productos = (
        datos.get("resources", {}).get("results", {}).get("products", [])
    )
    ofertas = []
    for p in productos[:MAX_POR_TIENDA * 2]:
        titulo = p.get("title", "")
        if not interesa(titulo, producto):
            continue
        enlace = p.get("url", "")
        enlace = dominio + enlace.split("?")[0] if enlace.startswith("/") else enlace
        # Se pasa por la ficha: trae las variantes y permite pedir la tarifa
        # real de envío, cosa que el buscador no da.
        try:
            ofertas.extend(shopify_producto(cli, tienda, producto, enlace, moneda))
        except Exception as exc:
            log(f"    ficha {enlace.rsplit('/', 1)[-1]} ilegible ({exc.__class__.__name__})")
        if len(ofertas) >= MAX_POR_TIENDA:
            break
    if not ofertas and productos:
        raise SinResultados(
            f"la tienda respondió ({len(productos)} resultados) pero ninguno es "
            f"{producto['nombre']}"
        )
    return ofertas


def prestashop_busqueda(
    cli: Cliente, tienda: str, producto: dict, url_busqueda: str, moneda: str = "EUR"
) -> list[Oferta]:
    """Buscador de PrestaShop: el listado publica un ItemList en JSON-LD con
    nombre y URL; el precio se lee luego de la ficha de cada candidato."""
    html = cli.get(url_busqueda).text
    candidatos: list[tuple[str, str]] = []
    for nodo in bloques_jsonld(html):
        if str(nodo.get("@type", "")) != "ItemList":
            continue
        for item in nodo.get("itemListElement", []):
            nombre = (item.get("name") or "").replace("&quot;", '"')
            enlace = item.get("url") or ""
            if nombre and enlace and interesa(nombre, producto):
                candidatos.append((nombre, enlace.split("#")[0]))

    ofertas = []
    for nombre, enlace in candidatos[:5]:
        try:
            ficha = cli.get(enlace).text
        except RuntimeError as exc:
            log(f"    ficha inaccesible: {exc}")
            continue
        _, precio, mon, disponible = precio_desde_jsonld(ficha)
        if precio:
            ofertas.append(
                Oferta(tienda, nombre, precio, mon or moneda, enlace, disponible,
                       articulo=producto["id"])
            )
    if not ofertas:
        raise SinResultados(
            f"el buscador respondió pero no hay {producto['nombre']} en el catálogo"
        )
    return ofertas


def woocommerce_tienda(
    cli: Cliente, tienda: str, producto: dict, dominio: str,
    consulta: str | None = None, moneda: str = "GBP"
) -> list[Oferta]:
    """WooCommerce publica un Store API sin autenticación en /wp-json/wc/store.
    Devuelve nombre, precio y existencias en JSON: nada de raspar HTML."""
    ultimo: Exception | None = None
    busqueda = consulta or producto["consulta"]
    for ruta in ("/wp-json/wc/store/v1/products?search=", "/wp-json/wc/store/products?search="):
        try:
            items = cli.json(dominio + ruta + urllib.parse.quote(busqueda))
        except Exception as exc:
            ultimo = exc
            continue
        ofertas = []
        for p in items if isinstance(items, list) else []:
            nombre = limpiar(p.get("name", ""))
            if not interesa(nombre, producto):
                continue
            precios = p.get("prices") or {}
            crudo = precios.get("price")
            if crudo in (None, ""):
                continue
            escala = 10 ** int(precios.get("currency_minor_unit", 2) or 2)
            ofertas.append(
                Oferta(
                    tienda=tienda,
                    producto=nombre,
                    precio=int(crudo) / escala,
                    moneda=precios.get("currency_code") or moneda,
                    enlace=p.get("permalink") or dominio,
                    disponible=bool(p.get("is_in_stock", True)),
                    articulo=producto["id"],
                )
            )
        if not ofertas:
            raise SinResultados(
                f"el catálogo respondió pero no hay {producto['nombre']}"
            )
        return ofertas
    raise RuntimeError(f"Store API no disponible: {ultimo}")


def jsonld_generico(
    cli: Cliente, tienda: str, producto: dict, url: str, render: bool = False
) -> list[Oferta]:
    """Para cualquier tienda que publique schema.org/Product (WooCommerce,
    Magento, PrestaShop, la mayoría de temas modernos)."""
    html = cli.get(url, render=render).text
    nombre, precio, moneda, disponible = precio_desde_jsonld(html)
    if not precio:
        raise LookupError("la página se descargó pero no expone precio en JSON-LD")
    return [
        Oferta(tienda, limpiar(nombre) or producto["nombre"], precio, moneda, url,
               disponible, articulo=producto["id"])
    ]


def amazon_ficha(
    cli: Cliente, tienda: str, producto: dict, asin: str, dominio: str = "https://www.amazon.es"
) -> list[Oferta]:
    """Ficha de Amazon por ASIN. Su robots.txt permite /dp/<ASIN> (solo prohíbe
    subrutas como /dp/rate-this-item/), pero Amazon corta el paso a menudo
    cuando la petición no viene de una conexión doméstica: por eso esta fuente
    va marcada como tolerante y su fallo no pone el workflow en rojo."""
    html = cli.get(f"{dominio}/dp/{asin}").text
    if re.search(r"captcha|acceso automatizado|automated access|Pide ayuda", html, re.I):
        raise RuntimeError("Amazon ha devuelto su página de verificación")

    titulo = re.search(r'id="productTitle"[^>]*>([^<]+)', html)
    nombre = limpiar(titulo.group(1)) if titulo else producto["nombre"]
    if not interesa(nombre, producto):
        raise SinResultados(f"la ficha no es {producto['nombre']}: {nombre[:60]}")

    crudo = re.search(r'"displayPrice"\s*:\s*"([^"]+)"', html) or re.search(
        r'<span class="a-offscreen">([^<]+)</span>', html
    )
    if not crudo:
        raise LookupError("la ficha se descargó pero no expone precio")
    texto = crudo.group(1).replace("\xa0", " ").replace("€", "").strip()
    texto = texto.replace(".", "").replace(",", ".")
    try:
        precio = float(re.findall(r"\d+\.?\d*", texto)[0])
    except (IndexError, ValueError):
        raise LookupError(f"precio ilegible: {crudo.group(1)!r}")

    agotado = bool(re.search(r"No disponible|Currently unavailable", html, re.I))
    return [
        Oferta(tienda, nombre, precio, "EUR", f"{dominio}/dp/{asin}",
               disponible=not agotado, articulo=producto["id"])
    ]


def ebay_api(cli: Cliente, tienda: str, producto: dict, consulta: str | None = None) -> list[Oferta]:
    """Browse API oficial de eBay. Raspar ebay.es/sch devuelve 403 incluso desde
    una IP doméstica, así que la única vía estable es la API (gratuita)."""
    cid = os.getenv("EBAY_CLIENT_ID", "").strip()
    secreto = os.getenv("EBAY_CLIENT_SECRET", "").strip()
    if not (cid and secreto):
        raise PermissionError(
            "faltan EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (regístrate en developer.ebay.com)"
        )
    mercado = os.getenv("EBAY_MARKETPLACE", "EBAY_ES")
    consulta = consulta or producto["consulta"]

    token = cli.sesion.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        auth=(cid, secreto),
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=TIMEOUT,
    )
    token.raise_for_status()
    acceso = token.json()["access_token"]

    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(
        {"q": consulta, "limit": "50", "sort": "price"}
    )
    r = cli.sesion.get(
        url,
        headers={
            "Authorization": f"Bearer {acceso}",
            "X-EBAY-C-MARKETPLACE-ID": mercado,
            "Accept": "application/json",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    ofertas = []
    for item in r.json().get("itemSummaries", []):
        titulo = item.get("title", "")
        if not interesa(titulo, producto):
            continue
        precio = item.get("price") or {}
        try:
            valor = float(precio.get("value"))
        except (TypeError, ValueError):
            continue
        envio = 0.0
        for opcion in item.get("shippingOptions") or []:
            coste = (opcion.get("shippingCost") or {}).get("value")
            if coste:
                envio = float(coste)
                break
        pais = ((item.get("itemLocation") or {}).get("country") or "").upper()
        if pais == "ES":
            entrega = ENTREGA_ES
        elif pais in PAISES_UE:
            entrega = ENTREGA_UE
        elif pais:
            entrega = ENTREGA_IMPORTA
        else:
            entrega = ENTREGA_UE  # sin país declarado, no lo damos por nacional
        ofertas.append(
            Oferta(
                tienda=tienda,
                producto=titulo[:90],
                precio=valor,
                moneda=precio.get("currency", "EUR"),
                enlace=item.get("itemWebUrl", ""),
                entrega=entrega,
                envio=envio,
                articulo=producto["id"],
            )
        )
    ofertas.sort(key=lambda o: o.precio + (o.envio or 0))
    return ofertas[:3]


# --------------------------------------------------------------------------- #
# Fuentes
# --------------------------------------------------------------------------- #

# Cada producto trae sus tiendas, su filtro de títulos y su umbral de aviso.
# Las fuentes declaran a dónde entregan, comprobado en su página de envíos:
# eso es lo que decide si un precio te sirve de algo.
PRODUCTOS: list[dict] = [
    {
        "id": "gesture",
        "nombre": "Steelcase Gesture",
        "corto": "Gesture",
        "consulta": "steelcase gesture",
        "obligatorios": ("gesture",),
        "excluidos": (
            "funda", "cover", "repuesto", "recambio", "pieza", "spare", "part",
            "rueda", "castor", "brazo", "armrest", "armcap", "cojin", "cojín",
            "cushion", "cilindro", "cylinder", "manual", "cabecero", "headrest",
            "stool", "taburete", "gas", "compatible", "replacement", "kit",
        ),
        "umbral": float(os.getenv("UMBRAL_GESTURE", "750")),
        "solo_ue": False,
        "fuentes": [
            {
                "nombre": "Steelcase Oficial (ES)",
                "adaptador": shopify_tienda,
                "params": {"dominio": "https://es.steelcase.com", "handle": "gesture",
                           "moneda": "EUR"},
                "entrega": ENTREGA_ES,
            },
            {
                # Envía a España, pero avisan de que los aranceles e impuestos
                # de importación se cobran al finalizar la compra.
                "nombre": "The Office Crowd (reacond. ES)",
                "adaptador": shopify_tienda,
                "params": {
                    "dominio": "https://theofficecrowd.es",
                    "handle": "steelcase-gesture-ergonomic-office-chair-grey-fabric-refurbished",
                    "moneda": "EUR",
                },
                "entrega": ENTREGA_IMPORTA,
            },
            {
                "nombre": "The Office Crowd (reacond. UK)",
                "adaptador": shopify_tienda,
                "params": {"dominio": "https://theofficecrowd.com", "moneda": "GBP"},
                "entrega": ENTREGA_IMPORTA,
            },
            {
                # "FREE SHIPPING WITHIN LONDON M25" y ninguna otra zona.
                "nombre": "Chair Smith (reacond. UK)",
                "adaptador": woocommerce_tienda,
                "params": {"dominio": "https://chairsmith.co.uk", "consulta": "gesture",
                           "moneda": "GBP"},
                "entrega": ENTREGA_NO,
            },
            {
                # "Free Chair Delivery to UK Mainland", sin envíos fuera.
                "nombre": "Barkham Office Furniture (UK)",
                "adaptador": jsonld_generico,
                "params": {"url": "https://barkhamofficefurniture.co.uk/"
                                  "steelcase-gesture-chair-43625-p.asp"},
                "entrega": ENTREGA_NO,
            },
            {
                # "International Shipping is now available at additional fees".
                "nombre": "Office Logix Shop (reacond. EE. UU.)",
                "adaptador": shopify_tienda,
                "params": {"dominio": "https://www.officelogixshop.com", "moneda": "USD"},
                "entrega": ENTREGA_IMPORTA,
            },
            {
                "nombre": "Oficinas Montiel (2ª mano)",
                "adaptador": prestashop_busqueda,
                "params": {"url_busqueda": "https://www.oficinasmontiel.com/"
                                           "busqueda?controller=search&s=gesture"},
                "entrega": ENTREGA_ES,
            },
            {
                "nombre": "eBay (2ª mano)",
                "adaptador": ebay_api,
                "params": {},
                "entrega": ENTREGA_ES,
            },
        ],
        "a_mano": [
            ("Wallapop", "https://es.wallapop.com/app/search?keywords=steelcase%20gesture"),
            ("Milanuncios", "https://www.milanuncios.com/anuncios/?s=steelcase%20gesture"),
            ("Corporate Spec (UK)",
             "https://corporatespec.com/?s=steelcase+gesture&post_type=product"),
            ("PcComponentes", "https://www.pccomponentes.com/buscar/?query=steelcase%20gesture"),
        ],
    },
    {
        # Solo tiendas de la Unión Europea: sin aduanas ni IVA de importación.
        "id": "tygr",
        "nombre": "Beyerdynamic TYGR 300 R",
        "corto": "TYGR 300 R",
        "consulta": "beyerdynamic tygr 300",
        "obligatorios": ("tygr",),
        "excluidos": (
            "almohadilla", "earpad", "pad", "cable", "repuesto", "recambio",
            "spare", "funda", "case", "soporte", "stand", "fox", "team",
            "micrófono", "microphone", "bundle", "adaptador",
            # Solo de primera mano: nada de reacondicionados ni de usados.
            "b-stock", "bstock", "b stock", "refurbished", "reacondicionad",
            "segunda mano", "2ª mano", "usado", "used", "open box", "openbox",
        ),
        "umbral": float(os.getenv("UMBRAL_TYGR", "140")),
        "solo_ue": True,
        "fuentes": [
            {
                "nombre": "Thomann (DE)",
                "adaptador": jsonld_generico,
                "params": {"url": "https://www.thomann.es/beyerdynamic_tygr_300_r.htm"},
                "entrega": ENTREGA_UE,
            },
            {
                # Amazon suele cortar el paso desde servidores: va tolerante,
                # su fallo no pone el workflow en rojo.
                "nombre": "Amazon.es",
                "adaptador": amazon_ficha,
                "params": {"asin": "B07XYG56HS"},
                "entrega": ENTREGA_ES,
                "tolerante": True,
            },
            {
                "nombre": "Beyerdynamic oficial (UE)",
                "adaptador": jsonld_generico,
                "params": {"url": "https://europe.beyerdynamic.com/p/tygr-300-r"},
                "entrega": ENTREGA_UE,
            },
        ],
        # Madrid Hifi y PcComponentes responden con un reto de Cloudflare
        # incluso desde una conexión doméstica: rechazan clientes automáticos,
        # así que se ofrecen como enlace y no se rastrean.
        "a_mano": [
            ("Madrid Hifi", "https://www.madridhifi.com/buscar?controller=search&s=tygr+300+r"),
            ("PcComponentes", "https://www.pccomponentes.com/buscar/?query=tygr%20300%20r"),
            ("Amazon.es (buscador)", "https://www.amazon.es/s?k=beyerdynamic+tygr+300+r"),
        ],
    },
]

MAX_POR_TIENDA = 3


# --------------------------------------------------------------------------- #
# Conversión de divisa
# --------------------------------------------------------------------------- #


def tasas_cambio(cli: Cliente) -> dict[str, float]:
    tasas = {"EUR": 1.0}
    for moneda in ("GBP", "USD"):
        try:
            datos = cli.json(
                f"https://api.frankfurter.dev/v1/latest?base={moneda}&symbols=EUR"
            )
            tasas[moneda] = float(datos["rates"]["EUR"])
        except Exception as exc:
            log(f"  aviso: sin tipo de cambio {moneda}->EUR ({exc})")
    log("tipos de cambio: " + ", ".join(f"{k}={v}" for k, v in tasas.items()))
    return tasas


# --------------------------------------------------------------------------- #
# Persistencia
# --------------------------------------------------------------------------- #


def cargar_previo() -> list[dict]:
    if not ARCHIVO_DATOS.exists():
        return []
    try:
        datos = json.loads(ARCHIVO_DATOS.read_text(encoding="utf-8"))
        return datos if isinstance(datos, list) else []
    except json.JSONDecodeError:
        return []


def fusionar(nuevas: list[dict], previas: list[dict], fallidas: set[str]) -> list[dict]:
    """Conserva el último precio conocido de las tiendas que hoy han fallado.
    Así una caída puntual nunca vacía la web."""
    # El enlace entra en la clave: una tienda puede listar dos sillas con el
    # mismo nombre y distinto precio, y no deben pisarse.
    def clave(d: dict) -> str:
        articulo = d.get("Articulo") or "gesture"  # los datos viejos no lo traían
        return (
            f"{articulo}::{d.get('Tienda')}::{d.get('Producto')}::{d.get('Enlace')}"
        ).lower()

    por_clave = {clave(d): d for d in previas}
    salida = {clave(d): d for d in nuevas}

    limite = ahora() - timedelta(days=DIAS_CADUCIDAD)
    for llave, viejo in por_clave.items():
        if llave in salida:
            continue
        if viejo.get("Tienda") not in fallidas:
            continue  # la tienda respondió y ya no tiene el producto: se retira
        try:
            visto = datetime.fromisoformat(viejo["Última actualización"])
            if visto.tzinfo is None:
                visto = visto.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            visto = limite  # formato antiguo: se deja caducar
        if visto < limite:
            continue
        copia = dict(viejo)
        copia["Estado"] = "obsoleto"
        salida[llave] = copia

    return sorted(
        salida.values(),
        key=lambda d: (
            d.get("Entrega") == "no",
            d.get("Estado") != "ok",
            d.get("TotalEUR") or d.get("PrecioEUR") or d.get("Precio") or 10**9,
        ),
    )


# --------------------------------------------------------------------------- #
# Programa principal
# --------------------------------------------------------------------------- #


def recortar(ofertas: list[Oferta]) -> list[Oferta]:
    """Una tienda puede listar la misma silla en diez tapizados. Nos quedamos
    con las más baratas, dando prioridad a las que están en stock."""
    def orden(o: Oferta) -> tuple:
        return (o.entrega == ENTREGA_NO, not o.disponible, o.precio)

    unicas: dict[tuple, Oferta] = {}
    for o in sorted(ofertas, key=orden):
        unicas.setdefault((o.producto.lower(), round(o.precio, 2)), o)
    return sorted(unicas.values(), key=orden)[:MAX_POR_TIENDA]


def coste(oferta: dict) -> float | None:
    """Lo que cuesta ponerla en casa; si no hay tarifa de envío, el precio a
    secas. Misma regla que usa la app para ordenar."""
    for campo in ("TotalEUR", "PrecioEUR", "Precio"):
        if isinstance(oferta.get(campo), (int, float)):
            return float(oferta[campo])
    return None


def cargar_historico() -> dict:
    """{"gesture": [...], "tygr": [...]}. El formato antiguo era una lista
    pelada, de cuando solo se seguía la silla: se migra sola."""
    try:
        datos = json.loads(ARCHIVO_HISTORICO.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if isinstance(datos, list):
        return {"gesture": datos}
    return datos if isinstance(datos, dict) else {}


def actualizar_historico(articulo: str, publicadas: list[dict]) -> list[dict]:
    """Un registro por día con lo más barato que podías comprar de verdad:
    en stock y con entrega en España. Si el robot pasa varias veces en el
    mismo día se queda con el mínimo de la jornada."""
    comprables = [
        d for d in publicadas
        if d.get("Estado") == "ok" and d.get("Entrega") != ENTREGA_NO
        and coste(d) is not None
    ]
    por_tienda: dict[str, float] = {}
    for d in comprables:
        valor = coste(d)
        tienda = d["Tienda"]
        if valor is not None and valor < por_tienda.get(tienda, float("inf")):
            por_tienda[tienda] = round(valor, 2)

    mejor = min(comprables, key=lambda d: coste(d)) if comprables else None
    hoy = ahora().strftime("%Y-%m-%d")
    registro = {
        "fecha": hoy,
        "mejor": round(coste(mejor), 2) if mejor else None,
        "tienda": mejor["Tienda"] if mejor else None,
        "tiendas": por_tienda,
    }

    todo = cargar_historico()
    dias = todo.get(articulo) or []
    previo = next((d for d in dias if d.get("fecha") == hoy), None)
    if previo is None:
        dias.append(registro)
    else:
        # Varias pasadas en el mismo día: nos quedamos con lo más barato visto.
        anterior = previo.get("mejor")
        if registro["mejor"] is not None and (
            anterior is None or registro["mejor"] < anterior
        ):
            previo["mejor"] = registro["mejor"]
            previo["tienda"] = registro["tienda"]
        fusion = dict(previo.get("tiendas") or {})
        for tienda, valor in por_tienda.items():
            if valor < fusion.get(tienda, float("inf")):
                fusion[tienda] = valor
        previo["tiendas"] = fusion

    dias.sort(key=lambda d: d.get("fecha", ""))
    todo[articulo] = dias[-DIAS_HISTORICO:]
    ARCHIVO_HISTORICO.write_text(
        json.dumps(todo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if registro["mejor"] is not None:
        log(f"   histórico: {hoy} -> {registro['mejor']:.2f} EUR ({registro['tienda']})")
    else:
        log(f"   histórico: {hoy} -> sin nada comprable")
    return todo[articulo]


def euros(valor: float) -> str:
    """1234.5 -> "1.234,50 €", como se escribe en España."""
    return f"{valor:,.2f} €".replace(",", "@").replace(".", ",").replace("@", ".")


def temporada_actual(hoy: datetime | None = None) -> str | None:
    """Black Friday y Navidad cruzan el cambio de año, así que la ventana se
    compara por (mes, día) teniendo en cuenta la vuelta al 1 de enero."""
    hoy = hoy or ahora()
    actual = (hoy.month, hoy.day)
    for nombre, desde, hasta in TEMPORADAS:
        if desde <= hasta:
            dentro = desde <= actual <= hasta
        else:  # la ventana salta de diciembre a enero
            dentro = actual >= desde or actual <= hasta
        if dentro:
            return nombre
    return None


def mediana(valores: list[float]) -> float | None:
    datos = sorted(v for v in valores if isinstance(v, (int, float)))
    if not datos:
        return None
    medio = len(datos) // 2
    if len(datos) % 2:
        return datos[medio]
    return (datos[medio - 1] + datos[medio]) / 2


def precio_habitual(historial: list[dict], excluir_fecha: str) -> float | None:
    """Mediana de los últimos 30 días, sin contar hoy. La mediana y no la
    media: un día raro no debe mover la referencia."""
    recientes = [
        d.get("mejor") for d in historial[-31:]
        if d.get("fecha") != excluir_fecha and isinstance(d.get("mejor"), (int, float))
    ]
    if len(recientes) < DIAS_MINIMOS_PARA_COMPARAR:
        return None
    return mediana(recientes)


def mensaje_aviso(producto: dict, oferta: dict, valor: float,
                  motivos: list[str], temporada: str | None) -> tuple[str, str]:
    """Título y cuerpo del aviso, el mismo texto para los dos canales."""
    cabecera = f"[{temporada}] " if temporada else ""
    titulo = f"{cabecera}{producto['corto']} a {euros(valor)}"
    if oferta.get("EnvioEUR") is None:
        cuentas = (
            f"{euros(oferta['PrecioEUR'])} de precio. "
            "La tienda no da tarifa de envío, así que el transporte NO está contado."
        )
    else:
        partes = [f"{euros(oferta['PrecioEUR'])} de precio"]
        partes.append(
            "envío gratis" if oferta["EnvioEUR"] == 0 else f"{euros(oferta['EnvioEUR'])} de envío"
        )
        if oferta.get("Entrega") == ENTREGA_IMPORTA:
            partes.append("21 % de IVA de importación")
        cuentas = " + ".join(partes)

    cuerpo = (
        f"**{euros(valor)}** puesto en casa en **{oferta['Tienda']}**.\n\n"
        f"Producto: {producto['nombre']}\n\n"
        f"Entrega: {DESCRIPCION_ENTREGA.get(oferta.get('Entrega'), '?')}\n\n"
        f"Motivo del aviso: {'; '.join(motivos)}\n\n"
        f"{oferta.get('Producto', '')}\n\n"
        f"Cuentas: {cuentas}\n\n"
        f"{oferta.get('Enlace', '')}\n\n"
        "_No incluye aranceles ni gastos de despacho del transportista._"
    )
    return titulo, cuerpo


def enviar_telegram(cli: Cliente, titulo: str, cuerpo: str) -> bool:
    ficha = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (ficha and chat):
        return False
    r = cli.sesion.post(
        f"https://api.telegram.org/bot{ficha}/sendMessage",
        json={
            "chat_id": chat,
            "text": f"🔔 *{titulo}*\n\n{cuerpo}",
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        },
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        log(f"   Telegram rechazó el aviso: HTTP {r.status_code} {r.text[:120]}")
        return False
    log("   aviso enviado por Telegram")
    return True


def abrir_incidencia(cli: Cliente, titulo: str, cuerpo: str) -> bool:
    """Abre una incidencia en el propio repositorio. GitHub la manda por correo
    y por su app móvil, así que no hace falta configurar nada más."""
    ficha = os.getenv("GITHUB_TOKEN", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if not (ficha and repo):
        return False
    r = cli.sesion.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {ficha}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"title": f"🔔 {titulo}", "body": cuerpo},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        log(f"   GitHub rechazó la incidencia: HTTP {r.status_code} {r.text[:160]}")
        return False
    log(f"   aviso publicado como incidencia {r.json().get('html_url', '')}")
    return True


def mandar(cli: Cliente, titulo: str, cuerpo: str) -> list[str]:
    """Saca el aviso por los canales configurados y devuelve los que han ido."""
    canales = []
    if "telegram" in AVISO_CANALES and enviar_telegram(cli, titulo, cuerpo):
        canales.append("telegram")
    if "incidencia" in AVISO_CANALES and abrir_incidencia(cli, titulo, cuerpo):
        canales.append("incidencia")
    return canales


def probar_aviso(cli: Cliente, publicadas: list[dict]) -> None:
    """Aviso de prueba a mano, para comprobar que el canal llega. Lleva los
    precios de verdad, así que de paso se ve que el robot va fino."""
    lineas = []
    for prod in PRODUCTOS:
        suyas = [
            d for d in publicadas
            if (d.get("Articulo") or "gesture") == prod["id"] and apto_para_aviso(d)
        ]
        if suyas:
            m = min(suyas, key=lambda d: coste(d))
            lineas.append(
                f"{prod['corto']}: {euros(coste(m))} en {m['Tienda']} "
                f"(te avisaría por debajo de {euros(prod['umbral'])})"
            )
        else:
            lineas.append(f"{prod['corto']}: ahora mismo no hay nada comprable")

    titulo = "Prueba de aviso"
    cuerpo = ("Si estás leyendo esto, los avisos te llegan bien.\n\n"
              + "\n\n".join(lineas)
              + "\n\n_Mensaje de prueba lanzado a mano. No es una oferta._")
    canales = mandar(cli, titulo, cuerpo)
    if canales:
        log(f"aviso de prueba enviado por: {', '.join(canales)}")
    else:
        log("AVISO DE PRUEBA: no hay ningún canal configurado que funcione. "
            "Revisa TELEGRAM_TOKEN y TELEGRAM_CHAT_ID en los secrets del repo.")


def apto_para_aviso(oferta: dict) -> bool:
    """Qué cuenta para el aviso: algo que puedas comprar y que te llegue.

    Se exige tarifa de envío conocida a lo que viene de fuera de la UE. Sin
    ella solo tendríamos el precio de escaparate, y ya hemos visto que el
    transporte desde EE. UU. puede ser de 316 €: avisar de una "ganga" de
    560 € que en realidad cuesta 1.062 € puestos en casa sería mentir."""
    if oferta.get("Estado") != "ok":
        return False
    if oferta.get("Entrega") == ENTREGA_NO:
        return False
    if oferta.get("Entrega") == ENTREGA_IMPORTA and oferta.get("TotalEUR") is None:
        return False
    return coste(oferta) is not None


def cargar_avisos() -> dict:
    try:
        datos = json.loads(ARCHIVO_AVISOS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(datos, dict):
        return {}
    if "ultimo_avisado" in datos:  # formato antiguo, de un solo producto
        return {"gesture": datos}
    return datos


def avisar(cli: Cliente, producto: dict, publicadas: list[dict],
           historial: list[dict]) -> None:
    """Avisa por dos motivos: que baje del umbral, o que caiga bastante por
    debajo de su precio habitual (lo que pilla las rebajas). No repite salvo
    que baje todavía más, y se rearma cuando deja de haber motivo."""
    todo = cargar_avisos()
    memoria = todo.get(producto["id"]) or {}

    candidatas = [d for d in publicadas if apto_para_aviso(d)]
    if not candidatas:
        return

    mejor = min(candidatas, key=lambda d: coste(d))
    valor = coste(mejor)
    temporada = temporada_actual()
    motivos = []

    if valor < producto["umbral"]:
        motivos.append(f"por debajo de tu umbral de {euros(producto['umbral'])}")

    habitual = precio_habitual(historial, ahora().strftime("%Y-%m-%d"))
    caida = CAIDA_TEMPORADA if temporada else CAIDA_NORMAL
    if habitual and valor <= habitual * (1 - caida):
        porcentaje = (1 - valor / habitual) * 100
        motivos.append(
            f"un {porcentaje:.0f} % por debajo de su precio habitual ({euros(habitual)})"
        )

    if not motivos:
        if memoria.get("ultimo_avisado") is not None:
            log(f"   aviso rearmado: lo más barato está en {euros(valor)}")
            todo[producto["id"]] = {"ultimo_avisado": None,
                                    "rearmado": ahora().isoformat(timespec="seconds")}
            ARCHIVO_AVISOS.write_text(
                json.dumps(todo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        return

    ultimo = memoria.get("ultimo_avisado")
    if ultimo is not None and valor >= ultimo:
        log(f"   hay motivo de aviso ({euros(valor)}) pero ya avisé a {euros(ultimo)}")
        return

    titulo, cuerpo = mensaje_aviso(producto, mejor, valor, motivos, temporada)
    if AVISO_SIMULADO:
        log("   AVISO SIMULADO, no se manda a nadie:")
        log(f"     titulo: {titulo}")
        for linea in cuerpo.split("\n"):
            if linea.strip():
                log(f"     | {linea}")
        return

    canales = mandar(cli, titulo, cuerpo)
    if not canales:
        log("   hay motivo de aviso pero no hay ningún canal configurado")
        return

    todo[producto["id"]] = {
        "ultimo_avisado": round(valor, 2),
        "fecha": ahora().isoformat(timespec="seconds"),
        "tienda": mejor["Tienda"],
        "enlace": mejor.get("Enlace"),
        "motivos": motivos,
        "temporada": temporada,
        "canales": canales,
    }
    ARCHIVO_AVISOS.write_text(
        json.dumps(todo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def rastrear(cli: Cliente, producto: dict, cambio: dict[str, float]) -> list[Resultado]:
    """Pasa por todas las tiendas de un producto."""
    resultados: list[Resultado] = []
    for fuente in producto["fuentes"]:
        nombre = fuente["nombre"]
        log(f"-> {nombre}")
        res = Resultado(nombre, articulo=producto["id"],
                        tolerante=bool(fuente.get("tolerante")))
        try:
            crudas = fuente["adaptador"](cli, nombre, producto, **fuente["params"])
            for oferta in crudas:
                # eBay decide anuncio por anuncio; el resto hereda la de su tienda.
                if oferta.entrega == ENTREGA_ES and fuente["entrega"] != ENTREGA_ES:
                    oferta.entrega = fuente["entrega"]
            res.ofertas = recortar(crudas)
            if res.ofertas:
                mejor = min(res.ofertas, key=lambda o: o.precio)
                log(f"   OK: {len(res.ofertas)} oferta(s), desde {mejor.precio:.2f} {mejor.moneda}")
            else:
                res.nota = "sin resultados para este producto"
                log("   sin resultados")
        except SinResultados as exc:
            res.nota = str(exc)
            log(f"   sin resultados: {exc}")
        except PermissionError as exc:
            res.omitida, res.error = True, str(exc)
            log(f"   OMITIDA: {exc}")
        except Exception as exc:
            res.error = f"{exc.__class__.__name__}: {exc}"
            log(f"   FALLO: {res.error}")
        resultados.append(res)
        time.sleep(random.uniform(1.5, 4.0))  # no martillear las tiendas
    return resultados


def main() -> int:
    cli = Cliente()
    if cli.clave_scraperapi:
        log("ScraperAPI configurado: se usará como reintento ante bloqueos")
    temporada = temporada_actual()
    if temporada:
        log(f"temporada de rebajas: {temporada} (avisos más sensibles)")
    cambio = tasas_cambio(cli)

    previas = cargar_previo()
    resultados: list[Resultado] = []
    final: list[dict] = []
    frescas = 0
    conservados: list[str] = []

    for producto in PRODUCTOS:
        log(f"=== {producto['nombre']}")
        res_prod = rastrear(cli, producto, cambio)
        resultados.extend(res_prod)

        nuevas = [o.a_dict(cambio) for r in res_prod for o in r.ofertas]
        if producto["solo_ue"]:
            fuera = [d for d in nuevas if d["Entrega"] not in (ENTREGA_ES, ENTREGA_UE)]
            if fuera:
                log(f"   {len(fuera)} oferta(s) descartadas por venir de fuera de la UE")
            nuevas = [d for d in nuevas if d["Entrega"] in (ENTREGA_ES, ENTREGA_UE)]

        anteriores = [
            d for d in previas if (d.get("Articulo") or "gesture") == producto["id"]
        ]
        fallidas = {r.nombre for r in res_prod if not r.ok}
        cerrado = fusionar(nuevas, anteriores, fallidas)

        # Red de seguridad por producto: nunca borrar lo bueno con una lista vacía.
        if not cerrado and anteriores:
            log(f"   ninguna fuente dio precio: se conservan {len(anteriores)} anteriores")
            cerrado = anteriores
            conservados.append(producto["id"])

        final.extend(cerrado)
        frescas += len(nuevas)
        historial = actualizar_historico(producto["id"], cerrado)
        avisar(cli, producto, cerrado, historial)

    if not final:
        log("ERROR: ninguna fuente dio precio y no había nada anterior")
        estado(resultados, 0, conservados, temporada)
        return 1

    ARCHIVO_DATOS.write_text(
        json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    estado(resultados, len(final), conservados, temporada)
    log(f"datos.json escrito con {len(final)} oferta(s) ({frescas} frescas)")

    if AVISO_PRUEBA:
        probar_aviso(cli, final)

    # Que una tienda no tenga el producto es información, no avería. Un error
    # de red o de formato sí: el workflow debe ponerse en rojo y enterarte.
    averiadas = [r for r in resultados if not r.ok and not r.omitida and not r.tolerante]
    if averiadas:
        log("fuentes con error: " + ", ".join(
            f"{r.articulo}/{r.nombre}" for r in averiadas))
        return 1
    return 0


def estado(resultados: list[Resultado], total: int,
           conservados: list[str], temporada: str | None) -> None:
    ARCHIVO_ESTADO.write_text(
        json.dumps(
            {
                "ejecutado": ahora().isoformat(timespec="seconds"),
                "ofertas_publicadas": total,
                "conservado_sin_cambios": bool(conservados),
                "temporada": temporada,
                "productos": [
                    {
                        "id": prod["id"],
                        "nombre": prod["nombre"],
                        "corto": prod["corto"],
                        "umbral_aviso": prod["umbral"],
                        "solo_ue": prod["solo_ue"],
                        "conservado": prod["id"] in conservados,
                        "a_mano": [{"nombre": n, "url": u} for n, u in prod["a_mano"]],
                        "fuentes": [
                            {
                                "nombre": r.nombre,
                                "ok": r.ok,
                                "omitida": r.omitida,
                                "ofertas": len(r.ofertas),
                                "tolerante": r.tolerante,
                                "detalle": r.error or r.nota,
                            }
                            for r in resultados if r.articulo == prod["id"]
                        ],
                    }
                    for prod in PRODUCTOS
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
