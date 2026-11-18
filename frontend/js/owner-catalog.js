// "Servicios del local" y "Descuentos" (Entrega 3d): pantallas SOLO del dueño sobre la API del catálogo (3b).
// Seguridad: los nombres van SIEMPRE con textContent (nunca innerHTML) y los eventos con addEventListener
// (sin onclick en el HTML). Los precios los valida siempre el servidor: acá solo se revisa el formato.
// Usa API_URL, sessionExpired() y showX() de panel.js.
(function () {
    'use strict';

    const DURATIONS = [];
    for (let m = 5; m <= 180; m += 5) DURATIONS.push(m);
    const PRICE_MIN = 1, PRICE_MAX = 1000000;

    const svcCard = document.getElementById('ownerServicesCard');
    const svcList = document.getElementById('ownerServicesList');
    const svcStatus = document.getElementById('ownerServicesStatus');
    const svcAddBtn = document.getElementById('ownerServiceAddBtn');
    const svcNew = document.getElementById('ownerServiceNew');
    const dscCard = document.getElementById('ownerDiscountsCard');
    const dscList = document.getElementById('ownerDiscountsList');
    const dscStatus = document.getElementById('ownerDiscountsStatus');
    const dscAddBtn = document.getElementById('ownerDiscountAddBtn');
    const dscNew = document.getElementById('ownerDiscountNew');
    if (!svcCard || !svcList || !svcStatus || !svcAddBtn || !svcNew ||
        !dscCard || !dscList || !dscStatus || !dscAddBtn || !dscNew) return;

    let services = [];
    let discounts = [];

    // ---------- Utilidades ----------
    class SessionEnded extends Error {}

    function make(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function button(text, className, onClick) {
        const b = make('button', className, text);
        b.type = 'button';
        b.addEventListener('click', onClick);
        return b;
    }

    function ars(n) {
        return '$' + Number(n || 0).toLocaleString('es-AR');
    }

    function plural(n, one, many) {
        return `${n} ${n === 1 ? one : many}`;
    }

    // "15000", "15.000" o "$ 15.000" → 15000. Cualquier otra cosa (centavos, letras, vacío) → null.
    function parsePrice(text) {
        const t = String(text || '').trim().replace(/^\$\s*/, '');
        if (!/^(\d{1,3}(\.\d{3})+|\d+)$/.test(t)) return null;
        return Number(t.replace(/\./g, ''));
    }

    function nearestValid(minutes) {
        const n = Math.round((Number(minutes) || 30) / 5) * 5;
        return Math.min(180, Math.max(5, n));
    }

    function setStatus(el, text, kind) {
        el.textContent = text || '';
        el.hidden = !text;
        el.classList.remove('is-ok', 'is-warn', 'is-error');
        if (kind) el.classList.add(`is-${kind}`);
        if (text && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
    }

    function errorText(data, fallback) {
        const d = data && data.detail;
        if (Array.isArray(d)) return String((d[0] && d[0].msg) || 'Datos inválidos.').replace(/^Value error, /, '');
        return d ? String(d) : fallback;
    }

    async function api(path, method, body) {
        const headers = { 'Authorization': `Bearer ${localStorage.getItem('access_token')}` };
        const options = { method: method || 'GET', headers };
        if (body !== undefined) {
            headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(body);
        }
        const res = await fetch(`${API_URL}/admin/catalog${path}`, options);
        if (res.status === 401) {
            sessionExpired();
            throw new SessionEnded();
        }
        const data = await res.json().catch(() => ({}));
        return { ok: res.ok, status: res.status, data };
    }

    // Deshabilita los botones de un bloque mientras se espera al servidor (evita dobles clics)
    async function busy(container, work) {
        const controls = Array.from(container.querySelectorAll('button, input, select'));
        controls.forEach(c => { c.disabled = true; });
        try {
            return await work();
        } catch (err) {
            if (err instanceof SessionEnded) return undefined;
            throw err;
        } finally {
            controls.forEach(c => { c.disabled = false; });
        }
    }

    function field(labelText, control) {
        const label = make('label', 'owner-field');
        label.append(make('span', 'owner-field-label', labelText), control);
        return label;
    }

    function priceInput(value, ariaLabel) {
        const input = document.createElement('input');
        input.type = 'text';
        input.inputMode = 'numeric';
        input.autocomplete = 'off';
        input.className = 'time-input owner-price-input';
        input.maxLength = 9;
        input.placeholder = '$';
        if (value !== undefined && value !== null && value !== '') input.value = String(value);
        if (ariaLabel) input.setAttribute('aria-label', ariaLabel);
        return input;
    }

    function formActions(saveText, onCancel) {
        const actions = make('div', 'owner-row-actions');
        const save = make('button', 'owner-btn owner-btn-primary', saveText);
        save.type = 'submit';
        actions.append(save, button('Cancelar', 'owner-btn', onCancel));
        return actions;
    }

    function formError() {
        const p = make('p', 'my-services-status is-error owner-form-error');
        p.setAttribute('role', 'alert');
        p.hidden = true;
        return p;
    }

    function showFormError(p, text) {
        p.textContent = text;
        p.hidden = !text;
        if (text && p.scrollIntoView) p.scrollIntoView({ block: 'nearest' });
    }

    // ---------- Carga ----------
    async function loadOwnerCatalog() {
        setStatus(svcStatus, '');
        setStatus(dscStatus, '');
        svcNew.replaceChildren();
        dscNew.replaceChildren();
        svcAddBtn.hidden = false;
        dscAddBtn.hidden = false;
        await reload();
    }

    async function reload() {
        try {
            const [s, d] = await Promise.all([api('/services'), api('/discounts')]);
            if (!s.ok || !d.ok) {
                const msg = (s.status === 403 || d.status === 403)
                    ? 'Esta sección es solo para el dueño.'
                    : 'No se pudo cargar el catálogo. Probá de nuevo.';
                svcList.replaceChildren(make('p', 'empty-exceptions', msg));
                dscList.replaceChildren(make('p', 'empty-exceptions', msg));
                return;
            }
            services = Array.isArray(s.data) ? s.data : [];
            discounts = Array.isArray(d.data) ? d.data : [];
            renderServices();
            renderDiscounts();
        } catch (err) {
            if (err instanceof SessionEnded) return;
            console.error('Error al cargar el catálogo:', err);
            svcList.replaceChildren(make('p', 'empty-exceptions', 'No se pudo cargar el catálogo. Probá de nuevo.'));
            dscList.replaceChildren(make('p', 'empty-exceptions', 'No se pudo cargar el catálogo. Probá de nuevo.'));
        }
    }

    // Después de cambiar un servicio, "Mis servicios" (3c) también cambia: se recarga
    async function afterServiceChange() {
        await reload();
        if (typeof window.loadMyServices === 'function') await window.loadMyServices();
    }

    // ---------- Servicios ----------
    function renderServices() {
        const active = services.filter(s => s.active);
        const inactive = services.filter(s => !s.active);
        const nodes = [];
        if (!services.length) {
            nodes.push(make('p', 'empty-exceptions', 'Todavía no hay servicios. Agregá el primero con el botón de abajo.'));
        }
        active.forEach(s => nodes.push(serviceRow(s)));
        if (inactive.length) {
            nodes.push(make('p', 'owner-group-title', `Desactivados (${inactive.length})`));
            inactive.forEach(s => nodes.push(serviceRow(s)));
        }
        svcList.replaceChildren(...nodes);
    }

    function serviceRow(s) {
        const row = make('div', 'schedule-day-item owner-row');
        row.dataset.serviceId = String(s.id);
        row.classList.toggle('day-off', !s.active);

        const head = make('div', 'owner-row-head');
        head.append(make('span', 'day-name-text owner-name', s.name));
        if (!s.active) head.append(make('span', 'owner-badge', 'Desactivado'));
        row.append(head, make('p', 'my-svc-meta owner-meta', `${ars(s.price)} · ${s.base_duration} min (duración base)`));
        if (s.active && !s.barbers) {
            row.append(make('p', 'my-svc-warn owner-warn',
                'Todavía ningún barbero hace este servicio: cada uno lo marca en "Mis servicios".'));
        }

        const actions = make('div', 'owner-row-actions');
        actions.append(button('Editar', 'owner-btn', () => editService(row, s)));
        if (s.active) {
            actions.append(button('Desactivar', 'owner-btn owner-btn-danger', () => setServiceActive(row, s, false)));
        } else {
            actions.append(button('Reactivar', 'owner-btn owner-btn-primary', () => setServiceActive(row, s, true)));
        }
        row.append(actions);
        return row;
    }

    function serviceForm(s, saveText, onSubmit, onCancel) {
        const form = make('form', 'owner-form');
        form.noValidate = true;
        const name = document.createElement('input');
        name.type = 'text';
        name.maxLength = 40;
        name.autocomplete = 'off';
        name.className = 'owner-name-input';
        name.value = s ? s.name : '';
        const price = priceInput(s ? s.price : '', 'Precio en pesos');
        const duration = document.createElement('select');
        duration.className = 'time-input owner-duration';
        DURATIONS.forEach(m => duration.appendChild(new Option(`${m} min`, String(m))));
        duration.value = String(nearestValid(s ? s.base_duration : 30));

        const grid = make('div', 'owner-form-grid');
        grid.append(field('Precio ($)', price), field('Duración base', duration));
        const error = formError();
        form.append(field('Nombre', name), grid, error, formActions(saveText, onCancel));
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const p = parsePrice(price.value);
            if (p === null || p < PRICE_MIN || p > PRICE_MAX) {
                showFormError(error, 'Revisá el precio: escribí solo números, sin centavos (por ejemplo 15000 o 15.000), entre $1 y $1.000.000.');
                return;
            }
            showFormError(error, '');
            await busy(form, () => onSubmit({ name: name.value, price: p, base_duration: Number(duration.value) }, error));
        });
        return form;
    }

    function openNewService() {
        setStatus(svcStatus, '');
        svcAddBtn.hidden = true;
        const close = () => { svcNew.replaceChildren(); svcAddBtn.hidden = false; };
        const form = serviceForm(null, 'Crear servicio', async (values, error) => {
            const r = await api('/services', 'POST', values);
            if (!r.ok) return showFormError(error, errorText(r.data, 'No se pudo crear el servicio.'));
            close();
            await afterServiceChange();
            setStatus(svcStatus, `Servicio “${r.data.name}” creado. Ningún barbero lo hace todavía: cada uno lo marca en "Mis servicios".`, 'ok');
        }, close);
        form.classList.add('owner-form-new');
        svcNew.replaceChildren(make('p', 'owner-group-title', 'Nuevo servicio'), form);
        form.querySelector('input').focus();
    }

    function editService(row, s) {
        setStatus(svcStatus, '');
        const form = serviceForm(s, 'Guardar', async (values, error) => {
            const changes = {};
            Object.keys(values).forEach(k => { if (values[k] !== s[k]) changes[k] = values[k]; });
            if (!Object.keys(changes).length) {
                renderServices();
                return setStatus(svcStatus, 'No había cambios para guardar.', 'ok');
            }
            const r = await api(`/services/${s.id}`, 'PATCH', changes);
            if (!r.ok) return showFormError(error, errorText(r.data, 'No se pudo guardar el servicio.'));
            await afterServiceChange();
            setStatus(svcStatus, `Servicio “${r.data.name}” guardado.`, 'ok');
        }, () => renderServices());
        row.replaceChildren(form);
        row.classList.remove('day-off');
        form.querySelector('input').focus();
    }

    async function setServiceActive(row, s, active) {
        setStatus(svcStatus, '');
        await busy(row, async () => {
            const r = await api(`/services/${s.id}/${active ? 'reactivate' : 'deactivate'}`, 'POST');
            if (!r.ok) return setStatus(svcStatus, errorText(r.data, 'No se pudo cambiar el servicio.'), 'error');
            await afterServiceChange();
            const n = Number(r.data.future_appointments) || 0;
            const name = r.data.service.name;
            if (active) return setStatus(svcStatus, `Servicio “${name}” reactivado.`, 'ok');
            const turnos = n
                ? ` Tiene ${plural(n, 'turno futuro reservado', 'turnos futuros reservados')}: ${n === 1 ? 'sigue' : 'siguen'} en pie.`
                : ' No tiene turnos futuros.';
            setStatus(svcStatus, `Servicio “${name}” desactivado.${turnos} Lo podés reactivar cuando quieras.`,
                n ? 'warn' : 'ok');
        });
    }

    // ---------- Descuentos ----------
    function renderDiscounts() {
        const active = discounts.filter(d => d.active);
        const inactive = discounts.filter(d => !d.active);
        const nodes = [];
        if (!discounts.length) {
            nodes.push(make('p', 'empty-exceptions', 'Todavía no hay descuentos.'));
        }
        active.forEach(d => nodes.push(discountRow(d)));
        if (inactive.length) {
            nodes.push(make('p', 'owner-group-title', `Desactivados (${inactive.length})`));
            inactive.forEach(d => nodes.push(discountRow(d)));
        }
        dscList.replaceChildren(...nodes);
        dscAddBtn.disabled = !services.some(s => s.active);
    }

    function discountRow(d) {
        const row = make('div', 'schedule-day-item owner-row');
        row.dataset.discountId = String(d.id);
        row.classList.toggle('day-off', !d.active);

        const head = make('div', 'owner-row-head');
        head.append(make('span', 'day-name-text owner-name', d.name));
        if (!d.active) head.append(make('span', 'owner-badge', 'Desactivado'));
        row.append(head);

        const prices = make('ul', 'owner-price-list');
        d.prices.forEach(p => {
            const base = services.find(s => s.id === p.service_id);
            const li = make('li', p.service_active ? '' : 'owner-price-off');
            li.append(make('span', 'owner-price-name', p.service_name));
            const detail = p.service_active
                ? `${ars(p.price)}${base ? ` (base ${ars(base.price)})` : ''}`
                : `${ars(p.price)} · servicio desactivado (se conserva)`;
            li.append(make('span', 'owner-price-value', detail));
            prices.append(li);
        });
        row.append(prices);

        const actions = make('div', 'owner-row-actions');
        actions.append(button('Editar', 'owner-btn', () => editDiscount(row, d)));
        if (d.active) {
            actions.append(button('Desactivar', 'owner-btn owner-btn-danger', () => setDiscountActive(row, d, false)));
        } else {
            actions.append(button('Reactivar', 'owner-btn owner-btn-primary', () => setDiscountActive(row, d, true)));
        }
        row.append(actions);
        return row;
    }

    function discountForm(d, saveText, onSubmit, onCancel) {
        const form = make('form', 'owner-form');
        form.noValidate = true;
        const name = document.createElement('input');
        name.type = 'text';
        name.maxLength = 40;
        name.autocomplete = 'off';
        name.className = 'owner-name-input';
        name.value = d ? d.name : '';

        const current = {};
        (d ? d.prices : []).forEach(p => { current[p.service_id] = p; });

        const box = make('div', 'owner-price-box');
        box.append(make('p', 'owner-help', 'Precio final con descuento. Dejalo vacío si el descuento no aplica a ese servicio.'));
        const inputs = [];
        services.filter(s => s.active).forEach(s => {
            const line = make('div', 'owner-price-line');
            const label = make('div', 'owner-price-label');
            label.append(make('span', 'owner-price-name', s.name), make('span', 'owner-price-base', `base ${ars(s.price)}`));
            const input = priceInput(current[s.id] ? current[s.id].price : '', `Precio con descuento de ${s.name}`);
            line.append(label, input);
            box.append(line);
            inputs.push({ service: s, input });
        });
        const kept = Object.values(current).filter(p => !p.service_active);
        if (kept.length) {
            const note = make('ul', 'owner-price-list owner-kept');
            kept.forEach(p => {
                const li = make('li', 'owner-price-off');
                li.append(make('span', 'owner-price-name', p.service_name),
                    make('span', 'owner-price-value', `${ars(p.price)} · servicio desactivado (se conserva)`));
                note.append(li);
            });
            box.append(note);
        }

        const error = formError();
        form.append(field('Nombre del descuento', name), box, error, formActions(saveText, onCancel));
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const prices = [];
            for (const { service, input } of inputs) {
                if (!input.value.trim()) continue;
                const p = parsePrice(input.value);
                if (p === null || p < PRICE_MIN) {
                    showFormError(error, `Revisá el precio de “${service.name}”: escribí solo números, sin centavos (por ejemplo 12000 o 12.000).`);
                    input.focus();
                    return;
                }
                prices.push({ service_id: service.id, price: p });
            }
            if (!prices.length) {
                showFormError(error, 'Poné el precio con descuento de al menos un servicio.');
                return;
            }
            showFormError(error, '');
            await busy(form, () => onSubmit({ name: name.value, prices }, error));
        });
        return form;
    }

    function openNewDiscount() {
        setStatus(dscStatus, '');
        dscAddBtn.hidden = true;
        const close = () => { dscNew.replaceChildren(); dscAddBtn.hidden = false; };
        const form = discountForm(null, 'Crear descuento', async (values, error) => {
            const r = await api('/discounts', 'POST', values);
            if (!r.ok) return showFormError(error, errorText(r.data, 'No se pudo crear el descuento.'));
            close();
            await reload();
            setStatus(dscStatus, `Descuento “${r.data.name}” creado.`, 'ok');
        }, close);
        form.classList.add('owner-form-new');
        dscNew.replaceChildren(make('p', 'owner-group-title', 'Nuevo descuento'), form);
        form.querySelector('input').focus();
    }

    function editDiscount(row, d) {
        setStatus(dscStatus, '');
        const form = discountForm(d, 'Guardar', async (values, error) => {
            const r = await api(`/discounts/${d.id}`, 'PATCH', values);
            if (!r.ok) return showFormError(error, errorText(r.data, 'No se pudo guardar el descuento.'));
            await reload();
            setStatus(dscStatus, `Descuento “${r.data.name}” guardado.`, 'ok');
        }, () => renderDiscounts());
        row.replaceChildren(form);
        row.classList.remove('day-off');
        form.querySelector('input').focus();
    }

    async function setDiscountActive(row, d, active) {
        setStatus(dscStatus, '');
        await busy(row, async () => {
            const r = await api(`/discounts/${d.id}/${active ? 'reactivate' : 'deactivate'}`, 'POST');
            if (!r.ok) return setStatus(dscStatus, errorText(r.data, 'No se pudo cambiar el descuento.'), 'error');
            await reload();
            const n = Number(r.data.future_appointments) || 0;
            const name = r.data.discount.name;
            if (active) return setStatus(dscStatus, `Descuento “${name}” reactivado.`, 'ok');
            const turnos = n
                ? ` Tiene ${plural(n, 'turno futuro reservado', 'turnos futuros reservados')} con este descuento: ${n === 1 ? 'no cambia' : 'no cambian'}.`
                : '';
            setStatus(dscStatus, `Descuento “${name}” desactivado.${turnos}`, n ? 'warn' : 'ok');
        });
    }

    svcAddBtn.addEventListener('click', openNewService);
    dscAddBtn.addEventListener('click', openNewDiscount);

    // Botones de navegación de la tarjeta (las funciones son de panel.js)
    const GO = {
        metrics: () => showMetrics(),
        schedule: () => showSchedule(),
        exceptions: () => showExceptions(),
        dashboard: () => showDashboard(),
    };
    svcCard.querySelectorAll('[data-go]').forEach(btn => {
        btn.addEventListener('click', () => {
            const go = GO[btn.dataset.go];
            if (go) go();
        });
    });

    // panel.js la llama al abrir "Servicios", solo si el usuario es el dueño
    window.loadOwnerCatalog = loadOwnerCatalog;
})();
