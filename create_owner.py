from app.database import SessionLocal
from app.models import User, UserRole
from app.core.security import get_password_hash

def create_first_owner():
    db = SessionLocal()
    
    # Ahora verificamos si existe el username
    existing_user = db.query(User).filter(User.username == "admin").first()
    if existing_user:
        print("El dueño ya existe en la base de datos.")
        db.close()
        return

    new_owner = User(
        name="Maxi",
        username="admin",  # Usuario simple y corto
        hashed_password=get_password_hash("Admin123!"),
        role=UserRole.OWNER
    )
    
    db.add(new_owner)
    db.commit()
    print("¡Usuario dueño creado con éxito!")
    db.close()

if __name__ == "__main__":
    create_first_owner()