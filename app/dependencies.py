from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, UserRole
from app.core.request_info import log_extra
from app.core.security import decode_access_token
from app.core.telemetry import sec_logger

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

def get_current_user(request: Request, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)   # exige exp y sub, algoritmo fijo, máximo 24 h
        user_id = int(payload.get("sub"))
    except (JWTError, TypeError, ValueError):
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception

    if not user.is_active:
        sec_logger.warning(
            f"Sesión revocada: el usuario inactivo {user.username} intentó usar un token activo.",
            extra=log_extra(request, user.id, "SESSION_REVOKED_INACTIVE_USER", {"username": user.username}),
        )
        raise credentials_exception

    return user

def require_role(allowed_roles: list[UserRole]):
    def role_checker(request: Request, current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            sec_logger.warning(
                f"Acceso denegado. El usuario {current_user.username} intentó acceder a una ruta restringida.",
                extra=log_extra(request, current_user.id, "RBAC_VIOLATION",
                                {"required_roles": [r.value for r in allowed_roles]}),
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tenés permisos suficientes")
        return current_user
    return role_checker

get_owner = require_role([UserRole.OWNER])
get_staff = require_role([UserRole.OWNER, UserRole.EMPLOYEE])