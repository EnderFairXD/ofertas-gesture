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
DIR_DEBUG = RAIZ / "debug"

TIMEOUT = 25
REINTENTOS = 3
DIAS_CADUCIDAD = int(os.getenv("DIAS_CADUCIDAD", "14"))
DIAS_HISTORICO = 365

# El producto que buscamos. Evita falsos positivos: sin esto, raspar una página
# de resultados devuelve el precio de la primera silla cualquiera que salga.
TERMINOS_OBLIGATORIOS = ("gesture",)
TERMINOS_EXCLUIDOS = (
    "funda", "cover", "repuesto", "recambio", "pieza", "spare", "part",
    "rueda", "castor", "brazo", "armrest", "armcap", "cojin", "cojín",
    "cushion", "cilindro", "cylinder", "manual", "cabecero", "headrest",
    "stool", "taburete", "gas", "compatible", "replacement", "kit",
)

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


def interesa(titulo: str) -> bool:
    t = titulo.lower()
    if not all(term in t for term in TERMINOS_OBLIGATORIOS):
        return False
    return not any(term in t for term in TERMINOS_EXCLUIDOS)


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
    cli: Cliente, tienda: str, url_producto: str, moneda: str = "EUR"
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
        )
    ]


def shopify_tienda(
    cli: Cliente,
    tienda: str,
    dominio: str,
    moneda: str = "EUR",
    handle: str | None = None,
    consulta: str = "steelcase gesture",
) -> list[Oferta]:
    """Primero la ficha conocida; si el enlace muere (cambian el handle o
    retiran el producto), se cae al buscador de la propia tienda."""
    if handle:
        try:
            return shopify_producto(cli, tienda, f"{dominio}/products/{handle}", moneda)
        except Exception as exc:
            log(f"    ficha {handle} no sirve ({exc.__class__.__name__}), pruebo el buscador")
    return shopify_busqueda(cli, tienda, dominio, consulta, moneda)


def shopify_busqueda(
    cli: Cliente, tienda: str, dominio: str, consulta: str, moneda: str
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
        if not interesa(titulo):
            continue
        enlace = p.get("url", "")
        enlace = dominio + enlace.split("?")[0] if enlace.startswith("/") else enlace
        # Se pasa por la ficha: trae las variantes y permite pedir la tarifa
        # real de envío, cosa que el buscador no da.
        try:
            ofertas.extend(shopify_producto(cli, tienda, enlace, moneda))
        except Exception as exc:
            log(f"    ficha {enlace.rsplit('/', 1)[-1]} ilegible ({exc.__class__.__name__})")
        if len(ofertas) >= MAX_POR_TIENDA:
            break
    if not ofertas and productos:
        raise SinResultados(
            f"la tienda respondió ({len(productos)} resultados) pero ninguno es una Gesture"
        )
    return ofertas


def prestashop_busqueda(
    cli: Cliente, tienda: str, url_busqueda: str, moneda: str = "EUR"
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
            if nombre and enlace and interesa(nombre):
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
                Oferta(tienda, nombre, precio, mon or moneda, enlace, disponible)
            )
    if not ofertas:
        raise SinResultados(
            "el buscador respondió pero no hay ninguna Gesture en el catálogo"
        )
    return ofertas


def woocommerce_tienda(
    cli: Cliente, tienda: str, dominio: str, consulta: str = "gesture", moneda: str = "GBP"
) -> list[Oferta]:
    """WooCommerce publica un Store API sin autenticación en /wp-json/wc/store.
    Devuelve nombre, precio y existencias en JSON: nada de raspar HTML."""
    ultimo: Exception | None = None
    for ruta in ("/wp-json/wc/store/v1/products?search=", "/wp-json/wc/store/products?search="):
        try:
            items = cli.json(dominio + ruta + urllib.parse.quote(consulta))
        except Exception as exc:
            ultimo = exc
            continue
        ofertas = []
        for p in items if isinstance(items, list) else []:
            nombre = limpiar(p.get("name", ""))
            if not interesa(nombre):
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
                )
            )
        if not ofertas:
            raise SinResultados("el catálogo respondió pero no hay ninguna Gesture")
        return ofertas
    raise RuntimeError(f"Store API no disponible: {ultimo}")


def jsonld_generico(
    cli: Cliente, tienda: str, url: str, render: bool = False
) -> list[Oferta]:
    """Para cualquier tienda que publique schema.org/Product (WooCommerce,
    Magento, PrestaShop, la mayoría de temas modernos)."""
    html = cli.get(url, render=render).text
    nombre, precio, moneda, disponible = precio_desde_jsonld(html)
    if not precio:
        raise LookupError("la página se descargó pero no expone precio en JSON-LD")
    return [Oferta(tienda, limpiar(nombre) or "Gesture", precio, moneda, url, disponible)]


def ebay_api(cli: Cliente, tienda: str, consulta: str) -> list[Oferta]:
    """Browse API oficial de eBay. Raspar ebay.es/sch devuelve 403 incluso desde
    una IP doméstica, así que la única vía estable es la API (gratuita)."""
    cid = os.getenv("EBAY_CLIENT_ID", "").strip()
    secreto = os.getenv("EBAY_CLIENT_SECRET", "").strip()
    if not (cid and secreto):
        raise PermissionError(
            "faltan EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (regístrate en developer.ebay.com)"
        )
    mercado = os.getenv("EBAY_MARKETPLACE", "EBAY_ES")

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
        if not interesa(titulo):
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
            )
        )
    ofertas.sort(key=lambda o: o.precio + (o.envio or 0))
    return ofertas[:3]


# --------------------------------------------------------------------------- #
# Fuentes
# --------------------------------------------------------------------------- #

# Cada fuente declara su adaptador, sus parámetros y a dónde entrega. La zona
# NO se deduce del país de la tienda: está comprobada en su propia página de
# envíos, porque es lo que decide si un precio te sirve de algo.
FUENTES: list[dict] = [
    {
        "nombre": "Steelcase Oficial (ES)",
        "adaptador": shopify_tienda,
        "params": {"dominio": "https://es.steelcase.com", "handle": "gesture", "moneda": "EUR"},
        "entrega": ENTREGA_ES,
    },
    {
        # Envía a España, pero avisan de que los aranceles e impuestos de
        # importación se cobran al finalizar la compra.
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
        # Su página de entregas dice "FREE SHIPPING WITHIN LONDON M25" y no
        # ofrece ninguna otra zona.
        "nombre": "Chair Smith (reacond. UK)",
        "adaptador": woocommerce_tienda,
        "params": {"dominio": "https://chairsmith.co.uk", "consulta": "gesture", "moneda": "GBP"},
        "entrega": ENTREGA_NO,
    },
    {
        # "Free Chair Delivery to UK Mainland"; no mencionan envíos fuera del
        # Reino Unido en ninguna parte.
        "nombre": "Barkham Office Furniture (UK)",
        "adaptador": jsonld_generico,
        "params": {"url": "https://barkhamofficefurniture.co.uk/steelcase-gesture-chair-43625-p.asp"},
        "entrega": ENTREGA_NO,
    },
    {
        # "International Shipping is now available at additional fees as well",
        # sin detallar tarifas. Desde Ohio, además, toca IVA de importación.
        "nombre": "Office Logix Shop (reacond. EE. UU.)",
        "adaptador": shopify_tienda,
        "params": {"dominio": "https://www.officelogixshop.com", "moneda": "USD"},
        "entrega": ENTREGA_IMPORTA,
    },
    {
        "nombre": "Oficinas Montiel (2ª mano)",
        "adaptador": prestashop_busqueda,
        "params": {"url_busqueda": "https://www.oficinasmontiel.com/busqueda?controller=search&s=gesture"},
        "entrega": ENTREGA_ES,
    },
    {
        # Cada anuncio trae su país: la zona se decide anuncio por anuncio.
        "nombre": "eBay (2ª mano)",
        "adaptador": ebay_api,
        "params": {"consulta": "steelcase gesture"},
        "entrega": ENTREGA_ES,
    },
]

# Tiendas que rechazan cualquier cliente automático (reto de Cloudflare incluso
# desde una IP doméstica) o que exigen cabeceras firmadas. No se rastrean: la
# app las ofrece como enlaces para mirarlas a mano.
A_MANO = [
    ("Wallapop", "https://es.wallapop.com/app/search?keywords=steelcase%20gesture"),
    ("Milanuncios", "https://www.milanuncios.com/anuncios/?s=steelcase%20gesture"),
    ("Corporate Spec (UK)", "https://corporatespec.com/?s=steelcase+gesture&post_type=product"),
    ("PcComponentes", "https://www.pccomponentes.com/buscar/?query=steelcase%20gesture"),
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
        return f"{d.get('Tienda')}::{d.get('Producto')}::{d.get('Enlace')}".lower()

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


def actualizar_historico(publicadas: list[dict]) -> None:
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

    try:
        dias = json.loads(ARCHIVO_HISTORICO.read_text(encoding="utf-8"))
        dias = dias if isinstance(dias, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        dias = []

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
    dias = dias[-DIAS_HISTORICO:]
    ARCHIVO_HISTORICO.write_text(
        json.dumps(dias, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if registro["mejor"] is not None:
        log(f"histórico: {hoy} -> {registro['mejor']:.2f} EUR ({registro['tienda']})")
    else:
        log(f"histórico: {hoy} -> sin nada comprable")


def main() -> int:
    cli = Cliente()
    if cli.clave_scraperapi:
        log("ScraperAPI configurado: se usará como reintento ante bloqueos")
    cambio = tasas_cambio(cli)

    resultados: list[Resultado] = []
    for fuente in FUENTES:
        nombre = fuente["nombre"]
        log(f"-> {nombre}")
        res = Resultado(nombre)
        try:
            crudas = fuente["adaptador"](cli, nombre, **fuente["params"])
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

    nuevas = [o.a_dict(cambio) for r in resultados for o in r.ofertas]
    fallidas = {r.nombre for r in resultados if not r.ok}
    previas = cargar_previo()
    final = fusionar(nuevas, previas, fallidas)

    # Red de seguridad: jamás publicar una lista vacía sobre datos buenos.
    if not final and previas:
        log("ERROR: ninguna fuente dio precio; se conserva el datos.json anterior")
        estado(resultados, len(previas), vacio=True)
        return 1

    ARCHIVO_DATOS.write_text(
        json.dumps(final, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    actualizar_historico(final)
    estado(resultados, len(final))
    log(f"datos.json escrito con {len(final)} oferta(s) ({len(nuevas)} frescas)")

    # Que una tienda no tenga el producto es información, no avería. Un error
    # de red o de formato sí: el workflow debe ponerse en rojo y enterarte.
    averiadas = [r for r in resultados if not r.ok and not r.omitida]
    if averiadas:
        log("fuentes con error: " + ", ".join(r.nombre for r in averiadas))
        return 1
    return 0


def estado(resultados: list[Resultado], total: int, vacio: bool = False) -> None:
    ARCHIVO_ESTADO.write_text(
        json.dumps(
            {
                "ejecutado": ahora().isoformat(timespec="seconds"),
                "ofertas_publicadas": total,
                "conservado_sin_cambios": vacio,
                "a_mano": [{"nombre": n, "url": u} for n, u in A_MANO],
                "fuentes": [
                    {
                        "nombre": r.nombre,
                        "ok": r.ok,
                        "omitida": r.omitida,
                        "ofertas": len(r.ofertas),
                        "detalle": r.error or r.nota,
                    }
                    for r in resultados
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
