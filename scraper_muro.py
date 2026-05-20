"""
Scraper Muro Inmobiliaria — Idealista
Extrae schema completo: id, coords, imágenes, equipamiento, características, descripción...

Uso:
  1. chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\chrome_debug
  2. Navega a: https://www.idealista.com/pro/muro-inmobiliaria/
  3. python scraper_muro.py
"""

import json, re, time, random
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL    = "https://www.idealista.com"
PROFILE_URL = "https://www.idealista.com/pro/muro-inmobiliaria/"
SECTIONS    = [
    "https://www.idealista.com/pro/muro-inmobiliaria/venta-viviendas/",
    "https://www.idealista.com/pro/muro-inmobiliaria/alquiler-viviendas/",
]
OUTPUT_FILE = Path("propiedades.json")

def jitter(a=1.5, b=3.5): time.sleep(random.uniform(a, b))

def txt(loc):
    try: return loc.inner_text().strip()
    except: return ""

def attr(loc, *attrs):
    for a in attrs:
        try:
            v = loc.get_attribute(a)
            if v and not v.startswith("data:") and v.strip() not in ("","#"): return v.strip()
        except: pass
    return None

def clean_price(raw): return re.sub(r"\s+", " ", raw).strip() if raw else "Consultar"

def clean_title(raw):
    if not raw: return "Sin título"
    t = re.sub(r"\s*[\d.,]+\s*€\/m[²2]", "", raw, flags=re.I)
    t = re.sub(r"\s*[\d.,]+\s*€(?!\s*/)", "", t)
    return re.sub(r"\s{2,}", " ", t).strip() or "Sin título"

def connect():
    p = sync_playwright().start()
    try:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        print("✅ Conectado a Chrome real (CDP)"); return p, b, "cdp"
    except Exception as e:
        print(f"⚠️  CDP no disponible. Lanzando Chromium propio...")
        b = p.chromium.launch(headless=False, args=["--no-sandbox","--disable-blink-features=AutomationControlled","--start-maximized"])
        return p, b, "own"

def build_ctx(browser, mode):
    if mode == "cdp": return browser.contexts[0]
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="es-ES", timezone_id="Europe/Madrid", viewport={"width":1366,"height":768}
    )
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});window.chrome={runtime:{}};")
    return ctx

def active_page(ctx):
    for pg in ctx.pages:
        if "muro-inmobiliaria" in pg.url: return pg
    for pg in ctx.pages:
        if "idealista.com" in pg.url: return pg
    return ctx.pages[-1]

def accept_cookies(page):
    for sel in ["#didomi-notice-agree-button","button:has-text('Aceptar todo')","button:has-text('Aceptar')"]:
        try:
            b = page.locator(sel).first
            if b.is_visible(timeout=1500): b.click(); jitter(0.8,1.5); return
        except: pass

def is_blocked(page):
    return any(x in page.url.lower() for x in ["captcha","blocked","403"]) or page.locator("#captcha,div.g-recaptcha").count() > 0

def handle_block(page, url):
    print("⚠️  CAPTCHA — resuélvelo y pulsa ENTER"); input()
    page.goto(url, wait_until="domcontentloaded", timeout=25000); jitter(2,3)

def scrape_listing(page):
    for sel in ["article.item","article","div.items-container"]:
        try: page.wait_for_selector(sel, timeout=10000); break
        except PWTimeout: pass

    cards = page.locator("article.item")
    if cards.count() == 0: cards = page.locator("article:has(a[href*='/inmueble/'])")
    if cards.count() == 0: return []
    print(f"   📦 {cards.count()} tarjetas")
    results = []

    for i in range(cards.count()):
        try:
            card = cards.nth(i)
            link_el = card.locator("a.item-link,a[href*='/inmueble/']").first
            href = attr(link_el, "href")
            if not href: continue
            url = BASE_URL + href if href.startswith("/") else href
            m = re.search(r"/inmueble/(\d+)/", url)
            property_id = int(m.group(1)) if m else None

            titulo = clean_title(txt(card.locator("a.item-link").first))
            if not titulo or re.match(r"^\d+/?$", titulo):
                for fb in [".item-title","h2","h3"]:
                    t2 = clean_title(txt(card.locator(fb).first))
                    if t2 and not re.match(r"^\d+/?$", t2): titulo = t2; break

            precio = clean_price(txt(card.locator("span.item-price,.price-row,span[class*='price']").first))

            ubicacion = txt(card.locator("div.item-address p.ellipsis,p.ellipsis").first)
            if not ubicacion:
                for sp in range(card.locator(".item-detail-char span.item-detail").count()):
                    t = txt(card.locator(".item-detail-char span.item-detail").nth(sp))
                    if t and not any(x in t.lower() for x in ["m²","hab","baño","€"]): ubicacion = t; break

            metros = habitaciones = banos = None
            spans = card.locator(".item-detail-char span.item-detail,.item-details span")
            for sp in range(spans.count()):
                t = txt(spans.nth(sp)).lower()
                if "m²" in t or "m2" in t: metros = re.sub(r"\s+"," ",t).strip()
                elif "hab" in t or "dorm" in t:
                    m2 = re.search(r"(\d+)", t); habitaciones = int(m2.group(1)) if m2 else None
                elif "baño" in t or "aseo" in t:
                    m2 = re.search(r"(\d+)", t); banos = int(m2.group(1)) if m2 else None

            img_el = card.locator("picture img,figure img,.item-multimedia img,img").first
            imagen_thumb = None
            if img_el.count() > 0:
                imagen_thumb = attr(img_el, "data-lazy-src","data-src","src")
                if imagen_thumb and ("placeholder" in imagen_thumb or imagen_thumb.endswith(".gif")): imagen_thumb = None

            operacion = "alquiler" if "alquiler" in page.url else "venta"

            results.append({
                "property_id": property_id,
                "url": url,
                "titulo": titulo,
                "precio": precio,
                "address": None,
                "ubicacion": ubicacion,
                "latitude": None,
                "longitude": None,
                "location_name": None,
                "location_hierarchy": [],
                "country": "es",
                "metros": metros,
                "lot_size": None,
                "habitaciones": habitaciones,
                "bathroom_count": banos,
                "operacion": operacion,
                "price_currency_symbol": "€",
                "property_condition": None,
                "property_description": "",
                "property_equipment": [],
                "property_features": [],
                "imagen": imagen_thumb,
                "property_images": [imagen_thumb] if imagen_thumb else [],
                "property_image_tags": [],
            })
            print(f"   ✓ [{i+1}] {titulo[:50]}  {precio}")
        except Exception as e:
            print(f"   ✗ tarjeta {i}: {e}")
    return results

def scrape_detail(page, item):
    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=20000); jitter(1.2, 2.5)

        # Todas las imágenes
        images = []
        for sel in ["div.detail-image-gallery img","ul.slider-list li img","div.multimedia-gallery img","div#main-multimedia img","figure img","img[data-lazy-src*='idealista']","img[data-src*='idealista']"]:
            els = page.locator(sel)
            for j in range(els.count()):
                src = attr(els.nth(j), "data-lazy-src","data-src","src")
                if src and src.startswith("http") and src not in images: images.append(src)
            if images: break
        if not images:
            for img in page.locator("img").all():
                src = attr(img, "data-lazy-src","data-src","src")
                if src and src.startswith("http") and any(x in src for x in ["idealista","img3.","img4."]) and src not in images: images.append(src)
        if images:
            item["imagen"] = images[0]
            item["property_images"] = images

        # Descripción
        for sel in ["div.comment-block p","div.adCommentsLanguage p","section.detail-info-description p","p.description"]:
            d = txt(page.locator(sel).first)
            if d: item["property_description"] = d[:1500]; break

        # Dirección
        addr = txt(page.locator("span.main-info__title-minor,h1.main-info__title").first)
        if addr: item["address"] = addr

        # Coordenadas del JSON-LD o del HTML inline
        try:
            for j in range(page.locator("script[type='application/ld+json']").count()):
                data = json.loads(page.locator("script[type='application/ld+json']").nth(j).inner_text())
                geo = (data.get("geo") or {}) if isinstance(data,dict) else {}
                if not geo: geo = (data.get("location",{}).get("geo",{}) or {}) if isinstance(data,dict) else {}
                if geo.get("latitude"):
                    item["latitude"] = geo["latitude"]; item["longitude"] = geo["longitude"]; break
        except: pass
        if not item["latitude"]:
            html = page.content()
            m = re.search(r'"latitude"[:\s]+([\d.]+)', html)
            m2 = re.search(r'"longitude"[:\s]+(-?[\d.]+)', html)
            if m: item["latitude"] = float(m.group(1))
            if m2: item["longitude"] = float(m2.group(1))

        # Breadcrumb
        hier = [txt(el) for el in page.locator("ol.breadcrumb li,nav[aria-label='Breadcrumb'] li").all() if txt(el) not in ["Inicio","Idealista",">"]]
        item["location_hierarchy"] = [h for h in hier if h]
        item["location_name"] = item["location_hierarchy"][-1] if item["location_hierarchy"] else item["ubicacion"]

        # Features / Equipment
        features, equipment = [], []
        equip_kws = ["aire","calefacc","ascensor","amueblado","cocina","portero","video","piscina","garaje","trastero","alarma","armario","seguridad"]
        for row in page.locator("div.details-property-feature-one li,div.details-property-feature-two li,ul.details-property_features li,section.detail-info-features li").all():
            t = txt(row)
            if not t: continue
            (equipment if any(k in t.lower() for k in equip_kws) else features).append(t)
        if features: item["property_features"] = features
        if equipment: item["property_equipment"] = equipment

        # Terreno
        lot_m = re.search(r"(\d[\d.,]*)\s*m[²2]\s*de\s*(parcela|terreno|solar)", page.content(), re.I)
        if lot_m: item["lot_size"] = int(re.sub(r"[.,]","",lot_m.group(1)))

        # Estado
        cond_map = {"nueva construcción":"new","obra nueva":"new","buen estado":"good","reformado":"renovated","a reformar":"to_renovate"}
        cl = page.content().lower()
        for kw, val in cond_map.items():
            if kw in cl: item["property_condition"] = val; break

        # Tags imágenes
        tags = []
        for img in page.locator("div.detail-image-gallery img,ul.slider-list li img").all():
            t = attr(img,"alt","title") or ""
            if t and t not in tags: tags.append(t)
        item["property_image_tags"] = tags

    except Exception as e:
        print(f"      ⚠️  Error detalle: {e}")
    return item

def next_url(page):
    for sel in ["a[rel='next']","a.icon-arrow-right-after","li.next a","a:has-text('Siguiente')"]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                h = attr(el,"href")
                if h: return BASE_URL + h if h.startswith("/") else h
        except: pass
    return None

def scrape_section(page, url):
    all_items, current, n = [], url, 1
    while current:
        print(f"\n  📄 Página {n}: {current}")
        try: page.goto(current, wait_until="domcontentloaded", timeout=25000); jitter(2,4)
        except PWTimeout: print("  Timeout"); break
        accept_cookies(page)
        if is_blocked(page): handle_block(page, current)
        items = scrape_listing(page)
        if not items: break
        all_items.extend(items)
        nxt = next_url(page)
        if nxt and nxt != current: current = nxt; n += 1; jitter(3,6)
        else: break
    return all_items

def main():
    print("="*60)
    print("  SCRAPER MURO INMOBILIARIA — schema completo")
    print("="*60)
    p, browser, mode = connect()
    ctx = build_ctx(browser, mode)
    if mode == "own":
        page = ctx.new_page()
        page.goto("https://www.idealista.com", wait_until="domcontentloaded", timeout=20000); jitter(2,4)
        accept_cookies(page)
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=20000); jitter(2,3)
    else:
        page = active_page(ctx)
        print(f"Pestaña: {page.url}")
        input("\n👉 Navega al perfil de Muro Inmobiliaria. ENTER...\n")
    accept_cookies(page)

    all_props = []
    for sec in SECTIONS:
        print(f"\n{'─'*50}\n  {sec}")
        items = scrape_section(page, sec)
        print(f"  → {len(items)} anuncios")
        all_props.extend(items)
        jitter(3,6)

    if not all_props:
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=25000); jitter(2,4)
        accept_cookies(page); all_props = scrape_listing(page)

    seen, unique = set(), []
    for item in all_props:
        if item["url"] not in seen: seen.add(item["url"]); unique.append(item)
    all_props = unique
    print(f"\nTotal únicos: {len(all_props)}")

    print("\nEntrando a cada anuncio para datos completos...")
    for i, item in enumerate(all_props):
        print(f"  [{i+1}/{len(all_props)}] {item['titulo'][:55]}")
        scrape_detail(page, item)
        jitter(1.5, 3.0)

    OUTPUT_FILE.write_text(json.dumps(all_props, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ {OUTPUT_FILE.resolve()}  —  {len(all_props)} propiedades guardadas")
    for item in all_props:
        print(f"\n  • {item['titulo']}  |  {item['precio']}")
        print(f"    📍 {item['address']}  ({item['latitude']}, {item['longitude']})")
        """
Scraper Muro Inmobiliaria — Idealista
Extrae schema completo: id, coords, imágenes, equipamiento, características, descripción...

Uso:
  1. chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\chrome_debug
  2. Navega a: https://www.idealista.com/pro/muro-inmobiliaria/
  3. python scraper_muro.py
"""

import json, re, time, random
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL    = "https://www.idealista.com"
PROFILE_URL = "https://www.idealista.com/pro/muro-inmobiliaria/"

SECTIONS = [
    "https://www.idealista.com/pro/muro-inmobiliaria/venta-viviendas/",
    "https://www.idealista.com/pro/muro-inmobiliaria/alquiler-viviendas/",
    "https://www.idealista.com/pro/muro-inmobiliaria/venta-locales/",
    "https://www.idealista.com/pro/muro-inmobiliaria/venta-garajes/",
    "https://www.idealista.com/pro/muro-inmobiliaria/venta-terrenos/",
]

OUTPUT_FILE = Path("propiedades.json")

VALID_IMG_PREFIXES = (
    "https://img1.idealista.com/",
    "https://img2.idealista.com/",
    "https://img3.idealista.com/",
    "https://img4.idealista.com/",
)

def jitter(a=1.5, b=3.5): time.sleep(random.uniform(a, b))

def txt(loc):
    try: return loc.inner_text().strip()
    except: return ""

def attr(loc, *attrs):
    for a in attrs:
        try:
            v = loc.get_attribute(a)
            if v and not v.startswith("data:") and v.strip() not in ("", "#"):
                return v.strip()
        except:
            pass
    return None

def clean_price(raw): return re.sub(r"\s+", " ", raw).strip() if raw else "Consultar"

def clean_title(raw):
    if not raw: return "Sin título"
    t = re.sub(r"\s*[\d.,]+\s*€\/m[²2]", "", raw, flags=re.I)
    t = re.sub(r"\s*[\d.,]+\s*€(?!\s*/)", "", t)
    return re.sub(r"\s{2,}", " ", t).strip() or "Sin título"

def connect():
    p = sync_playwright().start()
    try:
        b = p.chromium.connect_over_cdp("http://localhost:9222")
        print("✅ Conectado a Chrome real (CDP)")
        return p, b, "cdp"
    except:
        print("⚠️  CDP no disponible. Lanzando Chromium propio...")
        b = p.chromium.launch(
            headless=False,
            args=["--no-sandbox","--disable-blink-features=AutomationControlled","--start-maximized"]
        )
        return p, b, "own"

def build_ctx(browser, mode):
    if mode == "cdp":
        return browser.contexts[0]
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport={"width":1366,"height":768}
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
    )
    return ctx

def active_page(ctx):
    for pg in ctx.pages:
        if "muro-inmobiliaria" in pg.url:
            return pg
    for pg in ctx.pages:
        if "idealista.com" in pg.url:
            return pg
    return ctx.pages[-1]

def accept_cookies(page):
    for sel in ["#didomi-notice-agree-button","button:has-text('Aceptar todo')","button:has-text('Aceptar')"]:
        try:
            b = page.locator(sel).first
            if b.is_visible(timeout=1500):
                b.click()
                jitter(0.8,1.5)
                return
        except:
            pass

def is_blocked(page):
    return (
        any(x in page.url.lower() for x in ["captcha","blocked","403"])
        or page.locator("#captcha,div.g-recaptcha").count() > 0
    )

def handle_block(page, url):
    print("⚠️  CAPTCHA — resuélvelo y pulsa ENTER")
    input()
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    jitter(2,3)

def valid_image(url):
    if not url:
        return False
    return url.startswith(VALID_IMG_PREFIXES) and "WEB_DETAIL" in url

def scrape_listing(page):
    for sel in ["article.item","article","div.items-container"]:
        try:
            page.wait_for_selector(sel, timeout=10000)
            break
        except PWTimeout:
            pass

    cards = page.locator("article.item")
    if cards.count() == 0:
        cards = page.locator("article:has(a[href*='/inmueble/'])")
    if cards.count() == 0:
        return []

    print(f"   📦 {cards.count()} tarjetas")
    results = []

    for i in range(cards.count()):
        try:
            card = cards.nth(i)
            link_el = card.locator("a.item-link,a[href*='/inmueble/']").first
            href = attr(link_el, "href")
            if not href:
                continue

            url = BASE_URL + href if href.startswith("/") else href
            m = re.search(r"/inmueble/(\d+)/", url)
            property_id = int(m.group(1)) if m else None

            titulo = clean_title(txt(card.locator("a.item-link").first))
            if not titulo or re.match(r"^\d+/?$", titulo):
                for fb in [".item-title","h2","h3"]:
                    t2 = clean_title(txt(card.locator(fb).first))
                    if t2 and not re.match(r"^\d+/?$", t2):
                        titulo = t2
                        break

            precio = clean_price(txt(card.locator("span.item-price,.price-row,span[class*='price']").first))

            ubicacion = txt(card.locator("div.item-address p.ellipsis,p.ellipsis").first)
            if not ubicacion:
                for sp in range(card.locator(".item-detail-char span.item-detail").count()):
                    t = txt(card.locator(".item-detail-char span.item-detail").nth(sp))
                    if t and not any(x in t.lower() for x in ["m²","hab","baño","€"]):
                        ubicacion = t
                        break

            metros = habitaciones = banos = None
            spans = card.locator(".item-detail-char span.item-detail,.item-details span")
            for sp in range(spans.count()):
                t = txt(spans.nth(sp)).lower()
                if "m²" in t or "m2" in t:
                    metros = re.sub(r"\s+"," ",t).strip()
                elif "hab" in t or "dorm" in t:
                    m2 = re.search(r"(\d+)", t)
                    habitaciones = int(m2.group(1)) if m2 else None
                elif "baño" in t or "aseo" in t:
                    m2 = re.search(r"(\d+)", t)
                    banos = int(m2.group(1)) if m2 else None

            img_el = card.locator("picture img,figure img,.item-multimedia img,img").first
            imagen_thumb = None
            if img_el.count() > 0:
                src = attr(img_el, "data-lazy-src","data-src","src")
                if valid_image(src):
                    imagen_thumb = src

            operacion = "alquiler" if "alquiler" in page.url else "venta"

            results.append({
                "property_id": property_id,
                "url": url,
                "titulo": titulo,
                "precio": precio,
                "address": None,
                "ubicacion": ubicacion,
                "latitude": None,
                "longitude": None,
                "location_name": None,
                "location_hierarchy": [],
                "country": "es",
                "metros": metros,
                "lot_size": None,
                "habitaciones": habitaciones,
                "bathroom_count": banos,
                "operacion": operacion,
                "price_currency_symbol": "€",
                "property_condition": None,
                "property_description": "",
                "property_equipment": [],
                "property_features": [],
                "imagen": imagen_thumb,
                "property_images": [imagen_thumb] if imagen_thumb else [],
                "property_image_tags": [],
            })

            print(f"   ✓ [{i+1}] {titulo[:50]}  {precio}")

        except Exception as e:
            print(f"   ✗ tarjeta {i}: {e}")

    return results

def scrape_detail(page, item):
    try:
        page.goto(item["url"], wait_until="domcontentloaded", timeout=20000)
        jitter(1.2, 2.5)

        # IMÁGENES FILTRADAS
        images = []
        selectors = [
            "div.detail-image-gallery img",
            "ul.slider-list li img",
            "div.multimedia-gallery img",
            "div#main-multimedia img",
            "figure img",
            "img[data-lazy-src*='idealista']",
            "img[data-src*='idealista']"
        ]

        for sel in selectors:
            els = page.locator(sel)
            for j in range(els.count()):
                src = attr(els.nth(j), "data-lazy-src","data-src","src")
                if valid_image(src) and src not in images:
                    images.append(src)

        if not images:
            for img in page.locator("img").all():
                src = attr(img, "data-lazy-src","data-src","src")
                if valid_image(src) and src not in images:
                    images.append(src)

        if images:
            item["imagen"] = images[0]
            item["property_images"] = images

        # Descripción
        for sel in ["div.comment-block p","div.adCommentsLanguage p","section.detail-info-description p","p.description"]:
            d = txt(page.locator(sel).first)
            if d:
                item["property_description"] = d[:1500]
                break

        # Dirección
        addr = txt(page.locator("span.main-info__title-minor,h1.main-info__title").first)
        if addr:
            item["address"] = addr

        # Coordenadas
        try:
            for j in range(page.locator("script[type='application/ld+json']").count()):
                data = json.loads(page.locator("script[type='application/ld+json']").nth(j).inner_text())
                geo = (data.get("geo") or {}) if isinstance(data, dict) else {}
                if not geo:
                    geo = (data.get("location",{}).get("geo",{}) or {}) if isinstance(data, dict) else {}
                if geo.get("latitude"):
                    item["latitude"] = geo["latitude"]
                    item["longitude"] = geo["longitude"]
                    break
        except:
            pass

        if not item["latitude"]:
            html = page.content()
            m = re.search(r'"latitude"[:\s]+([\d.]+)', html)
            m2 = re.search(r'"longitude"[:\s]+(-?[\d.]+)', html)
            if m:
                item["latitude"] = float(m.group(1))
            if m2:
                item["longitude"] = float(m2.group(1))

        # Breadcrumb
        hier = [
            txt(el)
            for el in page.locator("ol.breadcrumb li,nav[aria-label='Breadcrumb'] li").all()
            if txt(el) not in ["Inicio","Idealista",">"]
        ]
        item["location_hierarchy"] = [h for h in hier if h]
        item["location_name"] = item["location_hierarchy"][-1] if item["location_hierarchy"] else item["ubicacion"]

        # Features / Equipment
        features, equipment = [], []
        equip_kws = ["aire","calefacc","ascensor","amueblado","cocina","portero","video","piscina","garaje","trastero","alarma","armario","seguridad"]

        for row in page.locator(
            "div.details-property-feature-one li,"
            "div.details-property-feature-two li,"
            "ul.details-property_features li,"
            "section.detail-info-features li"
        ).all():
            t = txt(row)
            if not t:
                continue
            if any(k in t.lower() for k in equip_kws):
                equipment.append(t)
            else:
                features.append(t)

        if features:
            item["property_features"] = features
        if equipment:
            item["property_equipment"] = equipment

        # Terreno
        lot_m = re.search(r"(\d[\d.,]*)\s*m[²2]\s*de\s*(parcela|terreno|solar)", page.content(), re.I)
        if lot_m:
            item["lot_size"] = int(re.sub(r"[.,]","",lot_m.group(1)))

        # Estado
        cond_map = {
            "nueva construcción":"new",
            "obra nueva":"new",
            "buen estado":"good",
            "reformado":"renovated",
            "a reformar":"to_renovate"
        }
        cl = page.content().lower()
        for kw, val in cond_map.items():
            if kw in cl:
                item["property_condition"] = val
                break

        # Tags imágenes
        tags = []
        for img in page.locator("div.detail-image-gallery img,ul.slider-list li img").all():
            t = attr(img,"alt","title") or ""
            if t and t not in tags:
                tags.append(t)
        item["property_image_tags"] = tags

    except Exception as e:
        print(f"      ⚠️  Error detalle: {e}")

    return item

def next_url(page):
    for sel in ["a[rel='next']","a.icon-arrow-right-after","li.next a","a:has-text('Siguiente')"]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                h = attr(el,"href")
                if h:
                    return BASE_URL + h if h.startswith("/") else h
        except:
            pass
    return None

def scrape_section(page, url):
    all_items, current, n = [], url, 1
    while current:
        print(f"\n  📄 Página {n}: {current}")
        try:
            page.goto(current, wait_until="domcontentloaded", timeout=25000)
            jitter(2,4)
        except PWTimeout:
            print("  Timeout")
            break

        accept_cookies(page)
        if is_blocked(page):
            handle_block(page, current)

        items = scrape_listing(page)
        if not items:
            break

        all_items.extend(items)
        nxt = next_url(page)

        if nxt and nxt != current:
            current = nxt
            n += 1
            jitter(3,6)
        else:
            break

    return all_items

def main():
    print("="*60)
    print("  SCRAPER MURO INMOBILIARIA — schema completo")
    print("="*60)

    p, browser, mode = connect()
    ctx = build_ctx(browser, mode)

    if mode == "own":
        page = ctx.new_page()
        page.goto("https://www.idealista.com", wait_until="domcontentloaded", timeout=20000)
        jitter(2,4)
        accept_cookies(page)
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=20000)
        jitter(2,3)
    else:
        page = active_page(ctx)
        print(f"Pestaña: {page.url}")
        input("\n👉 Navega al perfil de Muro Inmobiliaria. ENTER...\n")

    accept_cookies(page)

    all_props = []
    for sec in SECTIONS:
        print(f"\n{'─'*50}\n  {sec}")
        items = scrape_section(page, sec)
        print(f"  → {len(items)} anuncios")
        all_props.extend(items)
        jitter(3,6)

    if not all_props:
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=25000)
        jitter(2,4)
        accept_cookies(page)
        all_props = scrape_listing(page)

    # Eliminar duplicados
    seen, unique = set(), []
    for item in all_props:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    all_props = unique

    print(f"\nTotal únicos: {len(all_props)}")

    print("\nEntrando a cada anuncio para datos completos...")
    for i, item in enumerate(all_props):
        print(f"  [{i+1}/{len(all_props)}] {item['titulo'][:55]}")
        scrape_detail(page, item)
        jitter(1.5, 3.0)

    OUTPUT_FILE.write_text(
        json.dumps(all_props, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"\n✅ {OUTPUT_FILE.resolve()}  —  {len(all_props)} propiedades guardadas")

    for item in all_props:
        print(f"\n  • {item['titulo']}  |  {item['precio']}")
        print(f"    📍 {item['address']}  ({item['latitude']}, {item['longitude']})")
        print(f"    🖼  {len(item['property_images'])} imágenes  |  ⚙️  {len(item['property_equipment'])} equip  |  ✨ {len(item['property_features'])} features")

    try:
        if mode == "own": browser.close()
        p.stop()
    except: pass

if __name__ == "__main__":
    main()
