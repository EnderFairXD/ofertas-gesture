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
DIR_DEBUG = RAIZ / "debug"

TIMEOUT = 25
REINTENTOS = 3
DIAS_CADUCIDAD = int(os.getenv("DIAS_CADUCIDAD", "14"))

# El producto que buscamos. Evita falsos positivos: sin esto, raspar una página
# de resultados devuelve el precio de la primera silla cualquiera que salga.
TERMINOS_OBLIGATORIOS = ("gesture",)
TERMINOS_EXCLUIDOS = (
    "funda", "cover", "repuesto", "recambio", "pieza", "spare", "part",
    "rueda", "castor", "brazo", "armrest", "armcap", "cojin", "cojín",
    "cushion", "cilindro", "cylinder", "manual", "cabecero", "headrest",
)

CABECERAS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
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


@dataclass
class Oferta:
    tienda: str
    producto: str
    precio: float
    moneda: str
    enlace: str
    disponible: bool = True

    def clave(self) -> str:
        return f"{self.tienda}::{self.producto}".lower()

    def a_dict(self, cambio: dict[str, float]) -> dict:
        tasa = cambio.get(self.moneda, 1.0 if self.moneda == "EUR" else None)
        return {
            "Tienda": self.tienda,
            "Producto": self.producto,
            "Precio": round(self.precio, 2),
            "Moneda": self.moneda,
            "PrecioEUR": round(self.precio * tasa, 2) if tasa else None,
            "Enlace": self.enlace,
            "Disponible": self.disponible,
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
                    destino, timeout=TIMEOUT, headers=cabeceras, proxies=proxies
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
        return self.get(url, json_esperado=True, **kw).json()


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


def shopify_producto(cli: Cliente, tienda: str, url_producto: str) -> list[Oferta]:
    """Una ficha concreta de una tienda Shopify. /products/<handle>.js devuelve
    el precio en céntimos y todas las variantes, sin HTML por medio."""
    base = url_producto.split("?")[0].rstrip("/")
    datos = cli.json(f"{base}.js")
    variantes = [v for v in datos.get("variants", []) if v.get("available")]
    if not variantes:
        variantes = datos.get("variants", [])
    if not variantes:
        return []
    barata = min(variantes, key=lambda v: v.get("price") or 10**9)
    moneda = datos.get("price_currency") or ("GBP" if ".co.uk" in base or "/uk" in base else "EUR")
    return [
        Oferta(
            tienda=tienda,
            producto=datos.get("title", "Gesture"),
            precio=barata["price"] / 100.0,  # Shopify da céntimos
            moneda=moneda,
            enlace=url_producto,
            disponible=bool(barata.get("available")),
        )
    ]


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
    for p in productos:
        titulo = p.get("title", "")
        if not interesa(titulo):
            continue
        try:
            precio = float(str(p.get("price", "")).replace(",", ""))
        except ValueError:
            continue
        enlace = p.get("url", "")
        ofertas.append(
            Oferta(
                tienda=tienda,
                producto=titulo,
                precio=precio,
                moneda=moneda,
                enlace=dominio + enlace.split("?")[0] if enlace.startswith("/") else enlace,
                disponible=bool(p.get("available", True)),
            )
        )
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


def jsonld_generico(
    cli: Cliente, tienda: str, url: str, render: bool = False
) -> list[Oferta]:
    """Para cualquier tienda que publique schema.org/Product (WooCommerce,
    Magento, PrestaShop, la mayoría de temas modernos)."""
    html = cli.get(url, render=render).text
    nombre, precio, moneda, disponible = precio_desde_jsonld(html)
    if not precio:
        raise LookupError("la página se descargó pero no expone precio en JSON-LD")
    return [Oferta(tienda, nombre or "Gesture", precio, moneda, url, disponible)]


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
        ofertas.append(
            Oferta(
                tienda=tienda,
                producto=titulo[:90],
                precio=valor + envio,
                moneda=precio.get("currency", "EUR"),
                enlace=item.get("itemWebUrl", ""),
            )
        )
    ofertas.sort(key=lambda o: o.precio)
    return ofertas[:3]


# --------------------------------------------------------------------------- #
# Fuentes
# --------------------------------------------------------------------------- #

FUENTES: list[tuple[str, object]] = [
    (
        "Steelcase Oficial (ES)",
        lambda cli: shopify_producto(
            cli, "Steelcase Oficial (ES)", "https://es.steelcase.com/products/gesture"
        ),
    ),
    (
        "The Office Crowd (reacond. UK)",
        lambda cli: shopify_busqueda(
            cli,
            "The Office Crowd (reacond. UK)",
            "https://theofficecrowd.com",
            "steelcase gesture",
            "GBP",
        ),
    ),
    (
        "Oficinas Montiel (2ª mano)",
        lambda cli: prestashop_busqueda(
            cli,
            "Oficinas Montiel (2ª mano)",
            "https://www.oficinasmontiel.com/busqueda?controller=search&s=gesture",
        ),
    ),
    (
        "eBay (2ª mano)",
        lambda cli: ebay_api(cli, "eBay (2ª mano)", "steelcase gesture"),
    ),
]


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
    por_clave = {f"{d.get('Tienda')}::{d.get('Producto')}".lower(): d for d in previas}
    salida = {f"{d['Tienda']}::{d['Producto']}".lower(): d for d in nuevas}

    limite = ahora() - timedelta(days=DIAS_CADUCIDAD)
    for clave, viejo in por_clave.items():
        if clave in salida:
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
        salida[clave] = copia

    return sorted(
        salida.values(),
        key=lambda d: (d.get("PrecioEUR") or d.get("Precio") or 10**9),
    )


# --------------------------------------------------------------------------- #
# Programa principal
# --------------------------------------------------------------------------- #


def main() -> int:
    cli = Cliente()
    if cli.clave_scraperapi:
        log("ScraperAPI configurado: se usará como reintento ante bloqueos")
    cambio = tasas_cambio(cli)

    resultados: list[Resultado] = []
    for nombre, funcion in FUENTES:
        log(f"-> {nombre}")
        res = Resultado(nombre)
        try:
            res.ofertas = funcion(cli)  # type: ignore[operator]
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
    estado(resultados, len(final))
    log(f"datos.json escrito con {len(final)} oferta(s) ({len(nuevas)} frescas)")

    vivas = [r for r in resultados if r.ok and not r.omitida]
    return 0 if vivas else 1


def estado(resultados: list[Resultado], total: int, vacio: bool = False) -> None:
    ARCHIVO_ESTADO.write_text(
        json.dumps(
            {
                "ejecutado": ahora().isoformat(timespec="seconds"),
                "ofertas_publicadas": total,
                "conservado_sin_cambios": vacio,
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
