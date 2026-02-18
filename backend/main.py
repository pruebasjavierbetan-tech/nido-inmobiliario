"""
Nido — Agente Inmobiliario IA
Backend FastAPI: scrapers + análisis IA + alertas email + favoritos
"""

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import json, re, time, os, sqlite3, smtplib, threading, random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import requests
from bs4 import BeautifulSoup
import anthropic

app = FastAPI(title="Nido API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir frontend estático
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_PASS         = os.environ.get("SMTP_PASS", "")

import random

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

def get_headers(referer=None):
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
        **({"Referer": referer} if referer else {}),
    }

HEADERS = get_headers()  # backward compat

# ── Base de datos SQLite ───────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect("nido.db")
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS favoritos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portal TEXT, titulo TEXT, barrio TEXT, ciudad TEXT,
            precio INTEGER, precio_fmt TEXT, area REAL,
            habitaciones TEXT, banos TEXT, parqueadero TEXT, estrato TEXT,
            descripcion TEXT, url TEXT, precio_m2 INTEGER,
            score_ia REAL, analisis_ia TEXT, en_top3 TEXT,
            guardado_en TEXT DEFAULT (datetime('now'))
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS alertas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            nombre TEXT,
            criterios TEXT NOT NULL,
            activa INTEGER DEFAULT 1,
            ultima_ejecucion TEXT,
            creada_en TEXT DEFAULT (datetime('now'))
        )
    """)
    db.commit()
    db.close()

init_db()

# ── Modelos Pydantic ───────────────────────────────────────────────────────────

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
    if not texto: return None
    nums = re.sub(r"[^\d]", "", str(texto))
    return int(nums) if nums else None

def limpiar_area(texto):
    if not texto: return None
    m = re.search(r"([\d\.]+)", str(texto))
    return float(m.group(1).replace(".", "")) if m else None

def formato_precio(valor):
    if not valor: return "N/A"
    return f"${valor:,.0f}"

def prop_base(portal, titulo, barrio, ciudad, precio, area,
              habitaciones, banos, parqueadero, estrato, descripcion, url, antiguedad=None):
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

# ── Scrapers ───────────────────────────────────────────────────────────────────

def scrape_metrocuadrado(criterios: CriteriosBusqueda, max_items=10):
    resultados = []
    tipo_map = {"apartamento": "Apartamento", "casa": "Casa", "oficina": "Oficina", "lote": "Lote"}

    # Intento 1: API REST de Metrocuadrado
    params = {
        "realEstateTypeList": tipo_map.get(criterios.tipo, "Apartamento"),
        "realEstateBusinessList": "Venta" if criterios.operacion == "venta" else "Arriendo",
        "city": criterios.ciudad.capitalize(),
        "from": 0, "size": max_items,
    }
    if criterios.precio_min: params["minimumPrice"]   = criterios.precio_min
    if criterios.precio_max: params["maximumPrice"]   = criterios.precio_max
    if criterios.area_min:   params["minimumArea"]    = criterios.area_min
    if criterios.area_max:   params["maximumArea"]    = criterios.area_max
    if criterios.habitaciones_min: params["minimumBedrooms"]  = criterios.habitaciones_min
    if criterios.banos_min:        params["minimumBathrooms"] = criterios.banos_min

    try:
        session = requests.Session()
        # Primero visitar la home para obtener cookies
        session.get("https://www.metrocuadrado.com", headers=get_headers(), timeout=10)
        time.sleep(0.5)

        api_headers = {
            **get_headers("https://www.metrocuadrado.com/"),
            "x-api-key": "P1MfFHfQMOtL16Zpg36NmT6uh",
            "Accept": "application/json",
        }
        resp = session.get("https://www.metrocuadrado.com/rest-search/search",
                           params=params, headers=api_headers, timeout=20)
        print(f"[Metrocuadrado] API status: {resp.status_code}")

        if resp.status_code == 200:
            data  = resp.json()
            items = data.get("results", [])
            print(f"[Metrocuadrado] {len(items)} items encontrados")
            for item in items:
                try:
                    precio = item.get("salePrice") or item.get("rentPrice")
                    area   = item.get("area") or item.get("builtArea")
                    resultados.append(prop_base(
                        "Metrocuadrado",
                        f"{item.get('propertyType','')} en {item.get('city','')}",
                        item.get("neighborhood", item.get("location")),
                        item.get("city", criterios.ciudad), precio, area,
                        item.get("bedrooms"), item.get("bathrooms"),
                        item.get("garages", 0), item.get("stratum"),
                        item.get("comment", ""),
                        "https://www.metrocuadrado.com" + item.get("link", ""),
                        item.get("builtTime"),
                    ))
                except: continue
        else:
            print(f"[Metrocuadrado] API bloqueada ({resp.status_code}), intentando scraping HTML...")
            resultados.extend(_scrape_mc_html(criterios, max_items))
    except Exception as e:
        print(f"[Metrocuadrado] Error API: {e}")
        resultados.extend(_scrape_mc_html(criterios, max_items))

    return resultados


def _scrape_mc_html(criterios: CriteriosBusqueda, max_items=10):
    """Scraping HTML directo de Metrocuadrado como fallback."""
    resultados = []
    tipo_map = {"apartamento": "apartamento", "casa": "casas", "oficina": "oficinas", "lote": "lotes"}
    tipo_url = tipo_map.get(criterios.tipo, "apartamento")
    op_url   = "venta" if criterios.operacion == "venta" else "arriendo"
    url = f"https://www.metrocuadrado.com/{tipo_url}/{op_url}/{criterios.ciudad}/"

    try:
        session = requests.Session()
        session.get("https://www.metrocuadrado.com", headers=get_headers(), timeout=10)
        time.sleep(1)
        resp = session.get(url, headers=get_headers("https://www.metrocuadrado.com/"), timeout=20)
        print(f"[Metrocuadrado HTML] status: {resp.status_code}, size: {len(resp.text)}")
        soup = BeautifulSoup(resp.text, "html.parser")

        # Buscar __NEXT_DATA__
        script = soup.find("script", id="__NEXT_DATA__")
        if script:
            try:
                data  = json.loads(script.string)
                # Explorar el árbol de datos para encontrar listings
                page_props = data.get("props", {}).get("pageProps", {})
                items = (page_props.get("listings") or
                         page_props.get("results") or
                         page_props.get("inmuebles") or
                         page_props.get("data", {}).get("listings") or [])
                print(f"[Metrocuadrado HTML __NEXT_DATA__] {len(items)} items")
                for item in items[:max_items]:
                    precio = item.get("salePrice") or item.get("rentPrice") or limpiar_precio(str(item.get("price","")))
                    area   = item.get("area") or item.get("builtArea")
                    resultados.append(prop_base(
                        "Metrocuadrado",
                        item.get("title") or f"{item.get('propertyType','')} en {criterios.ciudad}",
                        item.get("neighborhood") or item.get("location") or item.get("barrio"),
                        criterios.ciudad, precio, area,
                        item.get("bedrooms") or item.get("habitaciones"),
                        item.get("bathrooms") or item.get("banos"),
                        item.get("garages") or item.get("garajes", 0),
                        item.get("stratum") or item.get("estrato"),
                        item.get("comment") or item.get("description",""),
                        "https://www.metrocuadrado.com" + str(item.get("link") or item.get("url","")),
                    ))
            except Exception as e:
                print(f"[Metrocuadrado HTML] Error parseando __NEXT_DATA__: {e}")

        # Fallback: tarjetas HTML
        if not resultados:
            cards = soup.select("[class*='result'],[class*='card'],[class*='property']")
            print(f"[Metrocuadrado HTML] {len(cards)} tarjetas HTML encontradas")
            for card in cards[:max_items]:
                try:
                    pe = card.select_one("[class*='price'],[class*='precio'],[class*='valor']")
                    te = card.select_one("h2,h3,[class*='title'],[class*='name']")
                    le = card.select_one("a[href*='/apartamento'],a[href*='/casa'],a[href*='/inmueble']")
                    precio = limpiar_precio(pe.text) if pe else None
                    resultados.append(prop_base(
                        "Metrocuadrado",
                        te.text.strip() if te else "Propiedad",
                        "Ver enlace", criterios.ciudad, precio, None,
                        None, None, None, None, card.text.strip()[:200],
                        "https://www.metrocuadrado.com" + le["href"] if le and le.get("href","").startswith("/") else url,
                    ))
                except: continue
    except Exception as e:
        print(f"[Metrocuadrado HTML] Error: {e}")
    return resultados


def scrape_fincaraiz(criterios: CriteriosBusqueda, max_items=10):
    resultados = []
    ciudad_map = {
        "bogota": "bogota-dc", "medellin": "antioquia/medellin",
        "cali": "valle-del-cauca/cali", "barranquilla": "atlantico/barranquilla",
        "cartagena": "bolivar/cartagena",
    }
    ciudad_url = ciudad_map.get(criterios.ciudad, criterios.ciudad)
    url = f"https://www.fincaraiz.com.co/{criterios.tipo}/{criterios.operacion}/{ciudad_url}/"
    params = {}
    if criterios.precio_min: params["precio-desde"]  = criterios.precio_min
    if criterios.precio_max: params["precio-hasta"]  = criterios.precio_max
    if criterios.area_min:   params["area-desde"]    = criterios.area_min
    if criterios.habitaciones_min: params["habitaciones"] = criterios.habitaciones_min

    try:
        session = requests.Session()
        session.get("https://www.fincaraiz.com.co", headers=get_headers(), timeout=10)
        time.sleep(0.8)
        resp = session.get(url, params=params,
                           headers=get_headers("https://www.fincaraiz.com.co/"), timeout=20)
        print(f"[FincaRaiz] status: {resp.status_code}, size: {len(resp.text)}")
        soup = BeautifulSoup(resp.text, "html.parser")

        # Buscar __NEXT_DATA__
        script = soup.find("script", id="__NEXT_DATA__")
        if script:
            try:
                data       = json.loads(script.string)
                page_props = data.get("props", {}).get("pageProps", {})
                items = (page_props.get("listings") or
                         page_props.get("inmuebles") or
                         page_props.get("data", {}).get("listings") or
                         page_props.get("searchResults", {}).get("listings") or [])
                print(f"[FincaRaiz __NEXT_DATA__] {len(items)} items")
                for item in items[:max_items]:
                    precio = limpiar_precio(str(item.get("precio") or item.get("price") or item.get("canonicalPrice") or ""))
                    area   = limpiar_area(str(item.get("area") or item.get("areaConstruida") or ""))
                    resultados.append(prop_base(
                        "Finca Raíz",
                        item.get("titulo") or item.get("title") or item.get("nombre"),
                        item.get("barrio") or item.get("neighborhood") or item.get("sector"),
                        criterios.ciudad, precio, area,
                        item.get("habitaciones") or item.get("bedrooms") or item.get("alcobas"),
                        item.get("banos") or item.get("bathrooms"),
                        item.get("garajes") or item.get("garages") or item.get("parqueaderos"),
                        item.get("estrato") or item.get("stratum"),
                        item.get("descripcion") or item.get("description", ""),
                        "https://www.fincaraiz.com.co" + str(item.get("url") or item.get("link") or ""),
                    ))
            except Exception as e:
                print(f"[FincaRaiz] Error parseando __NEXT_DATA__: {e}")

        # Fallback: tarjetas HTML
        if not resultados:
            cards = soup.select("div[class*='card'], article[class*='listing'], div[class*='listing-item']")
            print(f"[FincaRaiz HTML] {len(cards)} tarjetas encontradas")
            for card in cards[:max_items]:
                try:
                    pe = card.select_one("[class*='price'],[class*='precio'],[class*='valor']")
                    te = card.select_one("h2,h3,[class*='title'],[class*='titulo']")
                    le = card.select_one("a[href]")
                    ae = card.select_one("[class*='area']")
                    precio = limpiar_precio(pe.text) if pe else None
                    area   = limpiar_area(ae.text)   if ae else None
                    resultados.append(prop_base(
                        "Finca Raíz",
                        te.text.strip() if te else "Propiedad",
                        "Ver enlace", criterios.ciudad, precio, area,
                        None, None, None, None, card.text.strip()[:200],
                        "https://www.fincaraiz.com.co" + le["href"] if le and le.get("href","").startswith("/") else url,
                    ))
                except: continue
    except Exception as e:
        print(f"[FincaRaiz] Error: {e}")
    return resultados


def scrape_ciencuadras(criterios: CriteriosBusqueda, max_items=10):
    resultados = []
    ciudad_slug_map = {
        "bogota": "bogota", "medellin": "medellin",
        "cali": "cali", "barranquilla": "barranquilla", "cartagena": "cartagena",
    }
    ciudad_slug = ciudad_slug_map.get(criterios.ciudad, criterios.ciudad)
    url = f"https://www.ciencuadras.com/{criterios.operacion}/{criterios.tipo}/{ciudad_slug}"
    params = {}
    if criterios.precio_min: params["precio_min"]        = criterios.precio_min
    if criterios.precio_max: params["precio_max"]        = criterios.precio_max
    if criterios.area_min:   params["area_min"]          = criterios.area_min
    if criterios.habitaciones_min: params["habitaciones"] = criterios.habitaciones_min

    try:
        session = requests.Session()
        session.get("https://www.ciencuadras.com", headers=get_headers(), timeout=10)
        time.sleep(0.8)
        resp = session.get(url, params=params,
                           headers=get_headers("https://www.ciencuadras.com/"), timeout=20)
        print(f"[Ciencuadras] status: {resp.status_code}, size: {len(resp.text)}")
        soup = BeautifulSoup(resp.text, "html.parser")

        script = soup.find("script", id="__NEXT_DATA__")
        if script:
            try:
                data       = json.loads(script.string)
                page_props = data.get("props", {}).get("pageProps", {})
                items = (page_props.get("inmuebles") or
                         page_props.get("listings") or
                         page_props.get("data", {}).get("inmuebles") or
                         page_props.get("results") or [])
                print(f"[Ciencuadras __NEXT_DATA__] {len(items)} items")
                for item in items[:max_items]:
                    precio = limpiar_precio(str(item.get("precio") or item.get("price") or ""))
                    area   = limpiar_area(str(item.get("area") or item.get("areaConstruida") or ""))
                    resultados.append(prop_base(
                        "Ciencuadras",
                        item.get("titulo") or item.get("nombre") or item.get("title"),
                        item.get("barrio") or item.get("sector") or item.get("neighborhood"),
                        item.get("ciudad", criterios.ciudad), precio, area,
                        item.get("habitaciones") or item.get("alcobas") or item.get("bedrooms"),
                        item.get("banos") or item.get("bathrooms"),
                        item.get("garajes") or item.get("parqueaderos") or item.get("garages"),
                        item.get("estrato") or item.get("stratum"),
                        item.get("descripcion") or item.get("description", ""),
                        "https://www.ciencuadras.com" + str(item.get("url") or item.get("link") or ""),
                        item.get("antiguedad"),
                    ))
            except Exception as e:
                print(f"[Ciencuadras] Error parseando __NEXT_DATA__: {e}")

        if not resultados:
            cards = soup.select(".property-card,[class*='card-inmueble'],[class*='listing-item'],article")
            print(f"[Ciencuadras HTML] {len(cards)} tarjetas encontradas")
            for card in cards[:max_items]:
                try:
                    pe = card.select_one("[class*='precio'],[class*='price'],[class*='valor']")
                    te = card.select_one("h2,h3,[class*='title'],[class*='titulo'],[class*='nombre']")
                    le = card.select_one("a[href]")
                    ae = card.select_one("[class*='area']")
                    precio = limpiar_precio(pe.text) if pe else None
                    area   = limpiar_area(ae.text)   if ae else None
                    resultados.append(prop_base(
                        "Ciencuadras",
                        te.text.strip() if te else "Propiedad",
                        "Ver enlace", criterios.ciudad, precio, area,
                        None, None, None, None, card.text.strip()[:200],
                        "https://www.ciencuadras.com" + le["href"] if le and le.get("href","").startswith("/") else url,
                    ))
                except: continue
    except Exception as e:
        print(f"[Ciencuadras] Error: {e}")
    return resultados
                        item.get("antiguedad"),
                    ))
            except: pass
            break
        if not resultados:
            for card in soup.select(".property-card,[class*='card-inmueble'],article")[:max_items]:
                try:
                    pe = card.select_one("[class*='precio'],[class*='price']")
                    te = card.select_one("h2,h3,[class*='title'],[class*='titulo']")
                    le = card.select_one("a[href]")
                    ae = card.select_one("[class*='area']")
                    precio = limpiar_precio(pe.text) if pe else None
                    area   = limpiar_area(ae.text)   if ae else None
                    resultados.append(prop_base(
                        "Ciencuadras",
                        te.text.strip() if te else "Propiedad",
                        "Ver enlace", criterios.ciudad, precio, area,
                        None, None, None, None, card.text.strip()[:200],
                        "https://www.ciencuadras.com" + le["href"] if le else url,
                    ))
                except: continue
    except Exception as e:
        print(f"[Ciencuadras] Error: {e}")
    return resultados


# ── Filtros post-scraping ──────────────────────────────────────────────────────

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
        if criterios.parqueadero and not p.get("parqueadero"): continue
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

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    props_text = ""
    for i, p in enumerate(propiedades):
        props_text += (
            f"\n#{i+1} [{p['portal']}] {p['titulo']} | {p['barrio']} | "
            f"{p['precio_fmt']} | {p.get('area','N/A')}m² | "
            f"Hab:{p.get('habitaciones','?')} Baños:{p.get('banos','?')} "
            f"Est:{p.get('estrato','?')} | {p.get('descripcion','')[:150]}\n"
        )

    prompt = f"""Eres experto inmobiliario en Colombia. El comprador busca:
Ciudad: {criterios.ciudad} | {criterios.tipo} en {criterios.operacion}
Precio: ${criterios.precio_min:,} - ${criterios.precio_max:,} | Área: {criterios.area_min}-{criterios.area_max}m²
Hab mín: {criterios.habitaciones_min} | Estrato: {criterios.estrato_min}-{criterios.estrato_max}

Propiedades:
{props_text}

Responde SOLO con JSON válido:
{{
  "analisis": [
    {{"numero":1,"evaluacion_precio":"EXCELENTE|JUSTO|ALTO","score":8,"resumen":"...","pros":["..."],"cons":["..."]}},
    ...
  ],
  "top3": [{{"numero":X,"razon":"..."}},{{"numero":Y,"razon":"..."}},{{"numero":Z,"razon":"..."}}],
  "consejo_general":"..."
}}"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        respuesta = msg.content[0].text
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

def enviar_email_alerta(email_dest, nombre, propiedades, criterios_dict):
    if not SMTP_USER or not SMTP_PASS:
        print("[Email] SMTP no configurado")
        return
    try:
        top = [p for p in propiedades if p.get("en_top3") and not p.get("_meta")][:3]
        filas = "".join([
            f"""<tr style="border-bottom:1px solid #eee">
              <td style="padding:10px"><b>{p.get('titulo','')}</b><br>
                <small style="color:#666">{p.get('barrio','')} · {p.get('portal','')}</small></td>
              <td style="padding:10px;color:#c9a84c;font-weight:bold">{p.get('precio_fmt','N/A')}</td>
              <td style="padding:10px">{p.get('area','?')} m²</td>
              <td style="padding:10px"><a href="{p.get('url','#')}" style="color:#c9a84c">Ver →</a></td>
            </tr>""" for p in (top or propiedades[:5])
        ])
        html = f"""
        <div style="font-family:Georgia,serif;max-width:600px;margin:0 auto;background:#0f0e0c;color:#f0ece4;padding:30px;border-radius:12px">
          <h1 style="color:#c9a84c;font-size:24px;margin-bottom:4px">🏠 Nido</h1>
          <p style="color:#8a8476;font-size:12px;margin-top:0">Agente Inmobiliario IA</p>
          <hr style="border-color:#2e2c28">
          <p>Hola <b>{nombre}</b>, encontramos nuevas propiedades que coinciden con tu búsqueda:</p>
          <p style="color:#8a8476;font-size:13px">
            {criterios_dict.get('tipo','').capitalize()} en {criterios_dict.get('ciudad','').capitalize()} ·
            ${criterios_dict.get('precio_min',0):,} – ${criterios_dict.get('precio_max',0):,}
          </p>
          <table style="width:100%;border-collapse:collapse;margin:20px 0">
            <thead>
              <tr style="background:#1a1916;color:#8a8476;font-size:11px;text-transform:uppercase">
                <th style="padding:10px;text-align:left">Propiedad</th>
                <th style="padding:10px;text-align:left">Precio</th>
                <th style="padding:10px;text-align:left">Área</th>
                <th style="padding:10px;text-align:left">Enlace</th>
              </tr>
            </thead>
            <tbody>{filas}</tbody>
          </table>
          <p style="color:#8a8476;font-size:12px;text-align:center">
            Nido · Agente Inmobiliario IA · Colombia
          </p>
        </div>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🏠 Nido: {len(propiedades)} propiedades nuevas en {criterios_dict.get('ciudad','').capitalize()}"
        msg["From"]    = SMTP_USER
        msg["To"]      = email_dest
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, email_dest, msg.as_string())
        print(f"[Email] Enviado a {email_dest}")
    except Exception as e:
        print(f"[Email] Error: {e}")


# ── Rutas API ──────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return FileResponse("frontend/index.html")

@app.get("/api/diagnostico")
def diagnostico():
    """Verifica conectividad con cada portal."""
    resultados = {}
    portales = {
        "metrocuadrado": "https://www.metrocuadrado.com",
        "fincaraiz":     "https://www.fincaraiz.com.co",
        "ciencuadras":   "https://www.ciencuadras.com",
    }
    for nombre, url in portales.items():
        try:
            resp = requests.get(url, headers=get_headers(), timeout=10)
            resultados[nombre] = {
                "status": resp.status_code,
                "ok": resp.status_code == 200,
                "size": len(resp.text),
                "tiene_next_data": "__NEXT_DATA__" in resp.text,
            }
        except Exception as e:
            resultados[nombre] = {"ok": False, "error": str(e)}
    return resultados

@app.post("/api/buscar")
def buscar(criterios: CriteriosBusqueda):
    todos = []
    por_portal = max(8, criterios.max_resultados // len(criterios.portales))

    if "metrocuadrado" in criterios.portales:
        todos.extend(scrape_metrocuadrado(criterios, por_portal))
        time.sleep(1)
    if "fincaraiz" in criterios.portales:
        todos.extend(scrape_fincaraiz(criterios, por_portal))
        time.sleep(1)
    if "ciencuadras" in criterios.portales:
        todos.extend(scrape_ciencuadras(criterios, por_portal))

    filtrados = aplicar_filtros(todos, criterios)

    if not filtrados:
        return {"resultados": [], "total": 0, "consejo_general": "No se encontraron propiedades con esos criterios."}

    filtrados = analizar_con_ia(filtrados, criterios)

    consejo = ""
    props   = []
    for p in filtrados:
        if p.get("_meta"):
            consejo = p.get("consejo_general", "")
        else:
            props.append(p)

    props.sort(key=lambda x: x.get("score_ia") or 0, reverse=True)
    return {"resultados": props, "total": len(props), "consejo_general": consejo}


@app.post("/api/favoritos")
def guardar_favorito(req: FavoritoRequest):
    p  = req.propiedad
    db = get_db()
    try:
        db.execute("""
            INSERT INTO favoritos (portal,titulo,barrio,ciudad,precio,precio_fmt,area,
                habitaciones,banos,parqueadero,estrato,descripcion,url,precio_m2,
                score_ia,analisis_ia,en_top3)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            p.get("portal"), p.get("titulo"), p.get("barrio"), p.get("ciudad"),
            p.get("precio"), p.get("precio_fmt"), p.get("area"),
            str(p.get("habitaciones","")), str(p.get("banos","")),
            str(p.get("parqueadero","")), str(p.get("estrato","")),
            p.get("descripcion"), p.get("url"), p.get("precio_m2"),
            p.get("score_ia"), p.get("analisis_ia"), p.get("en_top3",""),
        ))
        db.commit()
        return {"ok": True, "mensaje": "Guardado en favoritos"}
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
        db.execute(
            "INSERT INTO alertas (email,nombre,criterios) VALUES (?,?,?)",
            (req.email, req.nombre, json.dumps(req.criterios.dict()))
        )
        db.commit()
        # Enviar email de confirmación
        background_tasks.add_task(
            enviar_email_confirmacion, req.email, req.nombre, req.criterios.dict()
        )
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

def enviar_email_confirmacion(email, nombre, criterios_dict):
    if not SMTP_USER or not SMTP_PASS:
        return
    try:
        html = f"""
        <div style="font-family:Georgia,serif;max-width:500px;margin:0 auto;background:#0f0e0c;color:#f0ece4;padding:30px;border-radius:12px">
          <h1 style="color:#c9a84c">🏠 Alerta activada</h1>
          <p>Hola <b>{nombre}</b>, tu alerta inmobiliaria está activa.</p>
          <p style="color:#8a8476">Te notificaremos cuando encontremos propiedades nuevas para:</p>
          <ul style="color:#c9a84c">
            <li>{criterios_dict.get('tipo','').capitalize()} en {criterios_dict.get('ciudad','').capitalize()}</li>
            <li>Precio: ${criterios_dict.get('precio_min',0):,} – ${criterios_dict.get('precio_max',0):,}</li>
          </ul>
          <p style="color:#8a8476;font-size:12px">Nido · Agente Inmobiliario IA</p>
        </div>"""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "🏠 Nido: Alerta inmobiliaria activada"
        msg["From"]    = SMTP_USER
        msg["To"]      = email
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls(); s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, email, msg.as_string())
    except Exception as e:
        print(f"[Email confirmación] {e}")


# ── Tarea periódica de alertas (cada 6 horas) ──────────────────────────────────

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
                    por_portal     = max(8, criterios.max_resultados // len(criterios.portales))
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
            print(f"[Alertas] Error general: {e}")

threading.Thread(target=ejecutar_alertas, daemon=True).start()
