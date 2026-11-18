"""
Límite de intentos de login (Bloque 3, #2). En MEMORIA del proceso.

IMPORTANTE: funciona porque en producción hay UN SOLO worker de uvicorn. Con 2 o más workers (o 2
servidores) cada uno tendría su propio contador y el límite real se multiplicaría por la cantidad de
workers; en ese caso hay que pasar esto a algo compartido (Redis, la base, o limit_req de Nginx).
Si uvicorn se reinicia (deploy, caída), los contadores empiezan de cero: es aceptable.

Reglas (las dos ventanas son de 15 minutos y los bloqueos también):
- 5 fallidos del MISMO usuario (normalizado: sin espacios a los lados y en minúsculas) desde la MISMA IP
  → ese usuario queda bloqueado desde esa IP. Desde otra IP, ese usuario puede seguir entrando.
- 20 fallidos desde la MISMA IP, con cualquier usuario (exista o no) → esa IP queda bloqueada para todos.
- Mientras hay bloqueo: 429 y la contraseña NO se revisa (los intentos bloqueados no suman ni se registran
  uno por uno en el log: solo el evento AUTH_RATE_LIMITED cuando EMPIEZA el bloqueo).
- Un login correcto borra el contador de ese usuario+IP (no el de la IP).

La memoria tiene tope: se limpia lo vencido y, si aun así hay más de MAX_KEYS claves, se descartan las más
viejas. Un atacante que llena la tabla con usuarios inventados desde muchas IPs, como mucho hace que se
olviden contadores viejos; no puede hacer crecer la memoria sin límite.
"""
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Deque, List, Optional, Tuple

WINDOW_SECONDS = 15 * 60
BLOCK_SECONDS = 15 * 60
MAX_FAILS_USER_IP = 5
MAX_FAILS_IP = 20
MAX_KEYS = 10_000                 # por tabla (usuario+IP y solo IP)
MAX_USERNAME_KEY = 150            # se recorta el usuario para la clave (un "usuario" de 1 MB no ocupa 1 MB)
PURGE_EVERY_SECONDS = 60

RATE_LIMITED_DETAIL = "Demasiados intentos. Probá de nuevo en unos minutos."

SCOPE_USER_IP = "user_ip"
SCOPE_IP = "ip"


def normalize_username(username: Optional[str]) -> str:
    return (username or "").strip().casefold()[:MAX_USERNAME_KEY]


class _Entry:
    __slots__ = ("fails", "blocked_until")

    def __init__(self):
        self.fails: Deque[float] = deque()
        self.blocked_until = 0.0


class _Table:
    """Contadores de fallidos por clave, con ventana deslizante y bloqueo."""

    def __init__(self, max_fails: int, max_keys: int):
        self.max_fails = max_fails
        self.max_keys = max_keys
        self.entries: "OrderedDict[object, _Entry]" = OrderedDict()

    def _drop_old_fails(self, entry: _Entry, now: float) -> None:
        while entry.fails and entry.fails[0] <= now - WINDOW_SECONDS:
            entry.fails.popleft()

    def is_blocked(self, key, now: float) -> bool:
        entry = self.entries.get(key)
        return entry is not None and entry.blocked_until > now

    def add_fail(self, key, now: float) -> Optional[int]:
        """Suma un fallido. Si con este se llega al máximo, empieza el bloqueo y devuelve cuántos hubo."""
        entry = self.entries.get(key)
        if entry is None:
            entry = self.entries[key] = _Entry()
        else:
            self.entries.move_to_end(key)          # la más usada queda al final (lo viejo se descarta primero)
        self._drop_old_fails(entry, now)
        entry.fails.append(now)
        if len(entry.fails) >= self.max_fails:
            count = len(entry.fails)
            entry.blocked_until = now + BLOCK_SECONDS
            entry.fails.clear()                    # cuando venza el bloqueo, empieza de cero
            self._cap(now)
            return count
        self._cap(now)
        return None

    def clear(self, key) -> None:
        self.entries.pop(key, None)

    def purge(self, now: float) -> None:
        for key in list(self.entries):
            entry = self.entries[key]
            self._drop_old_fails(entry, now)
            if not entry.fails and entry.blocked_until <= now:
                del self.entries[key]

    def _cap(self, now: float) -> None:
        if len(self.entries) <= self.max_keys:
            return
        self.purge(now)                            # primero, lo vencido
        while len(self.entries) > self.max_keys:   # si no alcanza, lo menos usado últimamente
            self.entries.popitem(last=False)


class LoginLimiter:
    """Uno por proceso (lo crea app/main.py en app.state.login_limiter). Seguro con varios hilos: FastAPI
    corre los endpoints comunes (def) en un grupo de hilos, así que todo pasa con un candado."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = MAX_KEYS):
        self._clock = clock
        self._lock = threading.Lock()
        self._user_ip = _Table(MAX_FAILS_USER_IP, max_keys)
        self._ip = _Table(MAX_FAILS_IP, max_keys)
        self._last_purge = clock()

    def _maybe_purge(self, now: float) -> None:
        if now - self._last_purge >= PURGE_EVERY_SECONDS:
            self._user_ip.purge(now)
            self._ip.purge(now)
            self._last_purge = now

    def is_blocked(self, username: str, ip: str) -> bool:
        key = (normalize_username(username), ip)
        with self._lock:
            now = self._clock()
            self._maybe_purge(now)
            return self._ip.is_blocked(ip, now) or self._user_ip.is_blocked(key, now)

    def record_failure(self, username: str, ip: str) -> List[Tuple[str, int]]:
        """Anota un fallido. Devuelve los bloqueos que EMPIEZAN con este intento: [(scope, fallidos)]."""
        key = (normalize_username(username), ip)
        started = []
        with self._lock:
            now = self._clock()
            self._maybe_purge(now)
            n = self._user_ip.add_fail(key, now)
            if n is not None:
                started.append((SCOPE_USER_IP, n))
            n = self._ip.add_fail(ip, now)
            if n is not None:
                started.append((SCOPE_IP, n))
        return started

    def record_success(self, username: str, ip: str) -> None:
        with self._lock:
            self._user_ip.clear((normalize_username(username), ip))

    def sizes(self) -> Tuple[int, int]:
        """(claves usuario+IP, claves IP): para las pruebas del tope de memoria."""
        with self._lock:
            return len(self._user_ip.entries), len(self._ip.entries)
