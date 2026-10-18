const API_URL = 'http://127.0.0.1:8000';
let allAppointmentsCache = [];
let currentUser = null; // { id, name, username, role }
let currentWeekOffset = 0; // 0 = Esta semana, 1 = Próxima semana, etc.
let workingDaysConfig = {
    my_working_days: [0, 1, 2, 3, 4, 5],
    team_working_days: [0, 1, 2, 3, 4, 5]
};

// Formatea fecha local YYYY-MM-DD sin desfasaje UTC nocturno
function formatLocalDate(d) {
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

document.addEventListener('DOMContentLoaded', async () => {
    const token = localStorage.getItem('access_token');
    if (token) {
        await initSession(token);
    }

    // Inicializar fecha mínima hoy en el selector de excepciones
    const excDateInput = document.getElementById('excDate');
    if (excDateInput) {
        const today = formatLocalDate(new Date());
        excDateInput.min = today;
        excDateInput.value = today;
    }

    // Filtro "Solo Hoy": si se activa, vuelve a la semana actual automáticamente
    document.getElementById('onlyTodayCheck')?.addEventListener('change', (e) => {
        if (e.target.checked && currentWeekOffset !== 0) {
            currentWeekOffset = 0;
        }
        renderAgenda(allAppointmentsCache);
    });

    // Filtro "Mis Turnos" (Dueño)
    document.getElementById('onlyMineCheck')?.addEventListener('change', () => {
        renderAgenda(allAppointmentsCache);
    });
});

async function initSession(token) {
    const ok = await loadCurrentUser(token);
    if (!ok) return logout();
    await loadWorkingDaysConfig(token);
    showDashboard();
    await fetchAppointments(token);
}

async function loadCurrentUser(token) {
    try {
        const res = await fetch(`${API_URL}/admin/me`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) return false;

        currentUser = await res.json();

        const headerTitle = document.getElementById('headerTitle');
        if (headerTitle && currentUser.name) {
            headerTitle.textContent = currentUser.name.toUpperCase();
        }

        const onlyMineControl = document.getElementById('onlyMineControl');
        if (onlyMineControl) {
            onlyMineControl.style.display = (currentUser.role === 'owner') ? 'inline-flex' : 'none';
        }

        return true;
    } catch (e) {
        console.error("Error obteniendo perfil:", e);
        return false;
    }
}

async function loadWorkingDaysConfig(token) {
    try {
        const res = await fetch(`${API_URL}/admin/working-days`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.ok) {
            workingDaysConfig = await res.json();
        }
    } catch (e) {
        console.error("Error obteniendo días laborales:", e);
    }
}

// ==========================================
// NAVEGACIÓN DE SEMANAS (LUNES A DOMINGO)
// ==========================================

function getWeekDaysArray(weekOffset = 0) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());

    // Convertimos getDay() (0=Dom..6=Sáb) a formato (0=Lunes..6=Domingo)
    const currentDayIndex = (today.getDay() + 6) % 7;

    const monday = new Date(today);
    monday.setDate(today.getDate() - currentDayIndex + (weekOffset * 7));

    const days = [];
    for (let i = 0; i < 7; i++) {
        const d = new Date(monday);
        d.setDate(monday.getDate() + i);
        days.push({
            dateObj: d,
            dateStr: formatLocalDate(d),
            dayOfWeek: i // 0 = Lunes ... 6 = Domingo
        });
    }
    return days;
}

function changeWeek(delta) {
    const nextOffset = currentWeekOffset + delta;
    if (nextOffset < 0) return; // No permitir ir a semanas pasadas

    currentWeekOffset = nextOffset;

    // Si avanza de semana y tenía "Solo Hoy" marcado, lo destildamos
    const onlyTodayCheck = document.getElementById('onlyTodayCheck');
    if (currentWeekOffset > 0 && onlyTodayCheck && onlyTodayCheck.checked) {
        onlyTodayCheck.checked = false;
    }

    renderAgenda(allAppointmentsCache);
}

function resetToCurrentWeek() {
    currentWeekOffset = 0;
    renderAgenda(allAppointmentsCache);
}

// ==========================================
// LOGIN Y CARGA DE TURNOS
// ==========================================

document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new URLSearchParams();
    formData.append('username', document.getElementById('username').value);
    formData.append('password', document.getElementById('password').value);

    try {
        const response = await fetch(`${API_URL}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: formData
        });
        if (response.ok) {
            const data = await response.json();
            localStorage.setItem('access_token', data.access_token);
            await initSession(data.access_token);
        } else {
            alert('Credenciales incorrectas');
        }
    } catch (error) {
        alert('Error de servidor.');
    }
});

async function fetchAppointments(token) {
    try {
        const response = await fetch(`${API_URL}/admin/appointments`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!response.ok) return logout();
        const appointments = await response.json();
        allAppointmentsCache = appointments;
        renderAgenda(appointments);
    } catch (error) {
        document.getElementById('agendaContent').innerHTML = 'Error al cargar.';
    }
}

function renderAgenda(appointments) {
    const container = document.getElementById('agendaContent');
    container.innerHTML = '';

    const onlyToday = document.getElementById('onlyTodayCheck')?.checked;
    const onlyMine = document.getElementById('onlyMineCheck')?.checked;
    const todayStr = formatLocalDate(new Date());

    // 1. Obtener los 7 días (Lunes a Domingo) de la semana seleccionada
    const weekDays = getWeekDaysArray(currentWeekOffset);
    const mondayObj = weekDays[0].dateObj;
    const sundayObj = weekDays[6].dateObj;

    // 2. Actualizar subtítulo "SEMANA DEL DD/MM AL DD/MM" y estado de botones < >
    const formatShort = (d) => `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}`;
    const subtitleEl = document.getElementById('weekSubtitle');
    if (subtitleEl) {
        subtitleEl.textContent = `SEMANA DEL ${formatShort(mondayObj)} AL ${formatShort(sundayObj)}`;
    }

    const prevBtn = document.getElementById('prevWeekBtn');
    if (prevBtn) prevBtn.disabled = (currentWeekOffset === 0);

    const resetBtn = document.getElementById('resetWeekBtn');
    if (resetBtn) {
        resetBtn.style.display = (currentWeekOffset > 0) ? 'inline-block' : 'none';
    }

    // 3. Determinar qué días laborales corresponden a la vista actual:
    // - Si es empleado, o si es el dueño con "Mis Turnos" activo -> usa my_working_days
    // - Si es el dueño viendo a toda la barbería -> usa team_working_days
    const isOwner = currentUser && currentUser.role === 'owner';
    const activeWorkingDays = (isOwner && !onlyMine)
        ? workingDaysConfig.team_working_days
        : workingDaysConfig.my_working_days;

    // 4. Agrupar turnos por fecha (aplicando filtro "Mis Turnos" si corresponde)
    const filteredAppts = (onlyMine && currentUser)
        ? appointments.filter(app => app.employee_id === currentUser.id)
        : appointments;

    const groupedByDate = {};
    filteredAppts.forEach(app => {
        if (!groupedByDate[app.date]) groupedByDate[app.date] = [];
        groupedByDate[app.date].push(app);
    });

    const diasSemanaNombres = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'];
    let visibleDaysCount = 0;

    // 5. Recorrer la semana de Lunes (0) a Domingo (6)
    weekDays.forEach(dayInfo => {
        const { dateObj, dateStr, dayOfWeek } = dayInfo;

        // A. No mostrar días pasados (ej. si hoy es miércoles, ignora lunes y martes)
        if (dateStr < todayStr) return;

        // B. Si está tildado "Solo Hoy", mostrar únicamente hoy
        if (onlyToday && dateStr !== todayStr) return;

        // C. No mostrar los días que no atiende el barbero (salvo que "Solo Hoy" esté marcado)
        if (!onlyToday && !activeWorkingDays.includes(dayOfWeek)) return;

        visibleDaysCount++;
        const isToday = (dateStr === todayStr);
        const dayLabel = `${diasSemanaNombres[dayOfWeek]} ${formatShort(dateObj)}${isToday ? ' (Hoy)' : ''}`;
        const dayApps = groupedByDate[dateStr] || [];

        let dayHtml = `
        <div class="day-group">
            <div class="day-title ${isToday ? 'day-title-today' : ''}">${dayLabel}</div>
            <div class="day-appointments-box">
        `;

        if (dayApps.length === 0) {
            dayHtml += `
                <div class="empty-day-row">
                    Sin turnos reservados
                </div>
            `;
        } else {
            dayApps.forEach(app => {
                let [h, m] = app.time.split(':').map(Number);
                let endM = m + 45;
                let endH = h;
                if (endM >= 60) { endM -= 60; endH += 1; }
                let endTime = `${endH.toString().padStart(2, '0')}:${endM.toString().padStart(2, '0')}`;

                const barberBadge = (isOwner && app.employee_name)
                    ? `<span class="barber-tag">${app.employee_name}</span>`
                    : '';

                dayHtml += `
                    <div class="appointment-item">
                        <div class="appt-details">
                            <h3 class="appt-time">${app.time.substring(0, 5)} - ${endTime}</h3>
                            <div class="name">${app.client_name}</div>
                            <div class="service-meta">
                                <span class="service">${app.service}</span>
                                ${barberBadge}
                            </div>
                        </div>
                        <div class="appt-actions">
                            <button class="btn-wpp" onclick="abrirWpp('${app.client_phone}')">WhatsApp</button>
                            <button class="btn-cancel" onclick="cancelarTurno(${app.id})">Cancelar</button>
                        </div>
                    </div>
                `;
            });
        }

        dayHtml += `
            </div>
        </div>`;
        container.innerHTML += dayHtml;
    });

    if (visibleDaysCount === 0) {
        container.innerHTML = '<p style="color:#64748b; text-align:center; padding: 20px 0;">No quedan días laborables en esta semana. Usá la flecha (›) para ver la próxima semana.</p>';
    }
}

function abrirWpp(phone) {
    window.open(`https://wa.me/549${phone}`, '_blank');
}

async function cancelarTurno(id) {
    if (!confirm('¿Seguro que querés cancelar este turno? Esta acción no se puede deshacer.')) return;

    const token = localStorage.getItem('access_token');
    try {
        const response = await fetch(`${API_URL}/admin/appointments/${id}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (response.ok) {
            fetchAppointments(token);
        } else {
            alert('No se pudo cancelar el turno.');
        }
    } catch (err) {
        alert('Error de conexión.');
    }
}

function showDashboard() {
    document.getElementById('loginSection').style.display = 'none';
    document.getElementById('scheduleSection').style.display = 'none';
    document.getElementById('exceptionsSection').style.display = 'none';
    document.getElementById('dashboardSection').style.display = 'block';
    document.getElementById('logoutBtn').style.display = 'block';
    if (allAppointmentsCache.length >= 0 && currentUser) {
        renderAgenda(allAppointmentsCache);
    }
}

function logout() {
    localStorage.removeItem('access_token');
    currentUser = null;
    window.location.reload();
}

// ==========================================
// GESTIÓN DE HORARIO HABITUAL
// ==========================================

const dayNames = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"];

function generateTimeOptions() {
    let options = '<option value="">--:--</option>';
    for (let i = 7; i <= 23; i++) {
        const hour = i.toString().padStart(2, '0');
        options += `<option value="${hour}:00">${hour}:00</option>`;
        options += `<option value="${hour}:30">${hour}:30</option>`;
    }
    return options;
}

function renderScheduleForm() {
    const container = document.getElementById('daysContainer');
    container.innerHTML = '';
    const timeOpts = generateTimeOptions();

    dayNames.forEach((day, index) => {
        const row = document.createElement('div');
        row.className = 'schedule-day-item';
        row.id = `day-row-${index}`;

        row.innerHTML = `
            <div class="schedule-day-header">
                <label class="schedule-day-toggle" for="check-${index}">
                    <input type="checkbox" id="check-${index}" class="day-checkbox" data-day="${index}" checked>
                    <span class="day-name-text">${day}</span>
                </label>
                <span id="status-badge-${index}" class="day-status-badge active">Atiende</span>
            </div>

            <div class="schedule-shifts-wrapper" id="shifts-wrap-${index}">
                <div class="shift-line">
                    <span class="shift-tag">1° Turno</span>
                    <div class="shift-selects">
                        <select id="s1-start-${index}" class="time-input">${timeOpts}</select>
                        <span class="shift-sep">a</span>
                        <select id="s1-end-${index}" class="time-input">${timeOpts}</select>
                    </div>
                </div>

                <div class="shift-line">
                    <span class="shift-tag">2° Turno</span>
                    <div class="shift-selects">
                        <select id="s2-start-${index}" class="time-input">${timeOpts}</select>
                        <span class="shift-sep">a</span>
                        <select id="s2-end-${index}" class="time-input">${timeOpts}</select>
                    </div>
                </div>
            </div>
        `;
        container.appendChild(row);
    });

    document.querySelectorAll('.day-checkbox').forEach(checkbox => {
        checkbox.addEventListener('change', function () {
            const idx = this.getAttribute('data-day');
            const row = document.getElementById(`day-row-${idx}`);
            const badge = document.getElementById(`status-badge-${idx}`);
            const timeInputs = row.querySelectorAll('.time-input');

            if (this.checked) {
                row.classList.remove('day-off');
                badge.textContent = 'Atiende';
                badge.classList.add('active');
                timeInputs.forEach(input => input.disabled = false);
            } else {
                row.classList.add('day-off');
                badge.textContent = 'Franco';
                badge.classList.remove('active');
                timeInputs.forEach(input => {
                    input.disabled = true;
                    input.value = '';
                });
            }
        });
    });
}

async function showSchedule() {
    document.getElementById('dashboardSection').style.display = 'none';
    document.getElementById('exceptionsSection').style.display = 'none';
    document.getElementById('scheduleSection').style.display = 'block';

    renderScheduleForm();

    const token = localStorage.getItem('access_token');

    try {
        const response = await fetch(`${API_URL}/admin/schedule`, {
            method: 'GET',
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.ok) {
            const horariosGuardados = await response.json();

            horariosGuardados.forEach(dia => {
                const i = dia.day_of_week;
                const checkbox = document.getElementById(`check-${i}`);
                if (!checkbox) return;

                checkbox.checked = dia.is_working;
                checkbox.dispatchEvent(new Event('change'));

                const formatTime = (timeStr) => timeStr ? timeStr.substring(0, 5) : '';

                if (dia.is_working) {
                    document.getElementById(`s1-start-${i}`).value = formatTime(dia.shift1_start);
                    document.getElementById(`s1-end-${i}`).value = formatTime(dia.shift1_end);
                    document.getElementById(`s2-start-${i}`).value = formatTime(dia.shift2_start);
                    document.getElementById(`s2-end-${i}`).value = formatTime(dia.shift2_end);
                }
            });
        }
    } catch (err) {
        console.error("Error al cargar los horarios:", err);
    }
}

document.getElementById('scheduleForm').addEventListener('submit', async function (e) {
    e.preventDefault();

    const scheduleData = [];

    for (let i = 0; i < 7; i++) {
        const isWorking = document.getElementById(`check-${i}`).checked;
        const getTime = (id) => document.getElementById(id).value || null;

        const s1_start = getTime(`s1-start-${i}`);
        const s1_end = getTime(`s1-end-${i}`);

        scheduleData.push({
            day_of_week: i,
            is_working: isWorking,
            shift1_start: isWorking ? s1_start : null,
            shift1_end: isWorking ? s1_end : null,
            shift2_start: isWorking ? getTime(`s2-start-${i}`) : null,
            shift2_end: isWorking ? getTime(`s2-end-${i}`) : null,
        });
    }

    const payload = { schedule: scheduleData };
    const token = localStorage.getItem('access_token');

    try {
        const response = await fetch(`${API_URL}/admin/schedule`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            await loadWorkingDaysConfig(token); // Refrescamos los días activos en vivo
            alert('Horarios guardados correctamente');
        } else {
            const error = await response.json();
            alert('Error al guardar: ' + JSON.stringify(error));
        }
    } catch (err) {
        alert('Error de conexión con el servidor.');
    }
});

// ==========================================
// GESTIÓN DE FERIADOS Y EXCEPCIONES
// ==========================================

async function showExceptions() {
    document.getElementById('dashboardSection').style.display = 'none';
    document.getElementById('scheduleSection').style.display = 'none';
    document.getElementById('exceptionsSection').style.display = 'block';

    if (!currentUser) {
        const token = localStorage.getItem('access_token');
        if (token) await loadCurrentUser(token);
    }

    const globalWrapper = document.getElementById('globalExceptionWrapper');
    const globalCheck = document.getElementById('excGlobal');
    if (globalWrapper && globalCheck) {
        if (currentUser && currentUser.role === 'owner') {
            globalWrapper.style.display = 'block';
        } else {
            globalWrapper.style.display = 'none';
            globalCheck.checked = false;
        }
    }

    loadExceptions();
}

async function loadExceptions() {
    const token = localStorage.getItem('access_token');
    const listContainer = document.getElementById('exceptionsList');
    listContainer.innerHTML = '<p class="empty-exceptions">Cargando...</p>';

    try {
        const response = await fetch(`${API_URL}/admin/exceptions`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (response.status === 401) return logout();
        if (!response.ok) throw new Error('Error al obtener excepciones');

        const exceptions = await response.json();

        if (exceptions.length === 0) {
            listContainer.innerHTML = '<p class="empty-exceptions">No hay excepciones programadas.</p>';
            return;
        }

        listContainer.innerHTML = exceptions.map(exc => {
            const [year, month, day] = exc.date.split('-');
            const formattedDate = `${day}/${month}/${year}`;
            const timeRange = (exc.start_time && exc.end_time)
                ? `${exc.start_time.substring(0, 5)} a ${exc.end_time.substring(0, 5)} hs`
                : 'Día completo';

            return `
                <div class="exception-item">
                    <div class="exception-details">
                        <div class="exception-main-text">
                            <strong>${formattedDate}</strong>
                        </div>
                        <div class="exception-sub-text">
                            <span class="exc-badge">${timeRange}</span>
                            <span>• ${exc.employee_name}</span>
                        </div>
                    </div>
                    <button type="button" class="btn-delete-exc" onclick="deleteException(${exc.id})">
                        Eliminar
                    </button>
                </div>
            `;
        }).join('');
    } catch (err) {
        listContainer.innerHTML = '<p class="empty-exceptions">Error al cargar las excepciones.</p>';
    }
}

document.getElementById('exceptionForm').addEventListener('submit', async function (e) {
    e.preventDefault();

    const date = document.getElementById('excDate').value;
    const start_time = document.getElementById('excStart').value || null;
    const end_time = document.getElementById('excEnd').value || null;
    const is_global = (currentUser && currentUser.role === 'owner')
        ? (document.getElementById('excGlobal')?.checked || false)
        : false;

    if ((start_time && !end_time) || (!start_time && end_time)) {
        alert("Para bloquear una franja horaria parcial, completá tanto 'Desde Hora' como 'Hasta Hora'. Si querés bloquear el día entero, dejá ambos vacíos.");
        return;
    }

    if (start_time && end_time && start_time >= end_time) {
        alert("La hora 'Desde' debe ser anterior a la hora 'Hasta'.");
        return;
    }

    const token = localStorage.getItem('access_token');

    try {
        const response = await fetch(`${API_URL}/admin/exceptions`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({ date, start_time, end_time, is_global })
        });

        const data = await response.json();

        if (!response.ok) {
            alert(data.detail || 'Error al guardar la excepción.');
            return;
        }

        document.getElementById('excStart').value = '';
        document.getElementById('excEnd').value = '';
        if (document.getElementById('excGlobal')) {
            document.getElementById('excGlobal').checked = false;
        }

        if (data.affected_appointments > 0) {
            alert(`Excepción registrada. Atención: ya tenés ${data.affected_appointments} turno(s) confirmado(s) en ese horario. Revisá tu agenda para avisarles por WhatsApp.`);
        }

        loadExceptions();
    } catch (err) {
        alert('Error de conexión con el servidor.');
    }
});

async function deleteException(id) {
    if (!confirm('¿Querés eliminar este bloqueo de horario?')) return;

    const token = localStorage.getItem('access_token');
    try {
        const response = await fetch(`${API_URL}/admin/exceptions/${id}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${token}` }
        });

        const data = await response.json();
        if (!response.ok) {
            alert(data.detail || 'No tenés permiso para eliminar este bloqueo.');
            return;
        }

        loadExceptions();
    } catch (err) {
        alert('Error de conexión con el servidor.');
    }
}