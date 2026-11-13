"""Dati per l'app: dal database se c'è (DATABASE_URL), altrimenti calcolati al volo dalle quote di mercato.

Così la stessa app gira sul PC con Postgres (storico, rilevazioni salvate) e su Streamlit Community Cloud
senza database (solo le partite dei prossimi giorni, ricalcolate dalle quote).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import weekend  # noqa: E402

LONDON = ZoneInfo("Europe/London")
ROME = ZoneInfo("Europe/Rome")
COMPETITIONS = weekend.COMPETITIONS


def secrets_to_env() -> None:
    """Su Streamlit Cloud chiavi e indirizzi stanno nei secrets: li si passa agli script come variabili d'ambiente."""
    for k in ("ODDS_API_KEY", "DATABASE_URL"):
        try:
            if k in st.secrets and not os.environ.get(k):
                os.environ[k] = str(st.secrets[k])
        except Exception:  # nessun file secrets: va bene, si usa il .env
            pass


def odds_key() -> str | None:
    return weekend.load_config().get("ODDS_API_KEY")


@st.cache_data(ttl=60, show_spinner=False)
def db_status() -> tuple[bool, str]:
    """(database utilizzabile, messaggio)."""
    import db

    if not db.db_configured():
        return False, "nessun database configurato (DATABASE_URL)"
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM schema_migrations")
        return True, db.database_url().split("@")[-1]
    except Exception as e:
        return False, f"database non raggiungibile: {type(e).__name__}"


def db_ok() -> bool:
    return db_status()[0]


@st.cache_data(ttl=300, show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    import db

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


UPCOMING_SQL = """
WITH p AS (SELECT DISTINCT ON (match_id) * FROM predictions WHERE model_version = 'market-v1'
           ORDER BY match_id, created_at DESC)
SELECT m.competition AS "Div", m.kickoff, h.name AS "HomeTeam", a.name AS "AwayTeam",
       p.lam::float AS lam, p.mu::float AS mu, p.created_at AS taken_at,
       s.odds_h::float AS "AvgH", s.odds_d::float AS "AvgD", s.odds_a::float AS "AvgA",
       s.odds_over25::float AS "Avg>2.5", s.odds_under25::float AS "Avg<2.5"
FROM p
JOIN matches m USING (match_id)
JOIN teams h ON h.team_id = m.home_team_id
JOIN teams a ON a.team_id = m.away_team_id
JOIN v_odds_wide s ON s.match_id = p.match_id AND s.phase = 'snapshot' AND s.bookmaker = 'avg'
                  AND s.taken_at = p.created_at
WHERE m.home_goals IS NULL AND m.kickoff > now() - interval '2 hours'
ORDER BY m.kickoff, m.competition
"""


def _with_uk_time(df: pd.DataFrame) -> pd.DataFrame:
    """Date e Time all'inglese, come in partite.csv (weekend.kickoff_it aggiunge l'ora italiana)."""
    k = pd.to_datetime(df["kickoff"], utc=True).dt.tz_convert(LONDON)
    return df.assign(Date=k.dt.strftime("%Y-%m-%d"), Time=k.dt.strftime("%H:%M"))


def upcoming_from_db() -> pd.DataFrame:
    df = query(UPCOMING_SQL)
    return _with_uk_time(df) if not df.empty else df


@st.cache_data(ttl=3600, show_spinner="Scarico le quote...")
def live_matches(leagues: tuple[str, ...], start: dt.date, days: int) -> tuple[pd.DataFrame, str, list[str]]:
    """Partite calcolate al momento dalle quote (senza database). In cache un'ora: ogni download costa crediti."""
    msgs: list[str] = []
    matches, source, _ = weekend.build_matches(list(leagues), start, days, log=msgs.append)
    return matches.assign(taken_at=pd.Timestamp.now(tz="UTC")), source, msgs


def refresh_into_db(leagues: list[str], start: dt.date, days: int) -> list[str]:
    """Scarica le quote e le salva nel database, come 'weekend.py lista'. Ritorna i messaggi da mostrare."""
    msgs: list[str] = []
    taken_at = dt.datetime.now(dt.timezone.utc)
    matches, source, raw_files = weekend.build_matches(leagues, start, days, log=msgs.append)
    import db
    from db import snapshot

    with db.connect() as conn:
        n_odds = sum(snapshot.ingest_odds_api(conn, path, div, taken_at) for div, path in raw_files)
        n = snapshot.save_weekend(conn, matches, source, taken_at)
    msgs.append(f"Salvate {n} partite" + (f" e {n_odds} quote dei bookmaker." if n_odds else "."))
    query.clear()
    return msgs


def current_matches() -> tuple[pd.DataFrame, str]:
    """Partite dei prossimi giorni e una riga che dice da dove vengono le quote."""
    if db_ok():
        df = upcoming_from_db()
        if df.empty:
            return df, "Nel database non ci sono partite future: aggiorna le quote."
        s = settings()
        first = pd.Timestamp(s["start"], tz=ROME)
        last = first + pd.Timedelta(days=s["days"])
        k = pd.to_datetime(df["kickoff"], utc=True)
        df = df[df["Div"].isin(s["leagues"]) & (k >= first) & (k < last)].reset_index(drop=True)
        t = pd.Timestamp(df["taken_at"].max()).tz_convert(ROME) if not df.empty else None
        return df, f"Quote dal database, rilevate il {t:%d/%m alle %H:%M}." if t else "Nessuna partita con questi filtri."
    s = settings()
    try:
        df, source, msgs = live_matches(tuple(s["leagues"]), s["start"], s["days"])
    except ValueError as e:
        return pd.DataFrame(), str(e)
    t = pd.Timestamp(df["taken_at"].max()).tz_convert(ROME)
    note = " ".join(msgs)
    return df, f"Quote da {source}, scaricate il {t:%d/%m alle %H:%M} (non salvate: nessun database). {note}"


def settings() -> dict:
    st.session_state.setdefault("leagues", list(COMPETITIONS))
    st.session_state.setdefault("days", 4)
    if st.session_state.get("start") is None or st.session_state["start"] < dt.date.today():
        st.session_state["start"] = dt.date.today()  # app rimasta aperta da ieri
    return {"leagues": st.session_state["leagues"], "days": st.session_state["days"],
            "start": st.session_state["start"]}


def match_label(m: pd.Series) -> str:
    k = weekend.kickoff_it(m)
    return f"{weekend.GIORNI[k.weekday()]} {k:%d/%m %H:%M} · {m['HomeTeam']} - {m['AwayTeam']}"


def logloss(p: pd.DataFrame, result: pd.Series) -> pd.Series:
    """Log loss di probabilità H/D/A (colonne in quest'ordine) dato il risultato 'H', 'D' o 'A'."""
    import numpy as np

    idx = result.map({"H": 0, "D": 1, "A": 2}).to_numpy()
    pp = p.to_numpy(float)[np.arange(len(p)), idx]
    return pd.Series(-np.log(np.clip(pp, 1e-12, 1)), index=p.index)

