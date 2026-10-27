const API_URL = window.location.origin.includes('file://') 
    ? 'http://127.0.0.1:8000' 
    : window.location.origin;
let allAppointmentsCache = [];
let currentUser = null;
let currentWeekOffset = 0;
let workingDaysConfig = {
    my_working_days: [0, 1, 2, 3, 4, 5],
    team_working_days: [0, 1, 2, 3, 4, 5]
};

let shopPrices = {
    price_corte: 15000,
    price_corte_promo2x: 12000,
    price_corte_vip: 10000,
    price_barba: 10000,
    price_corte_barba: 18000
};

let currentMetricsPeriod = 'month';
let currentMetricsEmployeeId = '';
let metricsBarbersLoaded = false;

const CORTE_STEPS = [30, 35, 40, 45];
const BARBA_STEPS = [10, 15, 20];

function formatLocalDate(d) {
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

function formatARS(amount) {
    return '$' + Number(amount || 0).toLocaleString('es-AR');
}

function formatCompactARS(amount) {
    const n = Number(amount || 0);
    if (n === 0) return '$0';
    if (n >= 1000000) return '$' + (n / 1000000).toFixed(1).replace('.0', '') + 'M';
    if (n >= 1000) return '$' + Math.round(n / 1000) + 'k';
    return '$' + n;
}

document.addEventListener('DOMContentLoaded', async () => {
    populateDurationSelects(30, 15);

    document.getElementById('durCorte')?.addEventListener('change', updateCombinedDurationDisplay);
    document.getElementById('durBarba')?.addEventListener('change', updateCombinedDurationDisplay);

    document.getElementById('metricsBarberFilter')?.addEventListener('change', (e) => {
        currentMetricsEmployeeId = e.target.value;
        loadMetricsData();
    });

    const token = localStorage.getItem('access_token');
    if (token) {
        await initSession(token);
    }

    const excDateInput = document.getElementById('excDate');
    if (excDateInput) {
        const today = formatLocalDate(new Date());
        excDateInput.min = today;
        excDateInput.value = today;
    }

    document.getElementById('onlyTodayCheck')?.addEventListener('change', (e) => {
        if (e.target.checked) {
            currentWeekOffset = 0;
        }
        renderAgenda(allAppointmentsCache);
    });

    document.getElementById('onlyMineCheck')?.addEventListener('change', () => {
        renderAgenda(allAppointmentsCache);
    });
});

async function initSession(token) {
    const ok = await loadCurrentUser(token);
    if (!ok) return logout();
    await Promise.all([
        loadWorkingDaysConfig(token),
        loadServicePrices(token)
    ]);
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

        const userBadge = document.getElementById('loggedUserName');
        if (userBadge && currentUser.name) {
            userBadge.textContent = currentUser.name;
            userBadge.style.display = 'inline-flex';
        }

        const isOwner = String(currentUser.role || '').toLowerCase() === 'owner';
        const onlyMineControl = document.getElementById('onlyMineControl');
        const onlyMineCheck = document.getElementById('onlyMineCheck');

        if (onlyMineControl) {
            if (isOwner) {
                onlyMineControl.hidden = false;
                onlyMineControl.style.setProperty('display', 'inline-flex', 'important');
            } else {
                onlyMineControl.hidden = true;
                onlyMineControl.style.setProperty('display', 'none', 'important');
            }
        }
        if (onlyMineCheck) {
            onlyMineCheck.checked = false;
        }

        if (isOwner && !currentMetricsEmployeeId) {
            currentMetricsEmployeeId = String(currentUser.id);
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
            dayOfWeek: i
        });
    }
    return days;
}

function getActiveWorkingDays() {
    const isOwner = currentUser && String(currentUser.role || '').toLowerCase() === 'owner';
    const showAllShop = document.getElementById('onlyMineCheck')?.checked;
    return (isOwner && showAllShop)
        ? workingDaysConfig.team_working_days
        : workingDaysConfig.my_working_days;
}

function getMinWeekOffset(activeWorkingDays, onlyToday) {
    if (onlyToday) return 0;

    const todayStr = formatLocalDate(new Date());
    const currentWeekDays = getWeekDaysArray(0);

    const hasRemainingWorkingDays = currentWeekDays.some(
        d => d.dateStr >= todayStr && activeWorkingDays.includes(d.dayOfWeek)
    );

    return hasRemainingWorkingDays ? 0 : 1;
}

function changeWeek(delta) {
    const onlyTodayCheck = document.getElementById('onlyTodayCheck');
    const activeWorkingDays = getActiveWorkingDays();

    if (delta > 0 && onlyTodayCheck && onlyTodayCheck.checked) {
        onlyTodayCheck.checked = false;
    }

    const minOffset = getMinWeekOffset(activeWorkingDays, onlyTodayCheck?.checked);
    const nextOffset = currentWeekOffset + delta;

    if (nextOffset < minOffset) return;

    currentWeekOffset = nextOffset;
    renderAgenda(allAppointmentsCache);
}

function resetToCurrentWeek() {
    const onlyToday = document.getElementById('onlyTodayCheck')?.checked;
    const activeWorkingDays = getActiveWorkingDays();
    currentWeekOffset = getMinWeekOffset(activeWorkingDays, onlyToday);
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
    const showAllShop = document.getElementById('onlyMineCheck')?.checked;
    const todayStr = formatLocalDate(new Date());

    const isOwner = currentUser && String(currentUser.role || '').toLowerCase() === 'owner';
    const activeWorkingDays = getActiveWorkingDays();

    const minOffset = getMinWeekOffset(activeWorkingDays, onlyToday);
    if (currentWeekOffset < minOffset) {
        currentWeekOffset = minOffset;
    }

    const weekDays = getWeekDaysArray(currentWeekOffset);
    const mondayObj = weekDays[0].dateObj;
    const sundayObj = weekDays[6].dateObj;

    const formatShort = (d) => `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')}`;
    const subtitleEl = document.getElementById('weekSubtitle');
    if (subtitleEl) {
        subtitleEl.textContent = `SEMANA DEL ${formatShort(mondayObj)} AL ${formatShort(sundayObj)}`;
    }

    const prevBtn = document.getElementById('prevWeekBtn');
    if (prevBtn) prevBtn.disabled = (currentWeekOffset <= minOffset);

    const resetBtn = document.getElementById('resetWeekBtn');
    if (resetBtn) {
        resetBtn.style.display = (currentWeekOffset > minOffset) ? 'inline-block' : 'none';
    }

    const filteredAppts = (isOwner && !showAllShop && currentUser)
        ? appointments.filter(app => app.employee_id === currentUser.id)
        : appointments;

    const groupedByDate = {};
    filteredAppts.forEach(app => {
        if (!groupedByDate[app.date]) groupedByDate[app.date] = [];
        groupedByDate[app.date].push(app);
    });

    const diasSemanaNombres = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'];
    let visibleDaysCount = 0;

    weekDays.forEach(dayInfo => {
        const { dateObj, dateStr, dayOfWeek } = dayInfo;

        if (dateStr < todayStr) return;
        if (onlyToday && dateStr !== todayStr) return;
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
                const [h, m] = app.time.split(':').map(Number);
                const durationMin = app.duration || 30;
                const totalEndMinutes = (h * 60) + m + durationMin;
                const endH = Math.floor(totalEndMinutes / 60) % 24;
                const endM = totalEndMinutes % 60;
                const endTime = `${String(endH).padStart(2, '0')}:${String(endM).padStart(2, '0')}`;

                const barberBadge = (isOwner && app.employee_name)
                    ? `<span class="barber-tag">${app.employee_name}</span>`
                    : '';

                const currentPrice = app.price ?? shopPrices.price_corte;
                const isCorte = app.service === 'Corte';
                const isPromo2x = isCorte && currentPrice === shopPrices.price_corte_promo2x;
                const isVip = isCorte && currentPrice === shopPrices.price_corte_vip;
                const isNormal = !isPromo2x && !isVip;

                // Color y etiqueta del badge de precio según la tarifa activa
                let priceTierClass = '';
                let tierSuffix = '';
                if (isPromo2x) {
                    priceTierClass = 'price-tag-promo';
                    tierSuffix = ' · 2x';
                } else if (isVip) {
                    priceTierClass = 'price-tag-vip';
                    tierSuffix = ' · VIP';
                }

                // Si es "Corte", el precio es clickeable para desplegar las opciones
                const priceBadgeHtml = isCorte
                    ? `<button type="button" class="price-tag clickable ${priceTierClass}" onclick="toggleTariffMenu(${app.id})" title="Cambiar tarifa">
                           ${formatARS(currentPrice)}${tierSuffix} <span class="price-caret">▾</span>
                       </button>`
                    : `<span class="price-tag">${formatARS(currentPrice)}</span>`;

                // Menú oculto por defecto; se abre solo al tocar el precio
                const tariffButtonsHtml = isCorte ? `
                    <div id="tariff-pills-${app.id}" class="tariff-pills" style="display: none;">
                        <button type="button" class="tariff-btn ${isNormal ? 'active' : ''}"
                            onclick="cambiarTarifaTurno(${app.id}, 'normal')">
                            Normal ${formatCompactARS(shopPrices.price_corte)}
                        </button>
                        <button type="button" class="tariff-btn ${isPromo2x ? 'active-promo' : ''}"
                            onclick="cambiarTarifaTurno(${app.id}, 'promo2x')">
                            Promo 2x ${formatCompactARS(shopPrices.price_corte_promo2x)}
                        </button>
                        <button type="button" class="tariff-btn ${isVip ? 'active-vip' : ''}"
                            onclick="cambiarTarifaTurno(${app.id}, 'vip')">
                            VIP ${formatCompactARS(shopPrices.price_corte_vip)}
                        </button>
                    </div>
                ` : '';

                dayHtml += `
                    <div class="appointment-item">
                        <div class="appt-details">
                            <h3 class="appt-time">${app.time.substring(0, 5)} - ${endTime}</h3>
                            <div class="name">${app.client_name}</div>
                            <div class="service-meta">
                                <span class="service">${app.service}</span>
                                ${priceBadgeHtml}
                                ${barberBadge}
                            </div>
                            ${tariffButtonsHtml}
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
        container.innerHTML = '<p style="color:#64748b; text-align:center; padding: 20px 0;">No hay días laborables configurados para mostrar.</p>';
    }
}

function toggleTariffMenu(appointmentId) {
    const targetMenu = document.getElementById(`tariff-pills-${appointmentId}`);
    if (!targetMenu) return;

    const isHidden = targetMenu.style.display === 'none';

    // Cierra cualquier otro menú de tarifa que haya quedado abierto
    document.querySelectorAll('.tariff-pills').forEach(el => {
        el.style.display = 'none';
    });

    targetMenu.style.display = isHidden ? 'flex' : 'none';
}

async function cambiarTarifaTurno(appointmentId, tier) {
    const token = localStorage.getItem('access_token');
    if (!token) return;

    try {
        const res = await fetch(`${API_URL}/admin/appointments/${appointmentId}/price`, {
            method: 'PATCH',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({ tier })
        });

        if (res.ok) {
            const updatedAppt = await res.json();
            const idx = allAppointmentsCache.findIndex(a => a.id === appointmentId);
            if (idx !== -1) {
                allAppointmentsCache[idx].price = updatedAppt.price;
            }
            renderAgenda(allAppointmentsCache);
        } else {
            const err = await res.json();
            alert(err.detail || 'No se pudo actualizar la tarifa del turno.');
        }
    } catch (e) {
        alert('Error de conexión al actualizar el precio.');
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

function hideAllSections() {
    document.getElementById('loginSection').style.display = 'none';
    document.getElementById('dashboardSection').style.display = 'none';
    document.getElementById('servicesSection').style.display = 'none';
    document.getElementById('scheduleSection').style.display = 'none';
    document.getElementById('exceptionsSection').style.display = 'none';
    document.getElementById('metricsSection').style.display = 'none';
}

function showDashboard() {
    hideAllSections();
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
// VISTA MÉTRICAS Y FACTURACIÓN
// ==========================================

async function showMetrics() {
    hideAllSections();
    document.getElementById('metricsSection').style.display = 'block';

    const token = localStorage.getItem('access_token');
    if (!currentUser && token) {
        await loadCurrentUser(token);
    }

    const isOwner = currentUser && String(currentUser.role || '').toLowerCase() === 'owner';
    const barberSelect = document.getElementById('metricsBarberFilter');

    if (barberSelect) {
        barberSelect.style.display = isOwner ? 'inline-block' : 'none';
    }

    if (isOwner && !metricsBarbersLoaded) {
        loadBarbersForMetricsFilter();
    }

    await loadMetricsData();
}

function loadBarbersForMetricsFilter() {
    const barberSelect = document.getElementById('metricsBarberFilter');
    if (!barberSelect || !currentUser) return;

    barberSelect.innerHTML = '';

    // 1. Por defecto: únicamente las métricas propias del dueño (Maxi)
    const ownerOpt = document.createElement('option');
    ownerOpt.value = String(currentUser.id);
    ownerOpt.textContent = `💈 ${currentUser.name} (Mis Métricas)`;
    barberSelect.appendChild(ownerOpt);

    // 2. Opción global de todo el local (sin desglose individual de empleados)
    const allOpt = document.createElement('option');
    allOpt.value = 'all';
    allOpt.textContent = '🏢 Todo el Local';
    barberSelect.appendChild(allOpt);

    if (!currentMetricsEmployeeId) {
        currentMetricsEmployeeId = String(currentUser.id);
    }
    barberSelect.value = currentMetricsEmployeeId;
    metricsBarbersLoaded = true;
}

function changeMetricsPeriod(period) {
    currentMetricsPeriod = period;
    document.querySelectorAll('.period-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.period === period);
    });
    loadMetricsData();
}

async function loadMetricsData() {
    const token = localStorage.getItem('access_token');
    if (!token) return;

    const isOwner = currentUser && String(currentUser.role || '').toLowerCase() === 'owner';
    const params = new URLSearchParams({ period: currentMetricsPeriod });

    // Si es dueño y eligió sus propias métricas, envía su ID; si eligió 'all', pide Todo el Local
    if (isOwner && currentMetricsEmployeeId && currentMetricsEmployeeId !== 'all') {
        params.append('employee_id', currentMetricsEmployeeId);
    }

    try {
        const res = await fetch(`${API_URL}/admin/metrics?${params.toString()}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });

        if (res.status === 401) return logout();
        if (!res.ok) {
            console.error("Error obteniendo métricas:", res.status);
            return;
        }

        const data = await res.json();
        renderMetricsDashboard(data);
    } catch (err) {
        console.error("Error de conexión al cargar métricas:", err);
    }
}

function renderMetricsDashboard(data) {
    const formatDateDMY = (isoStr) => {
        const [y, m, d] = isoStr.split('-');
        return `${d}/${m}/${y}`;
    };

    const rangeEl = document.getElementById('metricsDateRange');
    if (rangeEl) {
        rangeEl.textContent = (data.start_date === data.end_date)
            ? `HOY (${formatDateDMY(data.start_date)})`
            : `DEL ${formatDateDMY(data.start_date)} AL ${formatDateDMY(data.end_date)}`;
    }

    document.getElementById('kpiTotalRevenue').textContent = formatARS(data.total_revenue);
    document.getElementById('kpiRealRevenue').textContent = `Cobrado: ${formatARS(data.real_revenue)}`;
    document.getElementById('kpiPendingRevenue').textContent = `En agenda: ${formatARS(data.pending_revenue)}`;

    document.getElementById('kpiTotalAppts').textContent = data.total_appointments;
    document.getElementById('kpiApptsDetail').textContent =
        `${data.completed_appointments} completados · ${data.pending_appointments} pendientes`;

    renderRevenueBarChart(data.chart_series || []);
    renderDonutCharts(data);
}

function renderRevenueBarChart(series) {
    const container = document.getElementById('revenueBarChart');
    if (!container) return;

    if (!series.length) {
        container.innerHTML = '<p class="empty-exceptions">Sin datos para graficar en este período.</p>';
        return;
    }

    const maxRevenue = Math.max(...series.map(item => item.total_revenue), 1);

    container.innerHTML = series.map(item => {
        const totalPct = item.total_revenue > 0
            ? Math.max(Math.round((item.total_revenue / maxRevenue) * 100), 8)
            : 0;
        const realSharePct = item.total_revenue > 0
            ? Math.round((item.real_revenue / item.total_revenue) * 100)
            : 0;
        const pendingSharePct = 100 - realSharePct;

        return `
            <div class="bar-col">
                <div class="bar-top-value">${formatCompactARS(item.total_revenue)}</div>
                <div class="bar-track-vertical">
                    <div class="bar-stack" style="height: ${totalPct}%;">
                        <div class="bar-segment-pending" style="height: ${pendingSharePct}%;" title="En agenda: ${formatARS(item.pending_revenue)}"></div>
                        <div class="bar-segment-real" style="height: ${realSharePct}%;" title="Cobrado: ${formatARS(item.real_revenue)}"></div>
                    </div>
                </div>
                <div class="bar-bottom-label">${item.label}</div>
                <div class="bar-bottom-sub">${item.appointments} t.</div>
            </div>
        `;
    }).join('');
}

function renderDonutCharts(data) {
    const container = document.getElementById('donutsContainer');
    if (!container) return;

    const revPct = data.total_revenue > 0
        ? Math.round((data.real_revenue / data.total_revenue) * 100)
        : 0;

    const apptPct = data.total_appointments > 0
        ? Math.round((data.completed_appointments / data.total_appointments) * 100)
        : 0;

    container.innerHTML = `
        <div class="donut-box">
            <svg viewBox="0 0 36 36" class="donut-svg">
                <path class="donut-ring"
                    d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                <path class="donut-segment-emerald"
                    stroke-dasharray="${revPct}, 100"
                    d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                <text x="18" y="20.5" class="donut-center-text">${revPct}%</text>
            </svg>
            <div class="donut-info">
                <span class="donut-title">Caja Cobrada</span>
                <span class="donut-sub">${formatARS(data.real_revenue)} de ${formatARS(data.total_revenue)}</span>
            </div>
        </div>

        <div class="donut-box">
            <svg viewBox="0 0 36 36" class="donut-svg">
                <path class="donut-ring"
                    d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                <path class="donut-segment-violet"
                    stroke-dasharray="${apptPct}, 100"
                    d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                <text x="18" y="20.5" class="donut-center-text">${apptPct}%</text>
            </svg>
            <div class="donut-info">
                <span class="donut-title">Turnos Completados</span>
                <span class="donut-sub">${data.completed_appointments} de ${data.total_appointments} turnos</span>
            </div>
        </div>
    `;
}

// ==========================================
// VISTA SERVICIOS (PRECIOS DUEÑO + DURACIONES DE CADA BARBERO)
// ==========================================

function updateCombinedDurationDisplay() {
    const corteVal = parseInt(document.getElementById('durCorte')?.value, 10) || 30;
    const barbaVal = parseInt(document.getElementById('durBarba')?.value, 10) || 15;
    const combinedEl = document.getElementById('durCorteBarba');

    if (combinedEl) {
        const total = corteVal + barbaVal;
        combinedEl.value = `${total} min`;
        combinedEl.dataset.minutes = total;
    }
}

function populateDurationSelects(corte = 30, barba = 15) {
    const buildOpts = (steps, selectedVal) => {
        const safeVal = steps.includes(Number(selectedVal)) ? Number(selectedVal) : steps[0];
        return steps.map(min =>
            `<option value="${min}" ${min === safeVal ? 'selected' : ''}>${min} min</option>`
        ).join('');
    };

    const corteEl = document.getElementById('durCorte');
    const barbaEl = document.getElementById('durBarba');

    if (corteEl) corteEl.innerHTML = buildOpts(CORTE_STEPS, corte);
    if (barbaEl) barbaEl.innerHTML = buildOpts(BARBA_STEPS, barba);

    updateCombinedDurationDisplay();
}

async function showServices() {
    hideAllSections();
    document.getElementById('servicesSection').style.display = 'block';

    const token = localStorage.getItem('access_token');
    if (!currentUser && token) {
        await loadCurrentUser(token);
    }

    const isOwner = currentUser && String(currentUser.role || '').toLowerCase() === 'owner';
    const ownerPricesCard = document.getElementById('ownerPricesCard');
    const empNavPills = document.getElementById('empServicesNavPills');

    if (ownerPricesCard) {
        ownerPricesCard.style.display = isOwner ? 'block' : 'none';
    }
    if (empNavPills) {
        empNavPills.style.display = isOwner ? 'none' : 'flex';
    }

    await loadServiceDurations(token);
    await loadServicePrices(token);
}

async function loadServiceDurations(token) {
    try {
        const res = await fetch(`${API_URL}/admin/service-durations`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.ok) {
            const data = await res.json();
            populateDurationSelects(data.duration_corte, data.duration_barba);
            return;
        }
    } catch (err) {
        console.error("Error al cargar duraciones de servicios:", err);
    }
    populateDurationSelects(
        currentUser?.duration_corte || 30,
        currentUser?.duration_barba || 15
    );
}

async function loadServicePrices(token) {
    try {
        const res = await fetch(`${API_URL}/admin/service-prices`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.ok) {
            const data = await res.json();
            shopPrices = {
                price_corte: data.price_corte ?? 15000,
                price_corte_promo2x: data.price_corte_promo2x ?? 12000,
                price_corte_vip: data.price_corte_vip ?? 10000,
                price_barba: data.price_barba ?? 10000,
                price_corte_barba: data.price_corte_barba ?? 18000
            };

            const elCorte = document.getElementById('priceCorte');
            const elPromo2x = document.getElementById('priceCortePromo2x');
            const elVip = document.getElementById('priceCorteVip');
            const elBarba = document.getElementById('priceBarba');
            const elCorteBarba = document.getElementById('priceCorteBarba');

            if (elCorte) elCorte.value = shopPrices.price_corte;
            if (elPromo2x) elPromo2x.value = shopPrices.price_corte_promo2x;
            if (elVip) elVip.value = shopPrices.price_corte_vip;
            if (elBarba) elBarba.value = shopPrices.price_barba;
            if (elCorteBarba) elCorteBarba.value = shopPrices.price_corte_barba;
        }
    } catch (err) {
        console.error("Error al cargar precios:", err);
    }
}

document.getElementById('pricesForm')?.addEventListener('submit', async function (e) {
    e.preventDefault();

    const payload = {
        price_corte: parseInt(document.getElementById('priceCorte').value, 10) || 0,
        price_corte_promo2x: parseInt(document.getElementById('priceCortePromo2x').value, 10) || 0,
        price_corte_vip: parseInt(document.getElementById('priceCorteVip').value, 10) || 0,
        price_barba: parseInt(document.getElementById('priceBarba').value, 10) || 0,
        price_corte_barba: parseInt(document.getElementById('priceCorteBarba').value, 10) || 0
    };

    const token = localStorage.getItem('access_token');

    try {
        const response = await fetch(`${API_URL}/admin/service-prices`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            const updated = await response.json();
            shopPrices = {
                price_corte: updated.price_corte,
                price_corte_promo2x: updated.price_corte_promo2x,
                price_corte_vip: updated.price_corte_vip,
                price_barba: updated.price_barba,
                price_corte_barba: updated.price_corte_barba
            };
            alert('Precios y promociones actualizados correctamente');
        } else {
            const error = await response.json();
            alert('Error al guardar precios: ' + (error.detail || JSON.stringify(error)));
        }
    } catch (err) {
        alert('Error de conexión con el servidor.');
    }
});

document.getElementById('durationsForm')?.addEventListener('submit', async function (e) {
    e.preventDefault();

    const corteMin = parseInt(document.getElementById('durCorte').value, 10);
    const barbaMin = parseInt(document.getElementById('durBarba').value, 10);

    const payload = {
        duration_corte: corteMin,
        duration_barba: barbaMin,
        duration_corte_barba: corteMin + barbaMin
    };

    const token = localStorage.getItem('access_token');

    try {
        const response = await fetch(`${API_URL}/admin/service-durations`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            const updated = await response.json();
            if (currentUser) {
                currentUser.duration_corte = updated.duration_corte;
                currentUser.duration_barba = updated.duration_barba;
                currentUser.duration_corte_barba = updated.duration_corte_barba;
            }
            updateCombinedDurationDisplay();
            alert('Duraciones guardadas correctamente');
        } else {
            const error = await response.json();
            alert('Error al guardar duraciones: ' + (error.detail || JSON.stringify(error)));
        }
    } catch (err) {
        alert('Error de conexión con el servidor.');
    }
});

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
    hideAllSections();
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
            await loadWorkingDaysConfig(token);
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
    hideAllSections();
    document.getElementById('exceptionsSection').style.display = 'block';

    if (!currentUser) {
        const token = localStorage.getItem('access_token');
        if (token) await loadCurrentUser(token);
    }

    const isOwner = currentUser && String(currentUser.role || '').toLowerCase() === 'owner';
    const globalWrapper = document.getElementById('globalExceptionWrapper');
    const globalCheck = document.getElementById('excGlobal');
    if (globalWrapper && globalCheck) {
        if (isOwner) {
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
    const isOwner = currentUser && String(currentUser.role || '').toLowerCase() === 'owner';
    const is_global = isOwner
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