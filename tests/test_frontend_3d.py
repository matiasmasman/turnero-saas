"""Controles estáticos del frontend del 3d ("Servicios del local" y "Descuentos", solo dueño).
Las pruebas de navegador de verdad están en test_navegador_3d.py (Playwright)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "frontend/js/owner-catalog.js").read_text(encoding="utf-8")
PANEL_JS = (ROOT / "frontend/js/panel.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend/panel.html").read_text(encoding="utf-8")


def code_only(js: str) -> str:
    """El JS sin comentarios (los comentarios pueden nombrar innerHTML u onclick para explicar que no se usan)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(line.split("//")[0] if line.lstrip().startswith("//") or " // " in line else line
                     for line in js.splitlines())


def owner_cards_html() -> str:
    start = HTML.index('id="ownerServicesCard"')
    return HTML[start:HTML.index('id="myServicesCard"')]


def test_el_js_nuevo_no_arma_html_con_texto():
    code = code_only(JS)
    assert "fetch(`${API_URL}/admin/catalog${path}`" in code       # el recorte de comentarios no rompe el código
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "onclick", "eval(",
                      "alert(", "confirm("):
        assert forbidden not in code, forbidden


def test_los_nombres_van_con_textcontent_y_los_eventos_con_addeventlistener():
    assert "node.textContent = text" in JS
    assert "make('span', 'day-name-text owner-name', s.name)" in JS
    assert "make('span', 'day-name-text owner-name', d.name)" in JS
    assert "make('span', 'owner-price-name', p.service_name)" in JS
    assert "b.addEventListener('click', onClick)" in JS and "form.addEventListener('submit'" in JS


def test_sesion_vencida_va_al_login():
    assert "if (res.status === 401) {\n            logout();" in JS


def test_las_tarjetas_nuevas_no_tienen_onclick_ni_estilos_en_linea():
    cards = owner_cards_html()
    assert re.search(r"\sonclick\s*=", cards) is None
    assert re.search(r"\sstyle\s*=", cards) is None
    for needed in ('id="ownerServicesList"', 'id="ownerDiscountsList"', 'id="ownerServiceAddBtn"',
                   'id="ownerDiscountAddBtn"', 'data-go="metrics"'):
        assert needed in cards, needed
    assert 'id="ownerServicesCard" hidden' in cards and 'id="ownerDiscountsCard" hidden' in cards


def test_no_queda_la_tarjeta_vieja_de_precios():
    for old in ("ownerPricesCard", "pricesForm", "priceCortePromo2x", "priceCorteVip", "Guardar Precios"):
        assert old not in HTML, old
        assert old not in PANEL_JS, old
    # La agenda sigue usando los precios viejos hasta el 3f
    assert "/admin/service-prices" in PANEL_JS and "shopPrices" in PANEL_JS
    assert "if (isOwner) loads.push(loadOwnerCatalog());" in PANEL_JS


def test_escapehtml_escapa_la_comilla_simple():
    body = PANEL_JS[PANEL_JS.index("function escapeHtml"):PANEL_JS.index("function renderAgenda")]
    for pattern in ("/&/g, '&amp;'", "/</g, '&lt;'", "/>/g, '&gt;'", "/\"/g, '&quot;'", "/'/g, '&#39;'"):
        assert pattern in body, pattern


def test_versiones_de_cache_subidas():
    assert 'href="css/panel.css?v=12"' in HTML
    assert 'src="js/panel.js?v=12"' in HTML
    assert 'src="js/owner-catalog.js?v=1"' in HTML
    assert HTML.index('js/panel.js?v=12') < HTML.index('js/owner-catalog.js?v=1')
