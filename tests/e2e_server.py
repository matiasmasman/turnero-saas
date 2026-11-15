"""
Servidor de la app para las pruebas de navegador (3d). NO es una prueba: lo arranca tests/test_navegador_3d.py
como un proceso aparte.

    python tests/e2e_server.py <carpeta temporal> <contraseña de los usuarios>

Todo queda en la carpeta temporal y nada toca lo real:
- No lee el .env (app/database.py y app/core/security.py hacen load_dotenv(override=True), que pisaría
  estas variables): se anula load_dotenv, igual que en conftest.py.
- Base SQLite nueva en <carpeta>/e2e.db: `manage_staff init` + la migración e3a (scripts.migrate_e3).
- Log de seguridad en <carpeta>/security.log (SECURITY_LOG_FILE), no en logs/security.log.
- WhatsApp apagado y SECRET_KEY de prueba.
- Escucha SOLO en 127.0.0.1, en un puerto libre que elige el sistema. Cuando está listo, escribe el
  puerto en <carpeta>/port.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    tmp = Path(sys.argv[1]).resolve()
    password = sys.argv[2]
    db_url = f"sqlite:///{tmp / 'e2e.db'}"

    for key in [k for k in os.environ if k.startswith("WA_")]:
        del os.environ[key]
    os.environ.update({
        "DATABASE_URL": db_url,
        "SECRET_KEY": "e2e-" + "k" * 40,
        "WHATSAPP_ENABLED": "false",
        "SECURITY_LOG_FILE": str(tmp / "security.log"),
        "WA_TIMEZONE": "America/Argentina/Buenos_Aires",
    })

    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False

    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)                       # StaticFiles("frontend") es relativo

    # Freno: si el log de seguridad no quedó en la carpeta temporal (código que no conoce SECURITY_LOG_FILE),
    # no se sigue, para no escribir nunca en logs/security.log.
    import app.core.telemetry as telemetry
    if Path(telemetry.LOG_FILE).resolve() != (tmp / "security.log").resolve():
        sys.exit(f"ABORTADO: el log de seguridad apunta a {telemetry.LOG_FILE}, no a la carpeta temporal.")

    # Segundo freno: manage_staff y la app usan app.database (engine y SessionLocal). Si esa base no es
    # <carpeta>/e2e.db (por ejemplo, código que ignora DATABASE_URL), no se sigue: nunca turnero_dev.db.
    import app.database as database
    expected = (tmp / "e2e.db").resolve()
    for name, url in [("engine", database.engine.url), ("SessionLocal", database.SessionLocal.kw["bind"].url)]:
        if url.get_backend_name() != "sqlite" or not url.database or Path(url.database).resolve() != expected:
            sys.exit(f"ABORTADO: la base de {name} es {url.render_as_string(hide_password=True)}, "
                     f"no la temporal ({expected}).")

    import manage_staff
    manage_staff.cmd_init(password)
    from scripts.migrate_e3 import make_engine, migrate
    engine = make_engine(db_url)
    try:
        migrate(engine)
    finally:
        engine.dispose()

    import uvicorn
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config("app.main:app", log_level="warning", access_log=False))

    def announce():
        while not server.started:
            time.sleep(0.05)
        (tmp / "port.tmp").write_text(str(port))
        (tmp / "port.tmp").rename(tmp / "port")

    threading.Thread(target=announce, daemon=True).start()
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
