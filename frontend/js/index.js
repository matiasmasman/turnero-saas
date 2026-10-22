const API_URL = window.location.origin.includes('file://') 
    ? 'http://127.0.0.1:8000' 
    : window.location.origin;

document.addEventListener('DOMContentLoaded', async () => {
    const barberSelect = document.getElementById('employee_id');
    const dateInput = document.getElementById('appointmentDate');
    const timeSelect = document.getElementById('timeSelect');

    // 1. Configuramos la fecha mínima como HOY
    const todayStr = new Date().toISOString().split('T')[0];
    if (dateInput) {
        dateInput.min = todayStr;
        dateInput.value = todayStr;
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
        } else {
            console.error("El backend falló al devolver los barberos.");
        }
    } catch (error) {
        console.error("Error de conexión al obtener barberos:", error);
    }

    // 3. Función para actualizar horarios
    async function updateAvailableTimes() {
        const barberId = barberSelect.value;
        const selectedDate = dateInput.value;

        if (!selectedDate) {
            timeSelect.innerHTML = '<option value="">Primero elige una fecha</option>';
            timeSelect.disabled = true;
            return;
        }

        try {
            timeSelect.innerHTML = '<option value="">Cargando horarios...</option>';
            timeSelect.disabled = true;

            let url = API_URL + '/appointments/available-slots?target_date=' + selectedDate;
            if (barberId) {
                url += `&employee_id=${barberId}`;
            }

            const response = await fetch(url);

            if (response.ok) {
                const slots = await response.json();
                timeSelect.innerHTML = '';

                if (slots.length === 0) {
                    timeSelect.innerHTML = '<option value="">Día no laborable o completo</option>';
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

    // 4. Escuchadores de eventos
    if (barberSelect) barberSelect.addEventListener('change', updateAvailableTimes);
    if (dateInput) dateInput.addEventListener('change', updateAvailableTimes);

    // 5. Enviar los datos al backend para Confirmar el Turno
    const appointmentForm = document.getElementById('appointmentForm');

    if (appointmentForm) {
        appointmentForm.addEventListener('submit', async (e) => {
            e.preventDefault(); // Evitamos que la página se recargue al enviar el formulario

            // Cambiamos el texto del botón para que el usuario sepa que está cargando
            const submitBtn = appointmentForm.querySelector('button[type="submit"]');
            const originalText = submitBtn.textContent;
            submitBtn.textContent = 'Procesando...';
            submitBtn.disabled = true;

            // Recolectamos los datos y los adaptamos al formato que espera FastAPI
            const rawEmployeeId = document.getElementById('employee_id').value;

            const payload = {
                client_name: document.getElementById('client_name').value,
                client_phone: document.getElementById('client_phone').value,
                service: document.getElementById('service').value,
                date: document.getElementById('appointmentDate').value,
                time: document.getElementById('timeSelect').value,
                // Si eligió "Cualquiera", mandamos null. Si eligió a alguien, lo convertimos a número entero.
                employee_id: rawEmployeeId ? parseInt(rawEmployeeId) : null
            };

            try {
                // Hacemos la petición POST a nuestra API
                const response = await fetch(API_URL + '/appointments/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(payload)
                });

                if (response.ok) {
                    // Turno creado exitosamente
                    alert(`¡Turno confirmado con éxito!\nTe esperamos el ${payload.date} a las ${payload.time}.`);

                    // Limpiamos el formulario y reseteamos la fecha a hoy
                    appointmentForm.reset();
                    document.getElementById('appointmentDate').value = todayStr;
                    updateAvailableTimes(); // Volvemos a calcular los horarios libres
                } else {
                    // Si el backend rechaza el turno (ej: alguien más lo sacó un segundo antes)
                    const error = await response.json();
                    alert(`No se pudo reservar: ${error.detail || 'Horario no disponible.'}`);
                    updateAvailableTimes(); // Actualizamos la lista por las dudas
                }
            } catch (error) {
                console.error("Error al confirmar el turno:", error);
                alert("Error de conexión con el servidor. Intentá nuevamente.");
            } finally {
                // Restauramos el botón a la normalidad
                submitBtn.textContent = originalText;
                submitBtn.disabled = false;
            }
        });
    }

    // Carga inicial
    updateAvailableTimes();
});