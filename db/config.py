"""Connessione al database e cartella dei file grezzi.

La connessione si legge da DATABASE_URL (variabile d'ambiente, poi file .env del progetto).
Senza DATABASE_URL si usa il Postgres di docker-compose.yml: utente, password e nome da
POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / POSTGRES_PORT (default bets/bets/bets/5432).
Per passare a un Postgres in cloud basta cambiare DATABASE_URL.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
SQL_DIR = Path(__file__).resolve().parent / "sql"


def env_file(path: Path = ENV_FILE) -> dict[str, str]:
    cfg = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.split("#")[0].strip().strip('"').strip("'")
    return cfg


def setting(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key) or env_file().get(key) or default


def db_configured() -> bool:
    """True se l'utente ha scelto un database (DATABASE_URL impostato): weekend.py salva solo allora."""
    return setting("DATABASE_URL") is not None


def database_url() -> str:
    url = setting("DATABASE_URL")
    if url:
        return url
    user, pwd = setting("POSTGRES_USER", "bets"), setting("POSTGRES_PASSWORD", "bets")
    name, port = setting("POSTGRES_DB", "bets"), setting("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{pwd}@localhost:{port}/{name}"


def raw_dir() -> Path:
    return Path(setting("RAW_DIR") or ROOT / "raw")


def connect():
    import psycopg  # import qui: chi non usa il database non deve installarlo

    return psycopg.connect(database_url())
