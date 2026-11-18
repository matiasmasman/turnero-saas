import secrets

from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.core.login_limiter import RATE_LIMITED_DETAIL, SCOPE_IP, LoginLimiter
from app.core.request_info import client_ip, log_extra
from app.core.security import verify_password, create_access_token, get_password_hash
from app.core.telemetry import sec_logger

router = APIRouter(prefix="/auth", tags=["Autenticación"])

BAD_CREDENTIALS = "Usuario o contraseña incorrectos"
LOG_USERNAME_MAX = 100          # lo que se guarda en el log del usuario intentado (nunca la contraseña)

# Hash de una clave al azar que nadie conoce: con un usuario que NO existe se revisa la contraseña contra
# este hash, así tarda lo mismo que con un usuario real y la respuesta no deja adivinar qué usuarios existen.
_dummy_hash = None


def _dummy_password_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = get_password_hash(secrets.token_urlsafe(32))
    return _dummy_hash


def _limiter(request: Request) -> LoginLimiter:
    limiter = getattr(request.app.state, "login_limiter", None)
    if limiter is None:                        # por si la app se armó sin app/main.py
        limiter = request.app.state.login_limiter = LoginLimiter()
    return limiter


def _too_many() -> HTTPException:
    return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=RATE_LIMITED_DETAIL)


@router.post("/login", response_model=dict)
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    ip = client_ip(request)
    limiter = _limiter(request)
    attempted = (form_data.username or "")[:LOG_USERNAME_MAX]

    # 1) Bloqueado: 429 sin mirar la base ni la contraseña, y sin un evento por cada intento.
    if limiter.is_blocked(form_data.username, ip):
        raise _too_many()

    user = db.query(User).filter(User.username == form_data.username).first()
    # 2) Siempre se revisa una contraseña (la real o la de mentira): mismo tiempo exista o no el usuario.
    password_ok = verify_password(form_data.password, user.hashed_password if user else _dummy_password_hash())

    if not user or not password_ok:
        sec_logger.warning(
            f"Fallo de login para usuario: {attempted}",
            extra=log_extra(request, None, "AUTH_FAILED", {"username_attempted": attempted}),
        )
        started = limiter.record_failure(form_data.username, ip)
        for scope, failures in started:
            sec_logger.warning(
                "Login bloqueado 15 minutos por demasiados intentos fallidos "
                + ("desde esta IP" if scope == SCOPE_IP else f"para el usuario {attempted} desde esta IP"),
                extra=log_extra(request, None, "AUTH_RATE_LIMITED", {
                    "scope": scope,                         # "user_ip" o "ip"
                    "username_attempted": attempted if scope != SCOPE_IP else None,
                    "failures": failures, "window_minutes": 15, "block_minutes": 15,
                }),
            )
        if started:
            raise _too_many()                    # el intento que hace saltar el bloqueo ya avisa
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=BAD_CREDENTIALS,
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Contraseña correcta: se olvidan los fallidos de este usuario desde esta IP.
    limiter.record_success(form_data.username, ip)

    if not user.is_active:
        sec_logger.warning(
            f"Intento de login en cuenta inactiva: {user.username}",
            extra=log_extra(request, user.id, "AUTH_BLOCKED_INACTIVE", {"username": user.username}),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta cuenta se encuentra desactivada.",
        )

    access_token = create_access_token(data={"sub": str(user.id), "role": user.role.value})

    sec_logger.info(
        f"Login exitoso para usuario: {user.id}",
        extra=log_extra(request, user.id, "AUTH_SUCCESS", {"role": user.role.value}),
    )

    return {"access_token": access_token, "token_type": "bearer"}
