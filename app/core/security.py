import os

from dotenv import load_dotenv
from passlib.context import CryptContext
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt

load_dotenv(override=True)

# La clave se lee SOLO del .env (nunca escrita en el código)
SECRET_KEY = os.getenv("SECRET_KEY", "")
if len(SECRET_KEY) < 32:
    raise RuntimeError("SECRET_KEY falta en el .env o es muy corta (mínimo 32 caracteres)")
# Fijos en el código A PROPÓSITO (Bloque 3, #1): NO se leen del .env. El .env del VPS tiene
# ACCESS_TOKEN_EXPIRE_MINUTES y ALGORITHM con valores que nadie revisó; acá se ignoran.
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24      # 24 horas (antes 7 días)
# Margen para relojes un poco corridos al comparar "exp" con el tope de 24 h (ver decode_access_token).
_EXP_LEEWAY = timedelta(minutes=5)

# Configuramos Bcrypt como nuestro algoritmo de hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password: str) -> str:
    """Recibe texto plano y devuelve un hash seguro."""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica si la contraseña ingresada coincide con el hash guardado."""
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "iat": now})

    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> dict:
    """Valida el token y devuelve su contenido. Si algo no cierra, lanza JWTError.

    - Algoritmo fijo (algorithms=[HS256]): un token firmado con otro algoritmo (o "none") no pasa.
    - "exp" y "sub" son obligatorios (python-jose, por defecto, acepta un token SIN exp: acá no).
    - Tope de vida: un token cuyo "exp" queda a más de 24 h de ahora se rechaza aunque la firma sea buena.
      Así, al desplegar, los tokens viejos de 7 días que todavía tienen más de 24 h por delante dejan de
      andar enseguida, y ningún token puede durar más que ACCESS_TOKEN_EXPIRE_MINUTES.
    """
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM],
                         options={"require_exp": True, "require_sub": True})
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise JWTError("exp inválido")
    ceiling = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES) + _EXP_LEEWAY
    if exp > ceiling.timestamp():
        raise JWTError("el token dura más de lo permitido")
    return payload