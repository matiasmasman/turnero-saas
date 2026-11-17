// "Mis servicios" (Entrega 3c): cada barbero elige qué servicios hace y cuánto tarda en cada uno.
// Seguridad: los nombres de servicios van SIEMPRE con textContent (nunca innerHTML) y los eventos
// con addEventListener (sin onclick en el HTML). Usa API_URL y logout() de panel.js.
(function () {
    'use strict';

    const DURATIONS = [];
    for (let m = 5; m <= 180; m += 5) DURATIONS.push(m);

    const card = document.getElementById('myServicesCard');
    const form = document.getElementById('myServicesForm');
    const list = document.getElementById('myServicesList');
    const statusEl = document.getElementById('myServicesStatus');
    const saveBtn = document.getElementById('myServicesSave');
    if (!card || !form || !list || !statusEl || !saveBtn) return;

    const SAVE_TEXT = 'Guardar mis servicios';

    function authHeaders(extra) {
        return Object.assign({ 'Authorization': `Bearer ${localStorage.getItem('access_token')}` }, extra || {});
    }

    function make(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function plural(n, word) {
        return `${n} ${word}${n === 1 ? '' : 's'}`;
    }

    // Una duración guardada fuera de la grilla (5 a 180, de a 5) se muestra con el valor válido más cercano
    function nearestValid(minutes) {
        const n = Math.round((Number(minutes) || 30) / 5) * 5;
        return Math.min(180, Math.max(5, n));
    }

    function setStatus(text, kind) {
        statusEl.textContent = text || '';
        statusEl.hidden = !text;
        statusEl.classList.remove('is-ok', 'is-warn', 'is-error');
        if (kind) statusEl.classList.add(`is-${kind}`);
    }

    function errorText(data) {
        const d = data && data.detail;
        if (Array.isArray(d)) return String((d[0] && d[0].msg) || 'Datos inválidos.').replace(/^Value error, /, '');
        return d ? String(d) : 'No se pudieron guardar tus servicios.';
    }

    function buildRow(svc) {
        const row = make('div', 'schedule-day-item my-svc-row');
        row.dataset.serviceId = String(svc.service_id);

        const header = make('div', 'schedule-day-header my-svc-header');
        const label = make('label', 'schedule-day-toggle my-svc-toggle');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'my-svc-check';
        checkbox.id = `my-svc-${svc.service_id}`;
        checkbox.checked = Boolean(svc.does);
        label.htmlFor = checkbox.id;
        label.append(checkbox, make('span', 'day-name-text my-svc-name', svc.name));

        const select = document.createElement('select');
        select.className = 'time-input my-svc-duration';
        select.setAttribute('aria-label', `Duración de ${svc.name}`);
        DURATIONS.forEach(m => select.appendChild(new Option(`${m} min`, String(m))));
        select.value = String(nearestValid(svc.duration));
        header.append(label, select);

        const meta = make('p', 'my-svc-meta', `Lo hago · base del local: ${svc.base_duration} min`);
        const warn = make('p', 'my-svc-warn');
        row.append(header, meta, warn);

        function refresh() {
            const on = checkbox.checked;
            row.classList.toggle('day-off', !on);
            select.disabled = !on;
            meta.textContent = on ? `Lo hago · base del local: ${svc.base_duration} min`
                                  : `No lo hago · base del local: ${svc.base_duration} min`;
            const n = Number(svc.future_appointments) || 0;
            const showWarn = svc.does && !on && n > 0;
            warn.textContent = showWarn
                ? `Tenés ${plural(n, 'turno')} ${n === 1 ? 'futuro' : 'futuros'} con este servicio: siguen reservados.`
                : '';
            warn.hidden = !showWarn;
        }
        checkbox.addEventListener('change', refresh);
        refresh();
        return row;
    }

    function render(services) {
        if (!services.length) {
            list.replaceChildren(make('p', 'empty-exceptions',
                'Todavía no hay servicios activos en el catálogo. Cuando el dueño los cargue, vas a poder elegirlos acá.'));
            saveBtn.hidden = true;
            return;
        }
        list.replaceChildren(...services.map(buildRow));
        saveBtn.hidden = false;
    }

    async function loadMyServices() {
        setStatus('');
        saveBtn.hidden = true;
        list.replaceChildren(make('p', 'empty-exceptions', 'Cargando...'));
        try {
            const res = await fetch(`${API_URL}/admin/my-services`, { headers: authHeaders() });
            if (res.status === 401) return logout();
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            render(Array.isArray(data.services) ? data.services : []);
        } catch (err) {
            console.error('Error al cargar mis servicios:', err);
            list.replaceChildren(make('p', 'empty-exceptions', 'No se pudieron cargar tus servicios. Probá de nuevo.'));
        }
    }

    async function saveMyServices(event) {
        event.preventDefault();
        const rows = Array.from(list.querySelectorAll('.my-svc-row'));
        const services = rows
            .filter(row => row.querySelector('.my-svc-check').checked)
            .map(row => ({
                service_id: Number(row.dataset.serviceId),
                duration: Number(row.querySelector('.my-svc-duration').value),
            }));

        if (rows.length && !services.length && !confirm(
            'No marcaste ningún servicio: no vas a aparecer para que te reserven. ¿Guardar igual?')) {
            return;
        }

        saveBtn.disabled = true;
        saveBtn.textContent = 'Guardando...';
        setStatus('');
        try {
            const res = await fetch(`${API_URL}/admin/my-services`, {
                method: 'PUT',
                headers: authHeaders({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ services }),
            });
            if (res.status === 401) return logout();
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                setStatus(errorText(data), 'error');
                return;
            }
            render(Array.isArray(data.services) ? data.services : []);
            const warnings = Array.isArray(data.warnings) ? data.warnings : [];
            const first = data.changed ? 'Tus servicios se guardaron.' : 'No había cambios para guardar.';
            setStatus([first, ...warnings].join('\n'), warnings.length ? 'warn' : 'ok');
        } catch (err) {
            setStatus('Error de conexión con el servidor. Probá de nuevo.', 'error');
        } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = SAVE_TEXT;
        }
    }

    form.addEventListener('submit', saveMyServices);

    // Botones de navegación de la tarjeta (las funciones son de panel.js)
    const GO = {
        metrics: () => showMetrics(),
        schedule: () => showSchedule(),
        exceptions: () => showExceptions(),
        dashboard: () => showDashboard(),
    };
    card.querySelectorAll('[data-go]').forEach(btn => {
        btn.addEventListener('click', () => {
            const go = GO[btn.dataset.go];
            if (go) go();
        });
    });

    // panel.js la llama al abrir la sección "Servicios"
    window.loadMyServices = loadMyServices;
})();
