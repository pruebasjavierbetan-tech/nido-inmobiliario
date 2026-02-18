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


def _extraer_de_html(html_text, portal, criterios, base_url, max_items):
    """
    Extrae propiedades del HTML ya renderizado.
    Intenta __NEXT_DATA__, luego JSON en scripts, luego tarjetas HTML.
    """
    resultados = []
    soup = BeautifulSoup(html_text, "html.parser")

    # Intento 1: __NEXT_DATA__
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            data = json.loads(script.string)
            pp   = data.get("props", {}).get("pageProps", {})
            items = (
                pp.get("listings") or pp.get("inmuebles") or pp.get("results") or
                pp.get("data", {}).get("listings") or pp.get("data", {}).get("inmuebles") or []
            )
            print(f"[{portal} __NEXT_DATA__] {len(items)} items")
            for item in items[:max_items]:
                try:
                    resultados.append(_normalizar_item(portal, item, criterios.ciudad, base_url))
                except:
                    continue
        except Exception as e:
            print(f"[{portal} __NEXT_DATA__] Error: {e}")

    # Intento 2: JSON embebido en scripts
    if not resultados:
        for scr in soup.find_all("script"):
            src = scr.string or ""
            if len(src) < 200:
                continue
            if not ("precio" in src or "salePrice" in src or "price" in src):
                continue
            for key in ['"listings"', '"inmuebles"', '"results"']:
                m = re.search(re.escape(key) + r'\s*:\s*(\[.+?\])\s*[,}]', src, re.DOTALL)
                if m:
                    try:
                        items = json.loads(m.group(1))
                        print(f"[{portal} script JSON] {len(items)} items")
                        for item in items[:max_items]:
                            try:
                                resultados.append(_normalizar_item(portal, item, criterios.ciudad, base_url))
                            except:
                                continue
                        if resultados:
                            break
                    except:
                        continue
            if resultados:
                break

    # Intento 3: tarjetas HTML
    if not resultados:
        cards = soup.select(
            "div[class*='card'], article[class*='listing'], "
            "div[class*='property'], div[class*='inmueble'], li[class*='result']"
        )
        print(f"[{portal} HTML cards] {len(cards)} tarjetas")
        for card in cards[:max_items]:
            try:
                pe = card.select_one("[class*='price'],[class*='precio'],[class*='valor']")
                te = card.select_one("h2,h3,[class*='title'],[class*='titulo'],[class*='nombre']")
                le = card.select_one("a[href]")
                ae = card.select_one("[class*='area']")
                he = card.select_one("[class*='habitacion'],[class*='bedroom'],[class*='alcoba']")
                precio = limpiar_precio(pe.text) if pe else None
                area   = limpiar_area(ae.text)   if ae else None
                link   = le["href"] if le else ""
                if link and not link.startswith("http"):
                    link = base_url + link
                resultados.append(prop_base(
                    portal,
                    te.text.strip() if te else "Propiedad",
                    "Ver enlace", criterios.ciudad,
                    precio, area,
                    he.text.strip() if he else None,
                    None, None, None,
                    card.text.strip()[:200],
                    link or base_url,
                ))
            except:
                continue

    return resultados


# ── Scrapers por portal ────────────────────────────────────────────────────────

def scrape_metrocuadrado(criterios: CriteriosBusqueda, max_items=10):
    resultados = []
    tipo_map = {"apartamento": "Apartamento", "casa": "Casa", "oficina": "Oficina", "lote": "Lote"}

    # Intento 1: API REST de Metrocuadrado
    try:
        api_params = {
            "realEstateTypeList":     tipo_map.get(criterios.tipo, "Apartamento"),
            "realEstateBusinessList": "Venta" if criterios.operacion == "venta" else "Arriendo",
            "city": criterios.ciudad.capitalize(),
            "from": 0, "size": max_items,
        }
        if criterios.precio_min:       api_params["minimumPrice"]    = criterios.precio_min
        if criterios.precio_max:       api_params["maximumPrice"]    = criterios.precio_max
        if criterios.area_min:         api_params["minimumArea"]     = criterios.area_min
        if criterios.habitaciones_min: api_params["minimumBedrooms"] = criterios.habitaciones_min

        resp = scraper_get("https://www.metrocuadrado.com/rest-search/search", api_params)
        print(f"[Metrocuadrado API] status={resp.status_code} size={len(resp.text)}")

        if resp.status_code == 200 and resp.text.strip().startswith("{"):
            items = resp.json().get("results", [])
            print(f"[Metrocuadrado API] {len(items)} items")
            for item in items:
                try:
                    resultados.append(_normalizar_item("Metrocuadrado", item, criterios.ciudad, "https://www.metrocuadrado.com"))
                except:
                    continue
    except Exception as e:
        print(f"[Metrocuadrado API] Error: {e}")

    # Intento 2: página HTML de resultados
    if not resultados:
        try:
            tipo_url = {"apartamento": "apartamento", "casa": "casas", "oficina": "oficinas", "lote": "lotes"}.get(criterios.tipo, "apartamento")
            op_url   = "venta" if criterios.operacion == "venta" else "arriendo"
            url      = f"https://www.metrocuadrado.com/{tipo_url}/{op_url}/{criterios.ciudad}/"
            resp = scraper_get(url)
            print(f"[Metrocuadrado HTML] status={resp.status_code} size={len(resp.text)}")
            resultados = _extraer_de_html(resp.text, "Metrocuadrado", criterios, "https://www.metrocuadrado.com", max_items)
        except Exception as e:
            print(f"[Metrocuadrado HTML] Error: {e}")

    print(f"[Metrocuadrado] Total: {len(resultados)}")
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
    if criterios.precio_min:       params["precio-desde"] = criterios.precio_min
    if criterios.precio_max:       params["precio-hasta"] = criterios.precio_max
    if criterios.area_min:         params["area-desde"]   = criterios.area_min
    if criterios.habitaciones_min: params["habitaciones"] = criterios.habitaciones_min
    try:
        resp = scraper_get(url, params)
        print(f"[FincaRaiz] status={resp.status_code} size={len(resp.text)}")
        resultados = _extraer_de_html(resp.text, "Finca Raíz", criterios, "https://www.fincaraiz.com.co", max_items)
    except Exception as e:
        print(f"[FincaRaiz] Error: {e}")
    print(f"[FincaRaiz] Total: {len(resultados)}")
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
    if criterios.precio_min:       params["precio_min"]   = criterios.precio_min
    if criterios.precio_max:       params["precio_max"]   = criterios.precio_max
    if criterios.area_min:         params["area_min"]     = criterios.area_min
    if criterios.habitaciones_min: params["habitaciones"] = criterios.habitaciones_min
    try:
        resp = scraper_get(url, params)
        print(f"[Ciencuadras] status={resp.status_code} size={len(resp.text)}")
        resultados = _extraer_de_html(resp.text, "Ciencuadras", criterios, "https://www.ciencuadras.com", max_items)
    except Exception as e:
        print(f"[Ciencuadras] Error: {e}")
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


@app.post("/api/buscar")
def buscar(criterios: CriteriosBusqueda):
    todos      = []
    por_portal = max(8, criterios.max_resultados // max(len(criterios.portales), 1))

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
        return {"resultados": [], "total": 0,
                "consejo_general": "No se encontraron propiedades. Intenta ampliar el rango de precio o área."}

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
