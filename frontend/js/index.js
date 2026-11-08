const API_URL = window.location.origin.includes('file://') 
    ? 'http://127.0.0.1:8000' 
    : window.location.origin;

document.addEventListener('DOMContentLoaded', async () => {
    const serviceSelect = document.getElementById('service');
    const barberSelect = document.getElementById('employee_id');
    const dateInput = document.getElementById('appointmentDate');
    const timeSelect = document.getElementById('timeSelect');

    const todayStr = new Date().toISOString().split('T')[0];
    if (dateInput) {
        dateInput.min = todayStr;
        dateInput.value = todayStr;
    }

    // 1. Cargar precios configurados por el dueño para mostrarlos en el combo de Servicio
    try {
        const resPrices = await fetch(API_URL + '/appointments/services');
        if (resPrices.ok && serviceSelect) {
            const prices = await resPrices.json();
            const fmt = (n) => Number(n).toLocaleString('es-AR');
            serviceSelect.innerHTML = `
                <option value="Corte">Corte — $${fmt(prices.price_corte)}</option>
                <option value="Barba">Barba — $${fmt(prices.price_barba)}</option>
                <option value="Corte + Barba">Corte + Barba — $${fmt(prices.price_corte_barba)}</option>
            `;
        }
    } catch (e) {
        console.error("No se pudieron cargar los precios:", e);
    }

    // 2. Cargar los Barberos dinámicamente
    try {
        const response = await fetch(API_URL + '/appointments/barbers');
        if (response.ok) {
            const barbers = await response.json();
            barberSelect.innerHTML = '<option value="">Cualquiera disponible</option>';
            barbers.forEach(barber => {
                const option = document.createElement('option');
                option.value = barber.id;
                option.textContent = barber.name;
                barberSelect.appendChild(option);
            });
        }
    } catch (error) {
        console.error("Error de conexión al obtener barberos:", error);
    }

    // 3. Función para actualizar horarios
    async function updateAvailableTimes() {
        const barberId = barberSelect.value;
        const selectedDate = dateInput.value;
        const selectedService = serviceSelect ? serviceSelect.value : 'Corte';

        if (!selectedDate) {
            timeSelect.innerHTML = '<option value="">Primero elige una fecha</option>';
            timeSelect.disabled = true;
            return;
        }

        try {
            timeSelect.innerHTML = '<option value="">Cargando horarios...</option>';
            timeSelect.disabled = true;

            let url = `${API_URL}/appointments/available-slots?target_date=${encodeURIComponent(selectedDate)}&service=${encodeURIComponent(selectedService)}`;
            if (barberId) {
                url += `&employee_id=${encodeURIComponent(barberId)}`;
            }

            const response = await fetch(url);

            if (response.ok) {
                const slots = await response.json();
                timeSelect.innerHTML = '';

                if (slots.length === 0) {
                    timeSelect.innerHTML = '<option value="">Día no laborable o sin disponibilidad</option>';
                } else {
                    timeSelect.innerHTML = '<option value="">Selecciona un horario</option>';
                    slots.forEach(slot => {
                        const option = document.createElement('option');
                        option.value = slot;
                        option.textContent = slot;
                        timeSelect.appendChild(option);
                    });
                    timeSelect.disabled = false;
                }
            } else {
                timeSelect.innerHTML = '<option value="">Error al cargar horarios</option>';
            }
        } catch (error) {
            console.error("Error de conexión al obtener horarios:", error);
            timeSelect.innerHTML = '<option value="">Error de conexión</option>';
        }
    }

    if (serviceSelect) serviceSelect.addEventListener('change', updateAvailableTimes);
    if (barberSelect) barberSelect.addEventListener('change', updateAvailableTimes);
    if (dateInput) dateInput.addEventListener('change', updateAvailableTimes);

    // 4. Confirmar Turno
    const appointmentForm = document.getElementById('appointmentForm');

    if (appointmentForm) {
        appointmentForm.addEventListener('submit', async (e) => {
            e.preventDefault();

            const submitBtn = appointmentForm.querySelector('button[type="submit"]');
            const originalText = submitBtn.textContent;
            submitBtn.textContent = 'Procesando...';
            submitBtn.disabled = true;

            const rawEmployeeId = document.getElementById('employee_id').value;

            const payload = {
                client_name: document.getElementById('client_name').value,
                client_phone: document.getElementById('client_phone').value,
                service: document.getElementById('service').value,
                date: document.getElementById('appointmentDate').value,
                time: document.getElementById('timeSelect').value,
                employee_id: rawEmployeeId ? parseInt(rawEmployeeId) : null
            };

            try {
                const response = await fetch(API_URL + '/appointments/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                if (response.ok) {
                    const createdAppt = await response.json();
                    const barberOption = barberSelect
                        ? barberSelect.querySelector(`option[value="${createdAppt.employee_id}"]`)
                        : null;
                    const assignedBarberName = createdAppt.employee_name 
                        || (barberOption ? barberOption.textContent.trim() : 'Asignado');

                    appointmentForm.reset();
                    document.getElementById('appointmentDate').value = todayStr;
                    updateAvailableTimes();

                    if (createdAppt.wa_link) {
                        // Mostramos el botón de WhatsApp en lugar del formulario
                        showWhatsAppPanel(createdAppt, assignedBarberName, payload);
                    } else {
                        alert(
                            `¡Turno confirmado con éxito!\n` +
                            `Barbero: ${assignedBarberName}\n` +
                            `Te esperamos el ${payload.date} a las ${payload.time}.`
                        );
                    }
                } else {
                    const error = await response.json();
                    // Los errores de validación (422) vienen como lista: mostramos el primero, prolijo
                    const detail = Array.isArray(error.detail)
                        ? String(error.detail[0]?.msg || '').replace(/^Value error, /, '')
                        : error.detail;
                    alert(`No se pudo reservar: ${detail || 'Horario no disponible.'}`);
                    updateAvailableTimes();
                }
            } catch (error) {
                alert("Error de conexión con el servidor. Intentá nuevamente.");
            } finally {
                submitBtn.textContent = originalText;
                submitBtn.disabled = false;
            }
        });
    }

    // 5. Panel de turno confirmado con botón de WhatsApp
    function showWhatsAppPanel(appt, barberName, payload) {
        const [y, m, d] = payload.date.split('-');
        const panel = document.createElement('div');
        panel.className = 'wa-panel';

        const title = document.createElement('h3');
        title.textContent = '¡Turno confirmado!';

        const detail = document.createElement('p');
        detail.className = 'wa-detail';
        detail.textContent = `${barberName} · ${d}/${m}/${y} · ${payload.time} hs`;

        const btn = document.createElement('a');
        btn.className = 'wa-btn';
        btn.href = appt.wa_link;
        btn.target = '_blank';
        btn.rel = 'noopener noreferrer';
        btn.textContent = 'Quiero el recordatorio por WhatsApp';

        const hint = document.createElement('p');
        hint.className = 'wa-hint';
        hint.textContent = 'Se abre WhatsApp con el mensaje listo: solo tocá enviar. Te confirmamos el turno y te lo recordamos 2 horas antes.';

        const back = document.createElement('button');
        back.type = 'button';
        back.className = 'wa-back';
        back.textContent = 'Reservar otro turno';
        back.addEventListener('click', () => {
            panel.remove();
            appointmentForm.hidden = false;
        });

        panel.append(title, detail, btn, hint, back);
        appointmentForm.hidden = true;
        appointmentForm.insertAdjacentElement('afterend', panel);
    }

    updateAvailableTimes();
});
