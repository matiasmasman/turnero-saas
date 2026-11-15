"""Controles estáticos del frontend de "Mis servicios" (3c). No hay cómo correr JS en las pruebas,
así que se revisan los archivos: que no vuelvan innerHTML/onclick ni la pantalla vieja de duraciones."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "frontend/js/my-services.js").read_text(encoding="utf-8")
PANEL_JS = (ROOT / "frontend/js/panel.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend/panel.html").read_text(encoding="utf-8")


def code_only(js: str) -> str:
    """El JS sin comentarios (los comentarios pueden nombrar innerHTML u onclick para explicar que no se usan)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(line.split("//")[0] if line.lstrip().startswith("//") or " // " in line else line
                     for line in js.splitlines())


def card_html() -> str:
    start = HTML.index('id="myServicesCard"')
    return HTML[start:HTML.index("</section>", start)]


def test_el_js_nuevo_no_arma_html_con_texto():
    code = code_only(JS)
    assert "fetch(`${API_URL}/admin/my-services`" in code          # el recorte de comentarios no rompe el código
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "onclick", "eval("):
        assert forbidden not in code, forbidden


def test_los_nombres_van_con_textcontent():
    # el nombre del servicio se pone con make(..., svc.name), que usa textContent
    assert "make('span', 'day-name-text my-svc-name', svc.name)" in JS
    assert "node.textContent = text" in JS
    # los eventos van con addEventListener
    assert "form.addEventListener('submit'" in JS and "addEventListener('change'" in JS


def test_la_tarjeta_nueva_no_tiene_onclick_ni_estilos_en_linea():
    card = card_html()
    assert re.search(r"\sonclick\s*=", card) is None
    assert re.search(r"\sstyle\s*=", card) is None
    assert 'id="myServicesForm"' in card and 'id="myServicesList"' in card and 'id="myServicesSave"' in card


def test_no_queda_la_pantalla_vieja_de_duraciones():
    for old in ("durationsForm", "durCorte", "durBarba", "durCorteBarba"):
        assert old not in HTML, old
        assert old not in PANEL_JS, old
    for old in ("populateDurationSelects", "loadServiceDurations", "updateCombinedDurationDisplay",
                "/admin/service-durations", "CORTE_STEPS", "BARBA_STEPS"):
        assert old not in PANEL_JS, old
    assert "loadMyServices()" in PANEL_JS


def test_versiones_de_cache_subidas():
    # panel.css y panel.js subieron a v=12 en el 3d (ver test_frontend_3d.py)
    assert 'src="js/my-services.js?v=1"' in HTML
    # my-services.js va después de panel.js (usa API_URL, logout y showX de panel.js)
    assert HTML.index('js/panel.js?v=') < HTML.index('js/my-services.js?v=1')
