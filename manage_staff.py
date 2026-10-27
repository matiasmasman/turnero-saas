import sys
import getpass
import argparse
from datetime import time
from passlib.context import CryptContext

from app.database import SessionLocal, engine, Base
from app.models import User, UserRole, WorkingHours

# Intentamos usar el hasheador de app.core.security; si cambia el nombre, usamos bcrypt directo
try:
    from app.core.security import get_password_hash
except ImportError:
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    def get_password_hash(password: str) -> str:
        return pwd_context.hash(password)

# Horario semilla: 10:00 a 13:00 y 17:00 a 20:30
SHIFT1_START = time(10, 0)
SHIFT1_END = time(13, 0)
SHIFT2_START = time(17, 0)
SHIFT2_END = time(20, 30)

# Precios semilla por defecto
DEFAULT_PRICES = {
    "price_corte": 15000,
    "price_corte_promo2x": 12000,
    "price_corte_vip": 10000,
    "price_barba": 10000,
    "price_corte_barba": 18000,
}

INITIAL_STAFF = [
    {"name": "Maxi",   "username": "maxi",   "role": UserRole.OWNER},
    {"name": "Juampa", "username": "juampa", "role": UserRole.EMPLOYEE},
    {"name": "Leo",    "username": "leo",    "role": UserRole.EMPLOYEE},
    {"name": "Pedro",  "username": "pedro",  "role": UserRole.EMPLOYEE},
]


def seed_working_hours(db, user_id: int):
    """Crea o actualiza los 7 días de la semana (Lunes a Sábado activo, Domingo franco)."""
    existing = db.query(WorkingHours).filter(WorkingHours.employee_id == user_id).count()
    if existing > 0:
        return

    for day in range(7):
        is_working = day < 6  # 0=Lunes a 5=Sábado trabajan; 6=Domingo franco
        wh = WorkingHours(
            employee_id=user_id,
            day_of_week=day,
            is_working=is_working,
            shift1_start=SHIFT1_START,
            shift1_end=SHIFT1_END,
            shift2_start=SHIFT2_START,
            shift2_end=SHIFT2_END,
        )
        db.add(wh)


def cmd_init(default_password: str):
    """Inicializa a Maxi, Juampa, Leo y Pedro con precios y horarios semilla."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        print("\n=== Inicializando Staff de Men's Club ===")
        for member in INITIAL_STAFF:
            user = db.query(User).filter(User.username == member["username"]).first()
            if user:
                print(f"[•] El usuario '{member['username']}' ({member['name']}) ya existe. Verificando horarios...")
                seed_working_hours(db, user.id)
                continue

            pwd = default_password or f"{member['username']}123"
            new_user = User(
                name=member["name"],
                username=member["username"],
                hashed_password=get_password_hash(pwd),
                role=member["role"],
                is_active=True,
                duration_corte=30,
                duration_barba=15,
                duration_corte_barba=45,
                **DEFAULT_PRICES
            )
            db.add(new_user)
            db.flush()  # Para obtener new_user.id antes del commit
            seed_working_hours(db, new_user.id)
            print(f"[✓] Creado: {member['name']} | usuario: {member['username']} | pass: {pwd} | rol: {member['role'].value}")

        db.commit()
        print("\n[OK] Staff y horarios (Lun-Sáb 10:00-13:00 y 17:00-20:30) listos en la base de datos.\n")
    except Exception as e:
        db.rollback()
        print(f"[ERROR] Falló la inicialización: {e}")
    finally:
        db.close()


def cmd_list():
    """Lista todos los usuarios registrados en el sistema."""
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id).all()
        if not users:
            print("\nNo hay usuarios cargados todavía. Ejecutá: python manage_staff.py init\n")
            return

        print(f"\n{'ID':<4} {'NOMBRE':<12} {'USUARIO':<12} {'ROL':<10} {'ESTADO':<10} {'CORTE/BARBA/C+B'}")
        print("-" * 70)
        for u in users:
            estado = "ACTIVO" if u.is_active else "PAUSADO"
            tiempos = f"{u.duration_corte}m / {u.duration_barba}m / {u.duration_corte_barba}m"
            print(f"{u.id:<4} {u.name:<12} {u.username:<12} {u.role.value:<10} {estado:<10} {tiempos}")
        print()
    finally:
        db.close()


def cmd_add(name: str, username: str, password: str, role_str: str):
    """Agrega un nuevo barbero o dueño con el horario semilla."""
    db = SessionLocal()
    try:
        username = username.strip().lower()
        if db.query(User).filter(User.username == username).first():
            print(f"[ERROR] El username '{username}' ya existe.")
            return

        role = UserRole.OWNER if role_str.lower() == "owner" else UserRole.EMPLOYEE
        if not password:
            password = getpass.getpass(f"Contraseña para {username}: ")

        new_user = User(
            name=name.strip(),
            username=username,
            hashed_password=get_password_hash(password),
            role=role,
            is_active=True,
            duration_corte=30,
            duration_barba=15,
            duration_corte_barba=45,
            **DEFAULT_PRICES
        )
        db.add(new_user)
        db.flush()
        seed_working_hours(db, new_user.id)
        db.commit()
        print(f"[✓] Usuario '{name}' ({username}) dado de alta con horario 10:00-13:00 y 17:00-20:30.")
    except Exception as e:
        db.rollback()
        print(f"[ERROR] No se pudo crear el usuario: {e}")
    finally:
        db.close()


def cmd_toggle(username: str):
    """Activa o pausa (is_active) a un usuario inmediatamente."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username.strip().lower()).first()
        if not user:
            print(f"[ERROR] No se encontró el usuario '{username}'.")
            return

        user.is_active = not user.is_active
        db.commit()
        nuevo_estado = "ACTIVO" if user.is_active else "PAUSADO (Acceso bloqueado)"
        print(f"[✓] Estado de '{user.name}' ({user.username}) actualizado a: {nuevo_estado}")
    finally:
        db.close()


def cmd_passwd(username: str, password: str = None):
    """Cambia la contraseña de un usuario."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username.strip().lower()).first()
        if not user:
            print(f"[ERROR] No se encontró el usuario '{username}'.")
            return

        if not password:
            password = getpass.getpass(f"Nueva contraseña para '{user.username}': ")
            confirm = getpass.getpass("Confirmar nueva contraseña: ")
            if password != confirm:
                print("[ERROR] Las contraseñas no coinciden.")
                return

        user.hashed_password = get_password_hash(password)
        db.commit()
        print(f"[✓] Contraseña actualizada correctamente para '{user.name}' ({user.username}).")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="CLI de Gestión de Staff - Men's Club SaaS")
    subparsers = parser.add_subparsers(dest="command")

    # Comando: init
    init_parser = subparsers.add_parser("init", help="Inicializa a Maxi, Juampa, Leo y Pedro con horarios y precios")
    init_parser.add_argument("--password", "-p", default=None, help="Contraseña única inicial opcional (por defecto: <usuario>123)")

    # Comando: list
    subparsers.add_parser("list", help="Lista todos los barberos y su estado")

    # Comando: add
    add_parser = subparsers.add_parser("add", help="Agrega un nuevo empleado con horario semilla")
    add_parser.add_argument("name", help="Nombre visible (ej: 'Franco')")
    add_parser.add_argument("username", help="Nombre de usuario para login (ej: 'franco')")
    add_parser.add_argument("--password", "-p", default=None, help="Contraseña (si no se pasa, la pide oculta)")
    add_parser.add_argument("--role", "-r", default="employee", choices=["employee", "owner"], help="Rol del usuario")

    # Comando: toggle
    toggle_parser = subparsers.add_parser("toggle", help="Activa o pausa (is_active) a un barbero")
    toggle_parser.add_argument("username", help="Username del barbero (ej: leo)")

    # Comando: passwd
    passwd_parser = subparsers.add_parser("passwd", help="Cambia la contraseña de un usuario")
    passwd_parser.add_argument("username", help="Username del usuario (ej: maxi)")
    passwd_parser.add_argument("--password", "-p", default=None, help="Nueva contraseña")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args.password)
    elif args.command == "list":
        cmd_list()
    elif args.command == "add":
        cmd_add(args.name, args.username, args.password, args.role)
    elif args.command == "toggle":
        cmd_toggle(args.username)
    elif args.command == "passwd":
        cmd_passwd(args.username, args.password)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()