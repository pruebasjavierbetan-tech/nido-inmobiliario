"""
Nido — Agente Inmobiliario IA
Backend FastAPI: scrapers + ScraperAPI + análisis IA + favoritos + alertas
"""

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List
import json, re, time, os, sqlite3, smtplib, threading, random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import anthropic

app = FastAPI(title="Nido API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SCRAPER_API_KEY   = os.environ.get("SCRAPER_API_KEY", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_PASS         = os.environ.get("SMTP_PASS", "")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

def get_headers(referer=None):
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "es-CO,es;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Connection": "keep-alive",
    }
    if referer:
        h["Referer"] = referer
    return h


# ── Base de datos ──────────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect("nido.db")
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS favoritos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portal TEXT, titulo TEXT, barrio TEXT, ciudad TEXT,
        precio INTEGER, precio_fmt TEXT, area REAL,
        habitaciones TEXT, banos TEXT, parqueadero TEXT, estrato TEXT,
        descripcion TEXT, url TEXT, precio_m2 INTEGER,
        score_ia REAL, analisis_ia TEXT, en_top3 TEXT,
        guardado_en TEXT DEFAULT (datetime('now'))
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS alertas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL, nombre TEXT, criterios TEXT NOT NULL,
        activa INTEGER DEFAULT 1, ultima_ejecucion TEXT,
        creada_en TEXT DEFAULT (datetime('now'))
    )""")
    db.commit()
    db.close()

init_db()


# ── Modelos ────────────────────────────────────────────────────────────────────

class CriteriosBusqueda(BaseModel):
    ciudad: str = "bogota"
    tipo: str = "apartamento"
    operacion: str = "venta"
    precio_min: int = 0
    precio_max: int = 0
    area_min: int = 0
    area_max: int = 0
    habitaciones_min: int = 0
    banos_min: int = 0
    estrato_min: int = 0
    estrato_max: int = 0
    parqueadero: bool = False
    portales: List[str] = ["metrocuadrado", "fincaraiz", "ciencuadras"]
    max_resultados: int = 30

class AlertaRequest(BaseModel):
    email: str
    nombre: str
    criterios: CriteriosBusqueda

class FavoritoRequest(BaseModel):
    propiedad: dict


# ── Utilidades ─────────────────────────────────────────────────────────────────

def limpiar_precio(texto):
    if not texto:
        return None
    nums = re.sub(r"[^\d]", "", str(texto))
    return int(nums) if nums else None

def limpiar_area(texto):
    if not texto:
        return None
    m = re.search(r"([\d\.]+)", str(texto))
    return float(m.group(1).replace(".", "")) if m else None

def formato_precio(valor):
    if not valor:
        return "N/A"
    return f"${valor:,.0f}"

def prop_base(portal, titulo, barrio, ciudad, precio, area,
              habitaciones, banos, parqueadero, estrato,
              descripcion, url, antiguedad=None):
    return {
        "portal": portal,
        "titulo": (titulo or "Propiedad")[:100],
        "barrio": barrio or "N/A",
        "ciudad": ciudad,
        "precio": precio,
        "precio_fmt": formato_precio(precio),
        "area": area,
        "habitaciones": habitaciones,
        "banos": banos,
        "parqueadero": parqueadero,
        "estrato": estrato,
        "antiguedad": antiguedad,
        "descripcion": str(descripcion or "")[:300],
        "url": url,
        "precio_m2": round(precio / area) if precio and area and area > 0 else None,
    }


# ── ScraperAPI — bypass Cloudflare ─────────────────────────────────────────────

def scraper_get(url, url_params=None):
    """
    Envuelve cualquier URL con ScraperAPI para evitar bloqueos.
    Si no hay SCRAPER_API_KEY, hace la petición directa (puede fallar).
    """
    target = url
    if url_params:
        qs = "&".join(f"{k}={v}" for k, v in url_params.items())
        target = f"{url}?{qs}"

    if SCRAPER_API_KEY:
        print(f"[ScraperAPI] {target[:80]}...")
        return requests.get(
            "https://api.scraperapi.com",
            params={"api_key": SCRAPER_API_KEY, "url": target, "country_code": "co"},
            timeout=60
        )
    else:
        print(f"[Direct] {target[:80]}...")
        return requests.get(target, headers=get_headers(), timeout=20)


def _normalizar_item(portal, item, ciudad, base_url):
    """Convierte un item de cualquier portal al formato estándar."""
    precio = None
    for k in ["salePrice", "rentPrice", "precio", "price", "canonicalPrice", "valor"]:
        v = item.get(k)
        if v:
            precio = limpiar_precio(str(v))
            if precio:
                break

    area = None
    for k in ["area", "areaConstruida", "builtArea", "areaTotal", "metrosCuadrados"]:
        v = item.get(k)
        if v:
            area = limpiar_area(str(v))
            if area:
                break

    link = str(item.get("link") or item.get("url") or item.get("href") or "")
    if link and not link.startswith("http"):
        link = base_url + link

    return prop_base(
        portal,
        item.get("titulo") or item.get("title") or item.get("nombre") or item.get("propertyType") or "Propiedad",
        item.get("barrio") or item.get("neighborhood") or item.get("sector") or item.get("location") or item.get("localidad"),
        item.get("ciudad") or item.get("city") or ciudad,
        precio, area,
        item.get("habitaciones") or item.get("bedrooms") or item.get("alcobas"),
        item.get("banos") or item.get("bathrooms"),
        item.get("garajes") or item.get("garages") or item.get("parqueaderos"),
        item.get("estrato") or item.get("stratum"),
        item.get("descripcion") or item.get("description") or item.get("comment") or "",
        link,
        item.get("antiguedad") or item.get("builtTime"),
    )


def scrape_metrocuadrado(criterios: CriteriosBusqueda, max_items=10):
    resultados = []
    tipo_map = {"apartamento": "Apartamento", "casa": "Casa", "oficina": "Oficina", "lote": "Lote"}
    params = {
        "realEstateTypeList":     tipo_map.get(criterios.tipo, "Apartamento"),
        "realEstateBusinessList": "Venta" if criterios.operacion == "venta" else "Arriendo",
        "city": criterios.ciudad.capitalize(),
        "from": 0, "size": max_items,
    }
    if criterios.precio_min:       params["minimumPrice"]    = criterios.precio_min
    if criterios.precio_max:       params["maximumPrice"]    = criterios.precio_max
    if criterios.area_min:         params["minimumArea"]     = criterios.area_min
    if criterios.habitaciones_min: params["minimumBedrooms"] = criterios.habitaciones_min
    try:
        resp = scraper_get("https://www.metrocuadrado.com/rest-search/search", params)
        print(f"[Metrocuadrado] status={resp.status_code} size={len(resp.text)}")
        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            items = resp.json().get("results", [])
            print(f"[Metrocuadrado] {len(items)} items")
            for item in items:
                try:
                    precio = item.get("salePrice") or item.get("rentPrice")
                    area   = item.get("area") or item.get("builtArea")
                    link   = item.get("link", "")
                    if link and not link.startswith("http"):
                        link = "https://www.metrocuadrado.com" + link
                    resultados.append(prop_base(
                        "Metrocuadrado",
                        f"{item.get('propertyType','')} en {item.get('neighborhood') or item.get('city','')}",
                        item.get("neighborhood") or item.get("location"),
                        item.get("city", criterios.ciudad),
                        precio, area,
                        item.get("bedrooms"), item.get("bathrooms"),
                        item.get("garages"), item.get("stratum"),
                        item.get("comment", ""),
                        link,
                    ))
                except: continue
    except Exception as e:
        print(f"[Metrocuadrado] Error: {e}")
    print(f"[Metrocuadrado] Total: {len(resultados)}")
    return resultados


def scrape_fincaraiz(criterios: CriteriosBusqueda, max_items=10):
    """
    Finca Raiz usa Apollo/GraphQL. Los datos estan en pageProps.apolloState.
    Clave del cache: ROOT_QUERY > search(...) > listings > [items]
    """
    resultados = []
    ciudad_map = {
        "bogota": "bogota-dc", "medellin": "antioquia/medellin",
        "cali": "valle-del-cauca/cali", "barranquilla": "atlantico/barranquilla",
        "cartagena": "bolivar/cartagena",
    }
    ciudad_url = ciudad_map.get(criterios.ciudad, criterios.ciudad)
    url = f"https://www.fincaraiz.com.co/{criterios.tipo}/{criterios.operacion}/{ciudad_url}/"
    params = {}
    if criterios.precio_min:       params["precio-desde"] = criterios.precio_min
    if criterios.precio_max:       params["precio-hasta"] = criterios.precio_max
    if criterios.area_min:         params["area-desde"]   = criterios.area_min
    if criterios.habitaciones_min: params["habitaciones"] = criterios.habitaciones_min
    try:
        resp = scraper_get(url, params)
        print(f"[FincaRaiz] status={resp.status_code} size={len(resp.text)}")
        soup = BeautifulSoup(resp.text, "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if not script or not script.string:
            print("[FincaRaiz] Sin __NEXT_DATA__")
            return resultados

        data      = json.loads(script.string)
        pp        = data.get("props", {}).get("pageProps", {})

        # Intentar fetchResult primero (estructura mas comun)
        fetch = pp.get("fetchResult") or {}
        if isinstance(fetch, dict):
            for v in fetch.values():
                if isinstance(v, dict) and "listings" in v:
                    items = v["listings"]
                    print(f"[FincaRaiz fetchResult] {len(items)} items")
                    for item in items[:max_items]:
                        try: resultados.append(_fr_item(item, criterios.ciudad))
                        except: continue
                    break

        # Intentar apolloState (cache GraphQL)
        if not resultados:
            apollo = pp.get("apolloState") or {}
            # Buscar listas de inmuebles en el cache Apollo
            listados = []
            for key, val in apollo.items():
                if isinstance(val, dict):
                    # Buscar referencias de tipo Listing
                    if val.get("__typename") in ("Listing", "Inmueble", "Property"):
                        listados.append(val)
                    # Buscar arrays de referencias
                    for subkey, subval in val.items():
                        if isinstance(subval, list) and len(subval) > 2:
                            for ref in subval:
                                if isinstance(ref, dict) and "__ref" in ref:
                                    ref_data = apollo.get(ref["__ref"], {})
                                    if ref_data.get("__typename") in ("Listing", "Inmueble", "Property", "RealEstate"):
                                        listados.append(ref_data)
            print(f"[FincaRaiz apolloState] {len(listados)} listados encontrados")
            for item in listados[:max_items]:
                try: resultados.append(_fr_item(item, criterios.ciudad))
                except: continue

        # Intentar FiltersContextInitialState
        if not resultados:
            filters_ctx = pp.get("FiltersContextInitialState") or {}
            items = filters_ctx.get("listings") or filters_ctx.get("results") or []
            print(f"[FincaRaiz FiltersContext] {len(items)} items")
            for item in items[:max_items]:
                try: resultados.append(_fr_item(item, criterios.ciudad))
                except: continue

    except Exception as e:
        print(f"[FincaRaiz] Error: {e}")
        import traceback; traceback.print_exc()
    print(f"[FincaRaiz] Total: {len(resultados)}")
    return resultados


def _fr_item(item, ciudad):
    """Normaliza un item de Finca Raiz al formato estandar."""
    precio = None
    for k in ["canonicalPrice", "price", "precio", "salePrice", "rentPrice", "priceFormatted"]:
        v = item.get(k)
        if v:
            precio = limpiar_precio(str(v))
            if precio: break

    area = None
    for k in ["area", "builtArea", "areaConstruida", "totalArea"]:
        v = item.get(k)
        if v:
            area = limpiar_area(str(v))
            if area: break

    link = str(item.get("link") or item.get("url") or item.get("href") or item.get("slug") or "")
    if link and not link.startswith("http"):
        link = "https://www.fincaraiz.com.co" + link

    titulo = (item.get("title") or item.get("titulo") or item.get("name") or
              item.get("propertyType") or item.get("tipoInmueble") or "Propiedad")

    barrio = (item.get("neighborhood") or item.get("barrio") or item.get("location") or
              item.get("sector") or item.get("zone") or "Ver enlace")

    return prop_base(
        "Finca Raiz",
        titulo, barrio, ciudad, precio, area,
        item.get("bedrooms") or item.get("habitaciones") or item.get("rooms"),
        item.get("bathrooms") or item.get("banos"),
        item.get("garages") or item.get("garajes") or item.get("parking"),
        item.get("stratum") or item.get("estrato"),
        item.get("description") or item.get("descripcion") or item.get("comment") or "",
        link,
    )


def scrape_ciencuadras(criterios: CriteriosBusqueda, max_items=10):
    """
    Ciencuadras no usa __NEXT_DATA__ sino un JSON embebido en un <script>
    con formato: {&q;results-/venta/...&q;: {data: {highlights: [...]}}}
    Los &q; son comillas escapadas en HTML.
    """
    resultados = []
    ciudad_slug_map = {
        "bogota": "bogota", "medellin": "medellin",
        "cali": "cali", "barranquilla": "barranquilla", "cartagena": "cartagena",
    }
    ciudad_slug = ciudad_slug_map.get(criterios.ciudad, criterios.ciudad)
    url = f"https://www.ciencuadras.com/{criterios.operacion}/{criterios.tipo}/{ciudad_slug}"
    params = {}
    if criterios.precio_min:       params["precio_min"]   = criterios.precio_min
    if criterios.precio_max:       params["precio_max"]   = criterios.precio_max
    if criterios.area_min:         params["area_min"]     = criterios.area_min
    if criterios.habitaciones_min: params["habitaciones"] = criterios.habitaciones_min
    try:
        resp = scraper_get(url, params)
        print(f"[Ciencuadras] status={resp.status_code} size={len(resp.text)}")

        # Metodo 1: JSON Schema (structured data) en <script type="application/ld+json">
        soup = BeautifulSoup(resp.text, "html.parser")
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data  = json.loads(script.string or "")
                items = data.get("itemListElement", [])
                if items:
                    print(f"[Ciencuadras ld+json] {len(items)} items")
                    for el in items[:max_items]:
                        item = el.get("item", el)
                        try:
                            offers = item.get("offers") or {}
                            precio = limpiar_precio(str(offers.get("price") or ""))
                            link   = item.get("url") or el.get("url") or ""
                            resultados.append(prop_base(
                                "Ciencuadras",
                                item.get("name") or "Propiedad",
                                None, criterios.ciudad,
                                precio, None,
                                None, None, None, None,
                                item.get("description") or "",
                                link,
                            ))
                        except: continue
                    break
            except: continue

        # Metodo 2: JSON embebido con &q; (comillas HTML-escaped)
        if not resultados:
            html = resp.text
            # Decodificar &q; -> "
            html_dec = html.replace("&q;", '"').replace("&amp;q;", '"')
            # Buscar el bloque results con highlights
            m = re.search(r'"results-[^"]+"\s*:\s*\{[^{]*"data"\s*:\s*\{[^{]*"highlights"\s*:\s*(\[.+?\])\s*,', html_dec, re.DOTALL)
            if m:
                try:
                    items = json.loads(m.group(1))
                    print(f"[Ciencuadras &q; JSON] {len(items)} items")
                    for item in items[:max_items]:
                        try:
                            link = item.get("url") or item.get("link") or ""
                            if link and not link.startswith("http"):
                                link = "https://www.ciencuadras.com" + link
                            resultados.append(prop_base(
                                "Ciencuadras",
                                item.get("realEstateType") or item.get("name") or "Propiedad",
                                item.get("neighborhood") or item.get("sector"),
                                item.get("city", criterios.ciudad),
                                limpiar_precio(str(item.get("price") or item.get("precio") or "")),
                                limpiar_area(str(item.get("area") or item.get("builtArea") or "")),
                                item.get("bedrooms") or item.get("habitaciones"),
                                item.get("bathrooms") or item.get("banos"),
                                item.get("garages") or item.get("parkingLots"),
                                item.get("stratum") or item.get("estrato"),
                                item.get("description") or "",
                                link,
                            ))
                        except: continue
                except Exception as e:
                    print(f"[Ciencuadras &q;] Error parseando: {e}")

        # Metodo 3: buscar URLs directas de inmuebles en el HTML
        if not resultados:
            links_inmueble = set()
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/inmueble/" in href:
                    full = href if href.startswith("http") else "https://www.ciencuadras.com" + href
                    links_inmueble.add(full)
            print(f"[Ciencuadras links] {len(links_inmueble)} URLs de inmuebles encontradas")
            for link in list(links_inmueble)[:max_items]:
                card = soup.find("a", href=lambda h: h and link.endswith(h))
                parent = card.parent if card else None
                precio_el = parent.select_one("[class*='price'],[class*='precio']") if parent else None
                resultados.append(prop_base(
                    "Ciencuadras", "Apartamento en Ciencuadras",
                    None, criterios.ciudad,
                    limpiar_precio(precio_el.text) if precio_el else None,
                    None, None, None, None, None, "", link,
                ))

    except Exception as e:
        print(f"[Ciencuadras] Error: {e}")
        import traceback; traceback.print_exc()
    print(f"[Ciencuadras] Total: {len(resultados)}")
    return resultados


# ── Filtros ────────────────────────────────────────────────────────────────────

def aplicar_filtros(resultados, criterios: CriteriosBusqueda):
    filtrados = []
    for p in resultados:
        precio = p.get("precio")
        area   = p.get("area")
        if precio:
            if criterios.precio_min and precio < criterios.precio_min: continue
            if criterios.precio_max and precio > criterios.precio_max: continue
        if area:
            if criterios.area_min and area < criterios.area_min: continue
            if criterios.area_max and criterios.area_max > 0 and area > criterios.area_max: continue
        if criterios.parqueadero and not p.get("parqueadero"):
            continue
        estrato = p.get("estrato")
        if estrato and str(estrato).isdigit():
            if criterios.estrato_min and int(estrato) < criterios.estrato_min: continue
            if criterios.estrato_max and int(estrato) > criterios.estrato_max: continue
        filtrados.append(p)
    return filtrados


# ── Análisis IA ────────────────────────────────────────────────────────────────

def analizar_con_ia(propiedades, criterios: CriteriosBusqueda):
    if not ANTHROPIC_API_KEY or not propiedades:
        for p in propiedades:
            p.update({"score_ia": None, "evaluacion_precio": "N/A",
                      "analisis_ia": "Sin análisis IA", "pros": "", "cons": "",
                      "en_top3": "", "razon_top3": ""})
        return propiedades

    client     = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    props_text = ""
    for i, p in enumerate(propiedades):
        props_text += (
            f"\n#{i+1} [{p['portal']}] {p['titulo']} | {p['barrio']} | "
            f"{p['precio_fmt']} | {p.get('area','N/A')}m² | "
            f"Hab:{p.get('habitaciones','?')} Est:{p.get('estrato','?')}\n"
        )

    prompt = f"""Eres experto inmobiliario en Colombia. Comprador busca:
{criterios.tipo} en {criterios.ciudad} | {criterios.operacion}
Precio: ${criterios.precio_min:,} - ${criterios.precio_max:,} | Área: {criterios.area_min}-{criterios.area_max}m²

Propiedades:
{props_text}

Responde SOLO con JSON válido, sin texto adicional:
{{
  "analisis": [
    {{"numero":1,"evaluacion_precio":"EXCELENTE","score":8,"resumen":"...","pros":["..."],"cons":["..."]}}
  ],
  "top3": [{{"numero":1,"razon":"..."}},{{"numero":2,"razon":"..."}},{{"numero":3,"razon":"..."}}],
  "consejo_general":"..."
}}"""

    try:
        msg        = client.messages.create(model="claude-sonnet-4-20250514", max_tokens=2000,
                       messages=[{"role": "user", "content": prompt}])
        respuesta  = msg.content[0].text
        json_match = re.search(r"\{[\s\S]+\}", respuesta)
        if json_match:
            resultado    = json.loads(json_match.group())
            analisis_map = {a["numero"]: a for a in resultado.get("analisis", [])}
            top3_nums    = [t["numero"] for t in resultado.get("top3", [])]
            top3_map     = {t["numero"]: t for t in resultado.get("top3", [])}
            for i, prop in enumerate(propiedades):
                num = i + 1
                an  = analisis_map.get(num, {})
                prop["score_ia"]          = an.get("score")
                prop["evaluacion_precio"] = an.get("evaluacion_precio", "N/A")
                prop["analisis_ia"]       = an.get("resumen", "")
                prop["pros"]              = " | ".join(an.get("pros", []))
                prop["cons"]              = " | ".join(an.get("cons", []))
                prop["en_top3"]           = "⭐ TOP 3" if num in top3_nums else ""
                prop["razon_top3"]        = top3_map.get(num, {}).get("razon", "")
            propiedades.append({"_meta": True, "consejo_general": resultado.get("consejo_general", "")})
    except Exception as e:
        print(f"[IA] Error: {e}")
        for p in propiedades:
            p.update({"score_ia": None, "analisis_ia": str(e), "en_top3": "", "razon_top3": ""})
    return propiedades


# ── Email ──────────────────────────────────────────────────────────────────────

def _send_email(to, subject, html_body):
    if not SMTP_USER or not SMTP_PASS:
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = to
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_USER, to, msg.as_string())

def enviar_email_alerta(email_dest, nombre, propiedades, criterios_dict):
    try:
        lista = [p for p in propiedades if p.get("en_top3") and not p.get("_meta")][:3] or propiedades[:5]
        filas = "".join([
            f"<tr><td style='padding:10px'><b>{p.get('titulo','')}</b><br>"
            f"<small>{p.get('barrio','')} · {p.get('portal','')}</small></td>"
            f"<td style='padding:10px;color:#c9a84c'>{p.get('precio_fmt','N/A')}</td>"
            f"<td style='padding:10px'>{p.get('area','?')}m²</td>"
            f"<td style='padding:10px'><a href='{p.get('url','#')}' style='color:#c9a84c'>Ver →</a></td></tr>"
            for p in lista
        ])
        html = f"""<div style="font-family:Georgia,serif;max-width:600px;background:#0f0e0c;color:#f0ece4;padding:30px;border-radius:12px">
          <h1 style="color:#c9a84c">🏠 Nido</h1>
          <p>Hola <b>{nombre}</b>, encontramos {len(propiedades)} propiedades nuevas en {criterios_dict.get('ciudad','').capitalize()}:</p>
          <table style="width:100%;border-collapse:collapse">{filas}</table>
          <p style="color:#555;font-size:12px">Nido · Agente Inmobiliario IA</p></div>"""
        _send_email(email_dest, f"🏠 Nido: Propiedades en {criterios_dict.get('ciudad','').capitalize()}", html)
        print(f"[Email] Enviado a {email_dest}")
    except Exception as e:
        print(f"[Email] Error: {e}")

def enviar_email_confirmacion(email, nombre, criterios_dict):
    try:
        html = f"""<div style="font-family:Georgia,serif;max-width:500px;background:#0f0e0c;color:#f0ece4;padding:30px;border-radius:12px">
          <h1 style="color:#c9a84c">🏠 Alerta activada</h1>
          <p>Hola <b>{nombre}</b>, te notificaremos de {criterios_dict.get('tipo','')} en {criterios_dict.get('ciudad','')}.</p>
          <p style="color:#555;font-size:12px">Nido · Agente Inmobiliario IA</p></div>"""
        _send_email(email, "🏠 Nido: Alerta activada", html)
    except Exception as e:
        print(f"[Email confirmación] {e}")


# ── Tarea periódica alertas ────────────────────────────────────────────────────

def ejecutar_alertas():
    while True:
        time.sleep(6 * 3600)
        try:
            db   = get_db()
            rows = db.execute("SELECT * FROM alertas WHERE activa=1").fetchall()
            db.close()
            for row in rows:
                try:
                    criterios_dict = json.loads(row["criterios"])
                    criterios      = CriteriosBusqueda(**criterios_dict)
                    todos          = []
                    por_portal     = max(8, criterios.max_resultados // max(len(criterios.portales), 1))
                    if "metrocuadrado" in criterios.portales:
                        todos.extend(scrape_metrocuadrado(criterios, por_portal))
                    if "fincaraiz" in criterios.portales:
                        todos.extend(scrape_fincaraiz(criterios, por_portal))
                    if "ciencuadras" in criterios.portales:
                        todos.extend(scrape_ciencuadras(criterios, por_portal))
                    filtrados = aplicar_filtros(todos, criterios)
                    if filtrados:
                        filtrados = analizar_con_ia(filtrados, criterios)
                        props     = [p for p in filtrados if not p.get("_meta")]
                        enviar_email_alerta(row["email"], row["nombre"], props, criterios_dict)
                    db2 = get_db()
                    db2.execute("UPDATE alertas SET ultima_ejecucion=? WHERE id=?",
                                (datetime.now().isoformat(), row["id"]))
                    db2.commit()
                    db2.close()
                except Exception as e:
                    print(f"[Alerta {row['id']}] Error: {e}")
        except Exception as e:
            print(f"[Alertas] Error: {e}")

threading.Thread(target=ejecutar_alertas, daemon=True).start()


# ── Rutas ──────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("frontend/index.html")


@app.get("/api/diagnostico")
def diagnostico():
    portales = {
        "metrocuadrado": "https://www.metrocuadrado.com",
        "fincaraiz":     "https://www.fincaraiz.com.co",
        "ciencuadras":   "https://www.ciencuadras.com",
    }
    resultado = {"scraper_api_configurada": bool(SCRAPER_API_KEY)}
    for nombre, url in portales.items():
        try:
            resp = scraper_get(url)
            resultado[nombre] = {
                "status": resp.status_code,
                "ok": resp.status_code == 200,
                "size_kb": round(len(resp.text) / 1024),
                "tiene_next_data": "__NEXT_DATA__" in resp.text,
            }
        except Exception as e:
            resultado[nombre] = {"ok": False, "error": str(e)}
    return resultado


@app.get("/api/html-muestra/{portal}")
def html_muestra(portal: str):
    """
    Descarga la pagina de busqueda de un portal y extrae el __NEXT_DATA__.
    Util para depurar la estructura de datos real.
    """
    urls = {
        "fincaraiz":     "https://www.fincaraiz.com.co/apartamento/venta/bogota-dc/",
        "ciencuadras":   "https://www.ciencuadras.com/venta/apartamento/bogota",
        "metrocuadrado": "https://www.metrocuadrado.com/apartamento/venta/bogota/",
    }
    url = urls.get(portal)
    if not url:
        return {"error": "portal no valido"}
    try:
        resp = scraper_get(url)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Extraer __NEXT_DATA__
        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            data      = json.loads(script.string)
            pp        = data.get("props", {}).get("pageProps", {})
            # Mostrar estructura detallada para depuración
            claves = list(pp.keys())

            # Explorar fetchResult
            fetch  = pp.get("fetchResult") or {}
            fetch_keys = list(fetch.keys()) if isinstance(fetch, dict) else str(type(fetch))
            fetch_muestra = {}
            if isinstance(fetch, dict):
                for k, v in list(fetch.items())[:3]:
                    if isinstance(v, dict):
                        fetch_muestra[k] = {"keys": list(v.keys())[:10], "muestra": {kk: vv for kk, vv in list(v.items())[:5] if not isinstance(vv, (dict,list))}}
                    elif isinstance(v, list) and v:
                        fetch_muestra[k] = {"tipo": "lista", "len": len(v), "primer_item_keys": list(v[0].keys())[:15] if isinstance(v[0], dict) else str(v[0])[:200]}

            # Explorar apolloState: buscar primer item tipo Listing
            apollo = pp.get("apolloState") or {}
            apollo_types = {}
            primer_listing = None
            for key, val in list(apollo.items())[:200]:
                if isinstance(val, dict):
                    t = val.get("__typename", "")
                    apollo_types[t] = apollo_types.get(t, 0) + 1
                    if not primer_listing and t in ("Listing", "Inmueble", "Property", "RealEstate", "Ad"):
                        primer_listing = {"key": key, "data": val}

            # FiltersContextInitialState
            filters = pp.get("FiltersContextInitialState") or {}
            filters_keys = list(filters.keys())[:15] if isinstance(filters, dict) else []

            return {
                "portal": portal,
                "size_kb": round(len(resp.text) / 1024),
                "pageProps_keys": claves,
                "fetchResult_keys": fetch_keys,
                "fetchResult_muestra": fetch_muestra,
                "apolloState_types": apollo_types,
                "apolloState_primer_listing": primer_listing,
                "filtersContext_keys": filters_keys,
            }
        else:
            # Sin __NEXT_DATA__: mostrar primeras clases CSS encontradas
            clases = list(set(
                cls for tag in soup.find_all(True)
                for cls in (tag.get("class") or [])
                if len(cls) > 3
            ))[:40]
            # Buscar scripts con JSON
            scripts_con_data = []
            for scr in soup.find_all("script"):
                src = scr.string or ""
                if len(src) > 200 and ("precio" in src or "price" in src or "salePrice" in src):
                    scripts_con_data.append(src[:500])
            return {
                "portal": portal,
                "size_kb": round(len(resp.text) / 1024),
                "tiene_next_data": False,
                "clases_css": clases,
                "scripts_con_precio": scripts_con_data[:2],
                "html_inicio": resp.text[:1000],
            }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/buscar")
def buscar(criterios: CriteriosBusqueda):
    import traceback
    try:
        todos      = []
        por_portal = max(8, criterios.max_resultados // max(len(criterios.portales), 1))
        errores    = []

        if "metrocuadrado" in criterios.portales:
            try:
                todos.extend(scrape_metrocuadrado(criterios, por_portal))
            except Exception as e:
                errores.append(f"Metrocuadrado: {e}")
                print(f"[buscar] Metrocuadrado fallo: {e}")

        if "fincaraiz" in criterios.portales:
            try:
                todos.extend(scrape_fincaraiz(criterios, por_portal))
            except Exception as e:
                errores.append(f"Finca Raiz: {e}")
                print(f"[buscar] FincaRaiz fallo: {e}")

        if "ciencuadras" in criterios.portales:
            try:
                todos.extend(scrape_ciencuadras(criterios, por_portal))
            except Exception as e:
                errores.append(f"Ciencuadras: {e}")
                print(f"[buscar] Ciencuadras fallo: {e}")

        print(f"[buscar] Total bruto: {len(todos)} | Errores: {errores}")
        filtrados = aplicar_filtros(todos, criterios)
        print(f"[buscar] Tras filtros: {len(filtrados)}")

        if not filtrados:
            msg = "No se encontraron propiedades."
            if errores:
                msg += f" Errores: {', '.join(errores)}"
            else:
                msg += " Intenta ampliar el rango de precio o area."
            return {"resultados": [], "total": 0, "consejo_general": msg}

        try:
            filtrados = analizar_con_ia(filtrados, criterios)
        except Exception as e:
            print(f"[buscar] IA fallo: {e}")
            for p in filtrados:
                p.update({"score_ia": None, "analisis_ia": "", "en_top3": "", "razon_top3": ""})

        consejo = ""
        props   = []
        for p in filtrados:
            if p.get("_meta"):
                consejo = p.get("consejo_general", "")
            else:
                props.append(p)

        props.sort(key=lambda x: x.get("score_ia") or 0, reverse=True)
        return {"resultados": props, "total": len(props), "consejo_general": consejo}

    except Exception as e:
        print(f"[buscar] Error fatal: {e}")
        traceback.print_exc()
        return {"resultados": [], "total": 0, "consejo_general": f"Error interno: {str(e)}"}


@app.post("/api/favoritos")
def guardar_favorito(req: FavoritoRequest):
    p  = req.propiedad
    db = get_db()
    try:
        db.execute("""INSERT INTO favoritos
            (portal,titulo,barrio,ciudad,precio,precio_fmt,area,habitaciones,banos,
             parqueadero,estrato,descripcion,url,precio_m2,score_ia,analisis_ia,en_top3)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            p.get("portal"), p.get("titulo"), p.get("barrio"), p.get("ciudad"),
            p.get("precio"), p.get("precio_fmt"), p.get("area"),
            str(p.get("habitaciones","")), str(p.get("banos","")),
            str(p.get("parqueadero","")), str(p.get("estrato","")),
            p.get("descripcion"), p.get("url"), p.get("precio_m2"),
            p.get("score_ia"), p.get("analisis_ia"), p.get("en_top3",""),
        ))
        db.commit()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        db.close()

@app.get("/api/favoritos")
def listar_favoritos():
    db   = get_db()
    rows = db.execute("SELECT * FROM favoritos ORDER BY guardado_en DESC").fetchall()
    db.close()
    return {"favoritos": [dict(r) for r in rows]}

@app.delete("/api/favoritos/{fav_id}")
def eliminar_favorito(fav_id: int):
    db = get_db()
    db.execute("DELETE FROM favoritos WHERE id=?", (fav_id,))
    db.commit()
    db.close()
    return {"ok": True}

@app.post("/api/alertas")
def crear_alerta(req: AlertaRequest, background_tasks: BackgroundTasks):
    db = get_db()
    try:
        db.execute("INSERT INTO alertas (email,nombre,criterios) VALUES (?,?,?)",
                   (req.email, req.nombre, json.dumps(req.criterios.dict())))
        db.commit()
        background_tasks.add_task(enviar_email_confirmacion, req.email, req.nombre, req.criterios.dict())
        return {"ok": True, "mensaje": f"Alerta creada para {req.email}"}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        db.close()

@app.get("/api/alertas")
def listar_alertas():
    db   = get_db()
    rows = db.execute("SELECT id,email,nombre,activa,creada_en FROM alertas").fetchall()
    db.close()
    return {"alertas": [dict(r) for r in rows]}

@app.delete("/api/alertas/{alerta_id}")
def eliminar_alerta(alerta_id: int):
    db = get_db()
    db.execute("DELETE FROM alertas WHERE id=?", (alerta_id,))
    db.commit()
    db.close()
    return {"ok": True}
