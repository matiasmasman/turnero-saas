import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Si no le pasamos una URL de PostgreSQL (ej. en desarrollo), crea un archivo SQLite local.
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./turnero_dev.db")

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Dependencia para usar en nuestras rutas
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()