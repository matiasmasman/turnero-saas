# 💈 Men's Club — SaaS Appointment System (v2.0.1 - Blue Team Edition)

Sistema web de gestión de reservas y turnos **White-Label** en producción, diseñado bajo principios de **Seguridad por Diseño (Security by Design)**, control de acceso basado en roles (**RBAC**) y **telemetría estructurada en JSON** integrada en tiempo real con **Wazuh SIEM/XDR**.

🔗 **Producción (Clientes):** [https://mensclubturnos.duckdns.org](https://mensclubturnos.duckdns.org)  
🔐 **Panel Staff (Barberos/Admin):** [https://mensclubturnos.duckdns.org/panel.html](https://mensclubturnos.duckdns.org/panel.html)

---

## 🏗️ Arquitectura y Stack Tecnológico

* **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.1, Pydantic v2.
* **Base de Datos:** PostgreSQL 16 en producción (con soporte de fallback a SQLite para desarrollo local).
* **Autenticación y Criptografía:** JWT (`python-jose`) + Hashing de contraseñas con `bcrypt` (`passlib`).
* **Frontend:** HTML5, CSS3 (Dark Mode Esmeralda `#10b981`) y Vanilla JS sin dependencias de CDNs externos para cumplir con políticas estrictas de **Content Security Policy (CSP)**.
* **Infraestructura y Hardening (VPS Ubuntu 24.04 LTS):**
  * **Reverse Proxy & SSL/TLS:** Nginx con certificados **Let's Encrypt (Certbot)** y redirección forzada a HTTPS.
  * **Servicio de Aplicación:** `systemd` (`barberia.service`) corriendo Uvicorn sobre loopback (`127.0.0.1:8000`) aislado del exterior.
  * **Hardening de Red:** Firewall UFW activo, SSH securizado en puerto no estándar con autenticación exclusiva por llave criptográfica `ed25519` y red privada de administración vía **Tailscale**.
  * **Monitoreo SOC / SIEM:** Agente **Wazuh** ingiriendo eventos de seguridad de la aplicación en tiempo real desde `/home/matias/turnero-saas/logs/security.log`.

---

## 🛡️ Características de Seguridad (Blue Team Focus)

1. **Control de Acceso Basado en Roles (RBAC) y Anti-IDOR:**
   * Separación estricta entre roles `OWNER` (gestión global de agenda, precios de servicios y métricas de facturación) y `EMPLOYEE` (gestión exclusiva de su propia agenda, horarios y bloqueos).
   * Validación a nivel de endpoint para impedir que un empleado modifique o elimine recursos de otro profesional (*Insecure Direct Object References*).
2. **Protección Anti-DoS y Validación Estricta:**
   * Cálculo dinámico de bloques horarios (45 min) en backend evitando colisiones de concurrencia, inyecciones SQL y payloads malformados.
3. **Gestión de Identidades por CLI (`manage_staff.py`):**
   * Sin endpoints públicos de registro (`/register` deshabilitado). El alta, baja lógica (`toggle`) y rotación de credenciales (`passwd`) del personal se realiza exclusivamente por consola administrativa segura.
4. **Cabeceras de Seguridad HTTP:**
   * Middleware propio aplicando `Content-Security-Policy`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` y trazabilidad mediante `X-Request-ID` (UUIDv4).

---

## 📊 Telemetría JSON e Integración con Wazuh SIEM

El motor de telemetría (`app/core/telemetry.py`) emite registros estructurados en formato JSON (tanto a `stdout` para `journalctl` como a `logs/security.log` para ingesta directa en Wazuh), correlacionando IP real del cliente (`X-Real-IP` / `X-Forwarded-For`), `request_id`, `actor_id` y latencia en milisegundos.

### 1. Auditoría de Autenticación y Eventos de Negocio
Ejemplo real de trazabilidad durante el inicio de sesión y consulta de agenda:

```json
{"timestamp": "2026-10-27T20:53:55.394284+00:00", "level": "INFO", "event_type": "AUTH_SUCCESS", "request_id": "6bb7ec31-df44-4483-8250-5dd055bca3e2", "ip_address": "190.15.XXX.XXX", "actor_id": 1, "message": "Login exitoso para usuario: 1", "metadata": {"role": "owner"}}
{"timestamp": "2026-10-27T20:53:55.537403+00:00", "level": "INFO", "event_type": "AGENDA_VIEWED", "request_id": null, "ip_address": "UNKNOWN", "actor_id": 1, "message": "Agenda consultada por maxi", "metadata": {"role": "owner"}}
```

### 2. Detección de Escaneos Automatizados en Producción (Threat Hunting)
Eventos reales capturados en `logs/security.log` frente a un barrido de reconocimiento automatizado (*Directory Bruteforcing* buscando archivos `.env` y repositorios `.git` expuestos), mitigados y registrados en ~1 ms:

```json
{"timestamp": "2026-10-28T01:13:58.176668+00:00", "level": "INFO", "event_type": "HTTP_ACCESS", "request_id": "e9e3c2f8-d538-4416-8f53-de752b0a1dc7", "ip_address": "45.138.12.51", "actor_id": null, "message": "Access to /.env.dev.local", "metadata": {"method": "GET", "status_code": 404, "duration_ms": 1.43}}
{"timestamp": "2026-10-28T01:13:59.184361+00:00", "level": "INFO", "event_type": "HTTP_ACCESS", "request_id": "31fb68db-4073-4248-8024-d683a723fee2", "ip_address": "45.138.12.51", "actor_id": null, "message": "Access to /.env.secret", "metadata": {"method": "GET", "status_code": 404, "duration_ms": 1.05}}
{"timestamp": "2026-10-28T01:14:03.640070+00:00", "level": "INFO", "event_type": "HTTP_ACCESS", "request_id": "56cd2b1c-eb9e-4211-8587-e7fde26aa868", "ip_address": "45.138.12.51", "actor_id": null, "message": "Access to /files/.git/config", "metadata": {"method": "GET", "status_code": 404, "duration_ms": 0.88}}
```

---

## 🛠️ Administración del Staff vía CLI (`manage_staff.py`)

El sistema incluye una herramienta CLI administrativa para operar usuarios sin exponer rutas sensibles en la API:

```bash
# Inicializar base de datos y staff por defecto
python manage_staff.py init

# Listar profesionales, roles y estado (Activo/Inactivo)
python manage_staff.py list

# Agregar un nuevo profesional
python manage_staff.py add --username nuevo_barbero --name "NombreVisible" --password "ClaveSegura123!" --role employee

# Cambiar contraseña de un usuario en producción
python manage_staff.py passwd --username maxi --password "NuevaClaveSegura!"

# Activar / Desactivar un profesional de la agenda pública
python manage_staff.py toggle --username leo
```

---

## 📂 Estructura del Proyecto

```text
turnero-saas/
├── app/
│   ├── core/
│   │   ├── middleware.py      # Cabeceras CSP, Request-ID y captura de IP real
│   │   ├── security.py        # Hashing bcrypt y generación/validación JWT
│   │   └── telemetry.py       # Logger JSON dual (stdout + logs/security.log)
│   ├── routers/
│   │   ├── admin.py           # Endpoints protegidos con RBAC (Owner/Employee)
│   │   ├── appointments.py    # Lógica de disponibilidad (bloques de 45 min) y reservas
│   │   └── auth.py            # Login y emisión de tokens JWT
│   ├── database.py            # Conexión SQLAlchemy (PostgreSQL / SQLite fallback)
│   ├── dependencies.py        # Inyección de dependencias y validación de roles
│   ├── main.py                # Punto de entrada FastAPI y montaje de estáticos
│   ├── models.py              # Modelos relacionales (Users, Appointments, Schedules, Services)
│   └── schemas.py             # Validaciones estrictas con Pydantic v2
├── frontend/                  # Interfaz pública (index.html) y Panel Admin (panel.html)
├── logs/                      # Directorio de salida para Wazuh Agent (security.log)
├── manage_staff.py            # Herramienta CLI de gestión de identidades
└── requirements.txt           # Dependencias fijadas del entorno
```
