"""Copia del database del PC su un Postgres in cloud (es. Neon), per l'app pubblicata su Streamlit Cloud.

    python -m db sync-cloud      # CLOUD_DATABASE_URL nel .env

Il database vero resta quello del PC: la copia in cloud si sovrascrive a ogni sincronizzazione e l'app
online la legge e basta. Si copiano solo le tabelle che servono all'app: presenze, valori e xG dei singoli
calciatori restano sul PC (sono la parte più pesante e il piano gratuito di Neon ha 1 GB).
"""
from __future__ import annotations

import time

from .config import SQL_DIR, connect, setting

# In ordine di dipendenza (chiavi esterne).
TABLES = ["competitions", "teams", "team_aliases", "players", "player_aliases", "matches", "match_xg",
          "team_elo", "odds_snapshots", "predictions", "predicted_lineups", "predicted_absences"]
SKIPPED = ["player_match", "player_value", "player_match_xg", "absences", "raw_files"]


def cloud_url() -> str | None:
    return setting("CLOUD_DATABASE_URL")


def migrate(conn, log=print) -> list[str]:
    """Applica le migrazioni che mancano (le stesse di 'python -m db migrate'). Ritorna i nomi applicati."""
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, "
                    "applied_at timestamptz NOT NULL DEFAULT now())")
        cur.execute("SELECT name FROM schema_migrations")
        done = {r[0] for r in cur.fetchall()}
        todo = [p for p in sorted(SQL_DIR.glob("*.sql")) if p.name not in done]
        for p in todo:
            cur.execute(p.read_text())
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (p.name,))
            log(f"  applicata {p.name}")
    conn.commit()
    return [p.name for p in todo]


def _migrations(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations")
        return {r[0] for r in cur.fetchall()}


def _columns(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,))
        return [r[0] for r in cur.fetchall()]


def sync(url: str | None = None, log=print) -> None:
    """Sostituisce il contenuto delle TABLES in cloud con quello del database del PC, in una sola transazione:
    se qualcosa va storto la copia online resta quella di prima."""
    import psycopg

    url = url or cloud_url()
    if not url:
        raise SystemExit("Manca CLOUD_DATABASE_URL (nel .env): è l'indirizzo del Postgres in cloud.")
    t0 = time.time()
    with connect() as src, psycopg.connect(url) as dst:
        missing = {p.name for p in SQL_DIR.glob("*.sql")} - _migrations(src)
        if missing:
            raise SystemExit(f"Il database del PC non è aggiornato ({', '.join(sorted(missing))}): "
                             "lancia prima 'python -m db migrate'.")
        migrate(dst, log)
        with dst.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(TABLES)} CASCADE")
        for t in TABLES:
            cols = ", ".join(f'"{c}"' for c in _columns(src, t))
            with src.cursor() as rc, dst.cursor() as wc:
                with rc.copy(f"COPY {t} ({cols}) TO STDOUT (FORMAT BINARY)") as out, \
                        wc.copy(f"COPY {t} ({cols}) FROM STDIN (FORMAT BINARY)") as inp:
                    for chunk in out:
                        inp.write(chunk)
                wc.execute(f"SELECT count(*) FROM {t}")
                log(f"  {t}: {wc.fetchone()[0]} righe")
        with dst.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS cloud_sync (id int PRIMARY KEY DEFAULT 1 CHECK (id = 1), "
                        "synced_at timestamptz NOT NULL)")
            cur.execute("INSERT INTO cloud_sync VALUES (1, now()) "
                        "ON CONFLICT (id) DO UPDATE SET synced_at = EXCLUDED.synced_at")
        dst.commit()
        with dst.cursor() as cur:
            cur.execute("ANALYZE")
            cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            size = cur.fetchone()[0]
    log(f"Copia in cloud aggiornata in {time.time() - t0:.0f} s, database online: {size}.")


def last_sync(conn):
    """Ora dell'ultima sincronizzazione (None se il database non è una copia in cloud)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('cloud_sync') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT synced_at FROM cloud_sync")
        row = cur.fetchone()
        return row[0] if row else None
