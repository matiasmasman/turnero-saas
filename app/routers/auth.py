from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.core.security import verify_password, create_access_token
from app.core.telemetry import sec_logger

router = APIRouter(prefix="/auth", tags=["Autenticación"])

@router.post("/login", response_model=dict)
def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(), 
    db: Session = Depends(get_db)
):
    # Buscamos al usuario en la BD por su username
    user = db.query(User).filter(User.username == form_data.username).first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        sec_logger.warning(
            f"Fallo de login para usuario: {form_data.username}",
            extra={
                "event_type": "AUTH_FAILED",
                "request_id": request.state.request_id,
                "ip_address": request.state.client_ip,
                "metadata": {"username_attempted": form_data.username}
            }
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Si todo está bien, generamos el JWT
    access_token = create_access_token(data={"sub": str(user.id), "role": user.role.value})
    
    sec_logger.info(
        f"Login exitoso para usuario: {user.id}",
        extra={
            "event_type": "AUTH_SUCCESS",
            "request_id": request.state.request_id,
            "ip_address": request.state.client_ip,
            "actor_id": user.id,
            "metadata": {"role": user.role.value}
        }
    )

    return {"access_token": access_token, "token_type": "bearer"}