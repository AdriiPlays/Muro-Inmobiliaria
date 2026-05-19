"""
Scraper Muro Inmobiliaria — Idealista (v2 corregido)
=====================================================

MODO DE USO:
  1. Abre Chrome con depuración remota:
       Windows:  chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\chrome_debug
       Mac:      /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome_debug
       Linux:    google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome_debug

  2. En ese Chrome, ve a:  https://www.idealista.com/pro/muro-inmobiliaria/
     (resuelve captcha si aparece antes de lanzar el script)

  3. Ejecuta:  python scraper_muro.py

SALIDA:
  propiedades.json  (en el mismo directorio)
"""

import json
import re
import time
import random
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Configuración ──────────────────────────────────────────────────────────────
BASE_URL    = "https://www.idealista.com"
PROFILE_URL = "https://www.idealista.com/pro/muro-inmobiliaria/"
SECTIONS    = [
    "https://www.idealista.com/pro/muro-inmobiliaria/venta-viviendas/",
    "https://www.idealista.com/pro/muro-inmobiliaria/alquiler-viviendas/",
]
OUTPUT_FILE = Path("propiedades.json")


# ── Helpers ────────────────────────────────────────────────────────────────────
def jitter(a=1.5, b=3.5):
    time.sleep(random.uniform(a, b))


def get_attr(locator, *attrs):
    """Devuelve el primer atributo no vacío del locator."""
    for attr in attrs:
        try:
            val = locator.get_attribute(attr)
            if val and val.strip() and not val.startswith("data:image") and val.strip() != "#":
                return val.strip()
        except Exception:
            pass
    return None


def get_text(locator):
    try:
        return locator.inner_text().strip()
    except Exception:
        return ""


# ── Conexión ───────────────────────────────────────────────────────────────────
def connect(headless=False):
    """
    Intenta conectarse a Chrome real via CDP.
    Si no está disponible, lanza Chromium propio.
    """
    p = sync_playwright().start()
    try:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")
        print("✅ Conectado a Chrome real (CDP)")
        return p, browser, "cdp"
    except Exception as e:
        print(f"⚠️  CDP no disponible ({e})")
        print("   Lanzando Chromium propio...")
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        return p, browser, "own"


def build_context(browser, mode):
    if mode == "cdp":
        return browser.contexts[0]
    ctx = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport={"width": 1366, "height": 768},
    )
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        "window.chrome = {runtime: {}};"
        "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
    )
    return ctx


def active_page(context):
    """
    FIX del script anterior: buscaba la URL exacta /pro/muro-inmobiliaria/
    pero la URL real era /pro/muro-inmobiliaria/venta-viviendas/ → nunca coincidía
    y siempre devolvía la primera pestaña (que podía ser una pestaña vacía).
    Ahora busca cualquier pestaña con 'muro-inmobiliaria' o 'idealista' en la URL.
    """
    pages = context.pages
    for pg in pages:
        if "muro-inmobiliaria" in pg.url:
            return pg
    for pg in pages:
        if "idealista.com" in pg.url:
            return pg
    return pages[-1] if pages else context.new_page()


# ── Anti-bloqueo ───────────────────────────────────────────────────────────────
def accept_cookies(page):
    for sel in [
        "#didomi-notice-agree-button",
        "button[id*='accept']",
        "#onetrust-accept-btn-handler",
        "button:has-text('Aceptar todo')",
        "button:has-text('Aceptar')",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                btn.click()
                print("   🍪 Cookies aceptadas")
                jitter(0.8, 1.5)
                return
        except Exception:
            pass


def is_blocked(page):
    url = page.url.lower()
    if any(x in url for x in ["captcha", "blocked", "403", "security"]):
        return True
    try:
        if page.locator("#captcha, div.g-recaptcha, iframe[title*='reCAPTCHA']").count() > 0:
            return True
    except Exception:
        pass
    return False


def handle_block(page, url):
    print("\n⚠️  BLOQUEO / CAPTCHA DETECTADO")
    print("   Resuélvelo en el navegador y pulsa ENTER para continuar...")
    input()
    jitter(2, 4)
    page.goto(url, wait_until="domcontentloaded", timeout=25000)
    jitter(2, 3)


# ── Extracción de tarjetas del listado ─────────────────────────────────────────
def scrape_listing_page(page) -> list[dict]:
    """
    Extrae todos los datos de las tarjetas visibles en la página de listado.

    CORRECCIONES respecto al script anterior:
    ─────────────────────────────────────────
    1. TÍTULO: el error "1/" venía de hacer inner_text().split("\\n")[0] sobre
       el card completo. Ese primer texto era el contador de fotos (span "1/5").
       FIX: leer directamente el texto del enlace a.item-link, que ES el título.

    2. PRECIO: el script anterior no lo extraía en absoluto.
       FIX: span.item-price  |  .price-row  |  span[class*='price']

    3. UBICACIÓN: el script anterior no la extraía.
       FIX: div.item-address p.ellipsis  |  span.item-detail dentro de
            .item-detail-char

    4. IMAGEN: Idealista usa lazy loading. El src suele ser un placeholder.
       FIX: probar data-lazy-src → data-src → src, descartar data:image/...

    5. SELECTORES de tarjeta: "article" es demasiado amplio (coge nav, footer).
       FIX: article.item (clase específica de Idealista)
    """
    results = []

    # Esperar carga de tarjetas
    for wait_sel in ["article.item", "article", "div.items-container"]:
        try:
            page.wait_for_selector(wait_sel, timeout=10000)
            break
        except PWTimeout:
            pass

    # Localizar tarjetas
    cards = page.locator("article.item")
    if cards.count() == 0:
        # fallback: artículos que contengan un enlace a /inmueble/
        cards = page.locator("article:has(a[href*='/inmueble/'])")
    if cards.count() == 0:
        cards = page.locator("div.item:has(a[href*='/inmueble/'])")

    total = cards.count()
    print(f"   📦 {total} tarjetas")
    if total == 0:
        return []

    for i in range(total):
        try:
            card = cards.nth(i)
            data = {}

            # ── URL ────────────────────────────────────────────────────────
            link_el = card.locator("a.item-link, a[href*='/inmueble/']").first
            href = get_attr(link_el, "href")
            if not href:
                continue
            data["url"] = BASE_URL + href if href.startswith("/") else href

            # ── TÍTULO ─────────────────────────────────────────────────────
            # Usar texto del <a class="item-link"> que es exactamente el título.
            # NO usar card.inner_text() que incluye el contador de fotos primero.
            title = get_text(card.locator("a.item-link").first)
            if not title or re.match(r"^\d+[/\s]", title):
                # Si sigue mal, intentar h2/h3 o .item-title
                for fallback in [".item-title", "h2", "h3", ".title"]:
                    title = get_text(card.locator(fallback).first)
                    if title and not re.match(r"^\d+[/\s]", title):
                        break
            data["titulo"] = title or "Sin título"

            # ── PRECIO ─────────────────────────────────────────────────────
            raw = get_text(card.locator("span.item-price, .price-row, span[class*='price']").first)
            data["precio"] = re.sub(r"\s+", " ", raw).strip() if raw else "Consultar"

            # ── UBICACIÓN ──────────────────────────────────────────────────
            loc = get_text(card.locator("div.item-address p.ellipsis").first)
            if not loc:
                loc = get_text(card.locator("p.ellipsis").first)
            if not loc:
                # A veces en span dentro de .item-detail-char
                spans = card.locator(".item-detail-char span.item-detail, span.item-detail")
                for j in range(spans.count()):
                    txt = get_text(spans.nth(j))
                    # La ubicación no contiene m², hab, baños
                    if txt and not any(x in txt.lower() for x in ["m²", "m2", "hab", "baño", "aseo", "€"]):
                        loc = txt
                        break
            data["ubicacion"] = loc or ""

            # ── DESCRIPCIÓN ────────────────────────────────────────────────
            desc = get_text(card.locator("div.item-description p, p.item-description").first)
            if not desc:
                desc = get_text(card.locator(".description, .description-text").first)
            data["descripcion"] = desc[:400] if desc else ""

            # ── CARACTERÍSTICAS ────────────────────────────────────────────
            metros = habitaciones = banos = None
            spans = card.locator(".item-detail-char span.item-detail, .item-details span, span[class*='detail']")
            for j in range(spans.count()):
                txt = get_text(spans.nth(j)).lower()
                if "m²" in txt or "m2" in txt:
                    metros = re.sub(r"\s+", " ", txt).strip()
                elif "hab" in txt or "dorm" in txt:
                    m = re.search(r"(\d+)", txt)
                    habitaciones = int(m.group(1)) if m else None
                elif "baño" in txt or "aseo" in txt:
                    m = re.search(r"(\d+)", txt)
                    banos = int(m.group(1)) if m else None
            data["metros"]       = metros
            data["habitaciones"] = habitaciones
            data["banos"]        = banos

            # ── IMAGEN ─────────────────────────────────────────────────────
            # Idealista carga imágenes con lazy loading:
            #   src  = placeholder transparente (data:image/... o gif 1px)
            #   data-src / data-lazy-src = URL real
            imagen = None
            img_el = card.locator(
                "picture img, figure img, .item-multimedia img, "
                "div[class*='multimedia'] img, div[class*='image'] img, img"
            ).first
            if img_el.count() > 0:
                imagen = get_attr(img_el, "data-lazy-src", "data-src", "src")
                # Descartar placeholders
                if imagen and (
                    imagen.startswith("data:") or
                    "placeholder" in imagen.lower() or
                    imagen.endswith("pixel.gif")
                ):
                    imagen = None
            data["imagen"] = imagen

            # ── OPERACIÓN ──────────────────────────────────────────────────
            data["operacion"] = (
                "alquiler"
                if "alquiler" in page.url or "alquiler" in data["url"]
                else "venta"
            )

            results.append(data)
            print(f"   ✓ [{i+1}] {data['titulo'][:55]}  {data['precio']}")

        except Exception as e:
            print(f"   ✗ Error tarjeta {i}: {e}")

    return results


# ── Imagen desde página de detalle ─────────────────────────────────────────────
def get_detail_image(page, url: str):
    """
    Entra en la página de detalle del anuncio y extrae la primera
    imagen de alta resolución. Solo se llama si el listado no dio imagen.
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        jitter(1.0, 2.0)

        selectors = [
            "div.detail-image-gallery img",
            "div#main-multimedia img",
            "ul.slider-list li:first-child img",
            "div.multimedia-gallery img",
            "div.main-image img",
            "figure img",
            "img[data-lazy-src*='idealista']",
            "img[data-src*='idealista']",
            "img[src*='img3.idealista']",
            "img[src*='img4.idealista']",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    src = get_attr(el, "data-lazy-src", "data-src", "src")
                    if src and src.startswith("http"):
                        return src
            except Exception:
                pass

        # Último recurso: cualquier imagen que parezca de Idealista
        for img in page.locator("img").all():
            try:
                src = get_attr(img, "data-lazy-src", "data-src", "src")
                if src and src.startswith("http") and any(
                    x in src for x in ["idealista", "img3.", "img4.", "images."]
                ):
                    return src
            except Exception:
                pass

    except Exception as e:
        print(f"      ⚠️  No se pudo obtener imagen del detalle: {e}")
    return None


# ── Paginación ─────────────────────────────────────────────────────────────────
def next_page_url(page):
    for sel in ["a[rel='next']", "a.icon-arrow-right-after", "li.next a", "a:has-text('Siguiente')"]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible():
                href = get_attr(el, "href")
                if href:
                    return BASE_URL + href if href.startswith("/") else href
        except Exception:
            pass
    return None


def scrape_section(page, section_url: str) -> list[dict]:
    all_items = []
    current_url = section_url
    page_n = 1

    while current_url:
        print(f"\n  📄 Página {page_n}: {current_url}")
        try:
            page.goto(current_url, wait_until="domcontentloaded", timeout=25000)
            jitter(2.0, 4.0)
        except PWTimeout:
            print("  ⚠️  Timeout")
            break

        accept_cookies(page)

        if is_blocked(page):
            handle_block(page, current_url)

        if "muro-inmobiliaria" not in page.url and "idealista" not in page.url:
            print(f"  ⚠️  Redirigido fuera del perfil: {page.url}")
            break

        items = scrape_listing_page(page)
        if not items:
            break

        all_items.extend(items)
        nxt = next_page_url(page)
        if nxt and nxt != current_url:
            current_url = nxt
            page_n += 1
            jitter(3.0, 6.0)
        else:
            break

    return all_items


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  SCRAPER MURO INMOBILIARIA — Idealista (v2)")
    print("=" * 60)

    p, browser, mode = connect(headless=False)
    ctx = build_context(browser, mode)

    if mode == "own":
        page = ctx.new_page()
        page.goto("https://www.idealista.com", wait_until="domcontentloaded", timeout=20000)
        jitter(2, 4)
        accept_cookies(page)
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=20000)
        jitter(2, 3)
    else:
        page = active_page(ctx)
        print(f"Pestaña activa: {page.url}")
        input("\n👉 Navega al perfil de Muro Inmobiliaria si no estás ya. ENTER para continuar...\n")
        jitter(1, 2)

    accept_cookies(page)

    # Scrapear secciones
    all_properties = []
    for section_url in SECTIONS:
        print(f"\n{'─'*50}\n  Sección: {section_url}")
        items = scrape_section(page, section_url)
        print(f"  → {len(items)} anuncios")
        all_properties.extend(items)
        jitter(3, 6)

    # Fallback a ruta raíz si no encontró nada
    if not all_properties:
        print("\nIntentando ruta raíz...")
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=25000)
        jitter(2, 4)
        accept_cookies(page)
        all_properties = scrape_listing_page(page)

    # Deduplicar
    seen, unique = set(), []
    for item in all_properties:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    all_properties = unique
    print(f"\n{'─'*50}")
    print(f"Total únicos: {len(all_properties)}")

    # Enriquecer imágenes faltantes desde el detalle
    sin_imagen = [item for item in all_properties if not item.get("imagen")]
    print(f"Con imagen: {len(all_properties) - len(sin_imagen)}  |  Sin imagen: {len(sin_imagen)}")

    if sin_imagen:
        print("\nObteniendo imágenes desde páginas de detalle...")
        for i, item in enumerate(sin_imagen):
            print(f"  [{i+1}/{len(sin_imagen)}] {item['titulo'][:55]}")
            img = get_detail_image(page, item["url"])
            if img:
                item["imagen"] = img
                print(f"    ✓ {img[:80]}")
            else:
                print(f"    ✗ sin imagen")
            jitter(1.5, 3.0)

    # Guardar
    if all_properties:
        OUTPUT_FILE.write_text(
            json.dumps(all_properties, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n✅ {OUTPUT_FILE.resolve()}")
        print(f"   {len(all_properties)} propiedades guardadas\n")
        for item in all_properties:
            print(f"  • {item['titulo'][:55]}")
            print(f"    Precio:    {item['precio']}")
            print(f"    Ubicación: {item['ubicacion']}")
            print(f"    Imagen:    {'✓' if item.get('imagen') else '✗ no disponible'}")
            print(f"    URL:       {item['url']}")
    else:
        print("\n⚠️  Sin propiedades. Posibles causas:")
        print("  1. Selectores desactualizados — abre el navegador, inspecciona")
        print("     una tarjeta y busca la clase CSS real del título/precio.")
        print("  2. Idealista bloqueó la sesión — espera 10 min y reintenta.")
        print("  3. La agencia no tiene anuncios activos ahora mismo.")

    try:
        if mode == "own":
            browser.close()
        p.stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
