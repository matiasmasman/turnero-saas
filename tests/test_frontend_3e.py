"""Controles estáticos de la web pública de reservas (3e). Las pruebas de navegador de verdad están en
test_navegador_3e.py (Playwright)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "frontend/js/index.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/css/main.css").read_text(encoding="utf-8")


def code_only(js: str) -> str:
    """El JS sin comentarios (los comentarios pueden nombrar innerHTML o toISOString para explicar que no se usan)."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(line.split("//")[0] if line.lstrip().startswith("//") or " // " in line else line
                     for line in js.splitlines())


def form_html() -> str:
    start = HTML.index('<form id="appointmentForm"')
    return HTML[start:HTML.index("</form>", start)]


def test_el_js_no_arma_html_con_texto_ni_usa_ventanitas():
    code = code_only(JS)
    assert "fetch(`${API_URL}/appointments/catalog`)" in code       # el recorte de comentarios no rompe el código
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "onclick", "eval(",
                      "alert(", "confirm(", "querySelector(`"):
        assert forbidden not in code, forbidden


def test_los_nombres_van_con_textcontent_y_los_eventos_con_addeventlistener():
    assert "o.textContent = text;" in JS and "node.textContent = text;" in JS
    assert "option(s.id, s.name)" in JS                              # el desplegable: solo nombres
    assert "option(d.id, `${d.name} — ${fmtPrice(d.price)}`)" in JS
    assert "option('', discountId === null ? 'Descuento' : 'Quitar descuento')" in JS
    assert "option('', 'Sin descuentos')" in JS
    assert "options.push(option(b.id, b.name))" in JS
    for listener in ("serviceSelect.addEventListener('change'", "discountSelect.addEventListener('change'",
                     "barberSelect.addEventListener('change'", "dateInput.addEventListener('change'",
                     "form.addEventListener('submit'", "back.addEventListener('click'"):
        assert listener in JS, listener


def test_descuento_al_lado_del_servicio_y_deshabilitado_si_no_hay():
    pair = HTML[HTML.index('<div class="select-pair">'):HTML.index("</div>", HTML.index('<div class="select-pair">'))]
    assert pair.index('<select id="service"') < pair.index('<select id="discount" aria-label="Descuento" disabled>')
    assert "discountSelect.disabled = true;" in JS and "discountSelect.disabled = false;" in JS
    assert "minmax(0, 1fr) minmax(0, 1fr)" in CSS and "text-overflow: ellipsis" in CSS


def test_cambiar_de_servicio_quita_el_descuento():
    body = JS[JS.index("function onServiceChange("):JS.index("renderDiscounts();", JS.index("function onServiceChange("))]
    assert "discountId = null;" in body


def test_manda_ids_y_nunca_precio_ni_duracion():
    payload = JS[JS.index("const payload = {"):JS.index("};", JS.index("const payload = {"))]
    assert "service_id: s.id" in payload and "discount_id: discount ? discount.id : null" in payload
    for forbidden in ("price", "duration", "service:"):
        assert forbidden not in payload, forbidden


def test_fecha_por_defecto_en_hora_local():
    code = code_only(JS)
    assert "toISOString" not in code
    assert "d.getFullYear()" in code and "dateInput.value = dateInput.min;" in code


def test_los_errores_van_dentro_del_formulario():
    form = form_html()
    assert '<p id="formError" class="form-error" role="alert" hidden></p>' in form
    assert form.index('id="formError"') < form.index('<button type="submit">')
    assert "formError.textContent = text || '';" in JS


def test_el_formulario_no_tiene_nombres_onclick_ni_estilos_en_linea():
    form = form_html()
    assert re.search(r"\sonclick\s*=", form) is None
    assert re.search(r"\sstyle\s*=", form) is None
    for old in ('value="Corte"', 'value="Barba"', "Corte + Barba"):    # las opciones viejas fijas
        assert old not in form, old
    for needed in ('id="service"', 'id="servicePrice"', 'id="discount"', 'id="employee_id"',
                   'id="appointmentDate"', 'id="timeSelect"'):
        assert needed in form, needed
    for gone in ('id="discountBox"', 'id="priceSummary"', 'id="discountGroup"', 'id="discountToggle"',
                 'id="discountChips"'):                                              # diseños anteriores
        assert gone not in HTML, gone
    for gone in ("discountBox", "priceSummary", "discountGroup", "discountToggle", "discountChips", "chip"):
        assert gone not in JS, gone
    assert ".chip" not in CSS and ".link-btn" not in CSS
    # el precio va en la fila del label de "Servicio"
    row = form[form.index('<div class="label-row">'):form.index('<select id="service"')]
    assert '<label for="service">Servicio</label>' in row and 'id="servicePrice"' in row


def test_versiones_de_cache_subidas():
    assert 'href="css/main.css?v=5"' in HTML
    assert 'src="js/index.js?v=9"' in HTML
