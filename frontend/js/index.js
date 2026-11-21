const API_URL = window.location.origin.includes('file://')
    ? 'http://127.0.0.1:8000'
    : window.location.origin;

// Reserva con el catálogo (3e): servicio y descuento (opcional) lado a lado, con el precio al lado del
// título → barbero → horario.
// - Todo se arma con createElement/textContent y addEventListener: los nombres de servicios, descuentos
//   y barberos los escribe el dueño (acá no se usa innerHTML).
// - El navegador manda service_id y discount_id. El precio y la duración los calcula el servidor:
//   lo que se muestra acá es solo para que el cliente sepa cuánto va a pagar.
// - Los errores se muestran dentro del formulario (#formError), sin ventanitas.

const fmtPrice = (n) => '$' + Number(n).toLocaleString('es-AR');

// Fecha AAAA-MM-DD en la hora del teléfono/computadora (toISOString daría la de Greenwich: después
// de las 21 h de Argentina ya sería "mañana").
function localISODate(d) {
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function option(value, text) {
    const o = document.createElement('option');
    o.value = String(value);
    o.textContent = text;
    return o;
}

function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

// El detail de la API: un texto, o (422 de validación) una lista de la que mostramos el primero
function errorText(body, fallback) {
    const detail = body && body.detail;
    if (Array.isArray(detail)) {
        return String((detail[0] && detail[0].msg) || '').replace(/^Value error, /, '') || fallback;
    }
    return typeof detail === 'string' && detail ? detail : fallback;
}

document.addEventListener('DOMContentLoaded', async () => {
    const form = document.getElementById('appointmentForm');
    const serviceSelect = document.getElementById('service');
    const servicePrice = document.getElementById('servicePrice');
    const discountSelect = document.getElementById('discount');
    const barberSelect = document.getElementById('employee_id');
    const dateInput = document.getElementById('appointmentDate');
    const timeSelect = document.getElementById('timeSelect');
    const formError = document.getElementById('formError');
    const submitBtn = form.querySelector('button[type="submit"]');

    let catalog = { services: [], barbers: [], days_ahead: 60 };
    let discountId = null;           // descuento elegido (o null)
    let slotsRequest = 0;

    function showError(text) {
        formError.textContent = text || '';
        formError.hidden = !text;
    }

    function setDefaultDate() {
        const today = new Date();
        const last = new Date(today.getFullYear(), today.getMonth(), today.getDate() + (catalog.days_ahead || 0));
        dateInput.min = localISODate(today);
        dateInput.max = localISODate(last);
        dateInput.value = dateInput.min;
    }

    const currentService = () => catalog.services.find(s => String(s.id) === serviceSelect.value) || null;

    function currentDiscount() {
        const s = currentService();
        return s ? s.discounts.find(d => d.id === discountId) || null : null;
    }

    function shownPrice() {
        const s = currentService();
        const d = currentDiscount();
        return d ? d.price : (s ? s.price : null);
    }

    // Precio a la derecha de "SERVICIO": "$15.000", o con descuento "$15.000" tachado + "$12.000"
    function renderPrice() {
        const s = currentService();
        const d = currentDiscount();
        if (!s) return servicePrice.replaceChildren();
        if (d) servicePrice.replaceChildren(make('s', '', fmtPrice(s.price)), make('strong', '', fmtPrice(d.price)));
        else servicePrice.replaceChildren(make('strong', '', fmtPrice(s.price)));
    }

    // Desplegable de descuentos (a la derecha del de servicios). Primera opción (value=""): "Descuento",
    // o "Quitar descuento" si ya hay uno elegido. Sin descuentos para el servicio: deshabilitado, no se oculta.
    function renderDiscounts() {
        const s = currentService();
        const discounts = s ? s.discounts : [];
        if (!discounts.some(d => d.id === discountId)) discountId = null;
        if (!discounts.length) {
            discountSelect.replaceChildren(option('', 'Sin descuentos'));
            discountSelect.disabled = true;
            return;
        }
        discountSelect.replaceChildren(
            option('', discountId === null ? 'Descuento' : 'Quitar descuento'),
            ...discounts.map(d => option(d.id, d.name)));
        discountSelect.value = discountId === null ? '' : String(discountId);
        discountSelect.disabled = false;
    }

    function renderBarbers() {
        const s = currentService();
        const ids = s ? s.barber_ids : [];
        const keep = barberSelect.value;
        const options = [option('', 'Cualquiera disponible')];
        catalog.barbers.filter(b => ids.includes(b.id)).forEach(b => options.push(option(b.id, b.name)));
        barberSelect.replaceChildren(...options);
        if (options.some(o => o.value === keep)) barberSelect.value = keep;
    }

    function setTimes(placeholder, times) {
        timeSelect.replaceChildren(option('', placeholder), ...(times || []).map(t => option(t, t)));
        timeSelect.disabled = !(times && times.length);
    }

    async function updateAvailableTimes() {
        const s = currentService();
        if (!s || !dateInput.value) {
            setTimes('Primero elegí un servicio y una fecha');
            return;
        }
        const mine = ++slotsRequest;
        setTimes('Cargando horarios...');
        const params = new URLSearchParams({ target_date: dateInput.value, service_id: String(s.id) });
        if (barberSelect.value) params.set('employee_id', barberSelect.value);
        try {
            const res = await fetch(`${API_URL}/appointments/available-slots?${params}`);
            const body = await res.json().catch(() => null);
            if (mine !== slotsRequest) return;           // llegó tarde: ya se pidió otra combinación
            if (!res.ok) {
                setTimes('No se pudieron cargar los horarios');
                if (formError.hidden) showError(errorText(body, 'No se pudieron cargar los horarios.'));
            } else if (!body.length) {
                setTimes('Día no laborable o sin disponibilidad');
            } else {
                setTimes('Elegí un horario', body);
            }
        } catch (error) {
            if (mine === slotsRequest) setTimes('Error de conexión');
        }
    }

    function onServiceChange(keepError) {
        if (keepError !== true) showError('');
        discountId = null;           // cambiar de servicio quita el descuento
        renderDiscounts();
        renderBarbers();
        renderPrice();
        updateAvailableTimes();
    }

    // 1. Catálogo: servicios (con sus descuentos y quiénes los hacen) y barberos
    try {
        const res = await fetch(`${API_URL}/appointments/catalog`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        catalog = await res.json();
    } catch (error) {
        showError('No pudimos cargar los servicios. Recargá la página en un rato.');
    }
    setDefaultDate();
    if (catalog.services.length) {
        serviceSelect.replaceChildren(...catalog.services.map(s => option(s.id, s.name)));   // el precio va arriba
        serviceSelect.disabled = false;
    } else {
        serviceSelect.replaceChildren(option('', 'No hay servicios para reservar'));
        submitBtn.disabled = true;
        if (formError.hidden) showError('No hay servicios para reservar online. Consultá en la barbería.');
    }

    serviceSelect.addEventListener('change', onServiceChange);
    discountSelect.addEventListener('change', () => {
        discountId = discountSelect.value ? Number(discountSelect.value) : null;
        showError('');
        renderDiscounts();           // la primera opción pasa a "Quitar descuento" (o vuelve a "Aplicar")
        renderPrice();
    });
    barberSelect.addEventListener('change', () => { showError(''); updateAvailableTimes(); });
    dateInput.addEventListener('change', () => { showError(''); updateAvailableTimes(); });

    // 2. Confirmar turno
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        showError('');
        const s = currentService();
        if (!s) return showError('Elegí un servicio.');
        if (!timeSelect.value) return showError('Elegí un horario.');

        const discount = currentDiscount();
        const priceSeen = shownPrice();
        const payload = {
            client_name: document.getElementById('client_name').value,
            client_phone: document.getElementById('client_phone').value,
            service_id: s.id,
            discount_id: discount ? discount.id : null,
            date: dateInput.value,
            time: timeSelect.value,
            employee_id: barberSelect.value ? Number(barberSelect.value) : null
        };

        const originalText = submitBtn.textContent;
        submitBtn.textContent = 'Procesando...';
        submitBtn.disabled = true;
        try {
            const res = await fetch(`${API_URL}/appointments/`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const body = await res.json().catch(() => null);
            if (res.ok) {
                showConfirmation(body, priceSeen);
                form.reset();
                setDefaultDate();
                onServiceChange();
            } else {
                showError(`No se pudo reservar: ${errorText(body, 'el horario ya no está disponible.')}`);
                // Si el problema fue el horario, el servicio o el barbero, los horarios pueden haber cambiado
                if ([400, 404, 409].includes(res.status)) updateAvailableTimes();
            }
        } catch (error) {
            showError('Error de conexión con el servidor. Intentá de nuevo.');
        } finally {
            submitBtn.textContent = originalText;
            submitBtn.disabled = !catalog.services.length;
        }
    });

    // 3. Turno confirmado (con el botón de WhatsApp si está prendido)
    function showConfirmation(appt, priceSeen) {
        const [y, m, d] = appt.date.split('-');
        const panel = make('div', 'wa-panel');
        panel.id = 'confirmPanel';

        const what = [appt.employee_name, appt.service, appt.discount_name].filter(Boolean).join(' · ');
        panel.append(make('h3', '', '¡Turno confirmado!'),
                     make('p', 'wa-detail confirm-what', what),
                     make('p', 'wa-detail', `${d}/${m}/${y} · ${appt.time} hs · ${fmtPrice(appt.price)}`));
        if (priceSeen !== null && appt.price !== priceSeen) {
            panel.append(make('p', 'wa-hint', `El precio cambió mientras reservabas: es ${fmtPrice(appt.price)}.`));
        }

        if (appt.wa_link) {
            const btn = make('a', 'wa-btn', 'Quiero el recordatorio por WhatsApp');
            btn.href = appt.wa_link;
            btn.target = '_blank';
            btn.rel = 'noopener noreferrer';
            panel.append(btn,
                make('p', 'wa-hint', 'Se abre WhatsApp con el mensaje listo: solo tocá enviar. Te confirmamos el turno y te mandamos un recordatorio.'),
                make('p', 'wa-hint', 'Si en 1 minuto no te llega la confirmación por WhatsApp, mandá el código de nuevo.'));
        }

        const back = make('button', 'wa-back', 'Reservar otro turno');
        back.type = 'button';
        back.addEventListener('click', () => {
            panel.remove();
            form.hidden = false;
        });
        panel.append(back);
        form.hidden = true;
        form.insertAdjacentElement('afterend', panel);
    }

    onServiceChange(true);           // sin borrar el aviso de "no hay servicios"
});
