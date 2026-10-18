from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, UserRole
from app.core.security import SECRET_KEY, ALGORITHM
from app.core.telemetry import sec_logger

# Le decimos a FastAPI dónde se consiguen los tokens
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

def get_current_user(request: Request, token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Abrimos el token
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Buscamos al usuario en la base de datos
    user = db.query(User).filter(User.id == int(user_id)).first()
    if user is None:
        raise credentials_exception
    return user

def require_role(allowed_roles: list[UserRole]):
    def role_checker(request: Request, current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            # ALERTA BLUE TEAM: Intento de movimiento lateral o escalada de privilegios
            sec_logger.warning(
                f"Acceso denegado. El usuario {current_user.username} intentó acceder a una ruta restringida.",
                extra={
                    "event_type": "RBAC_VIOLATION",
                    "request_id": request.state.request_id,
                    "ip_address": request.state.client_ip,
                    "actor_id": current_user.id,
                    "metadata": {"required_roles": [r.value for r in allowed_roles]}
                }
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tenés permisos suficientes")
        return current_user
    return role_checker

# Atajo para proteger rutas que solo el dueño puede usar
get_owner = require_role([UserRole.OWNER])

# Atajo para proteger rutas que cualquier empleado (y el dueño) pueden usar
get_staff = require_role([UserRole.OWNER, UserRole.EMPLOYEE])