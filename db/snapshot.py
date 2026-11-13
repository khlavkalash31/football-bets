"""Rilevazioni del venerdì: quote per bookmaker (The Odds API) e probabilità di weekend.py, con data e ora.

Sono i dati che non si possono recuperare dopo: servono a misurare il CLV contro la chiusura.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import raw, store

LONDON = ZoneInfo("Europe/London")
ODDS_API = "the-odds-api"


def _match(cur, source: str, div: str, day: dt.date, home: str, away: str, kickoff: dt.datetime | None) -> int:
    h = store.team_id(cur, source, home, div)
    a = store.team_id(cur, source, away, div)
    return store.upsert_match(cur, div, day, h, a, kickoff)


def ingest_odds_api(conn, path: Path, div: str, taken_at: dt.datetime) -> int:
    """Quote di ogni bookmaker da una risposta di The Odds API salvata in raw/. Ritorna le quote scritte."""
    events = json.loads(Path(path).read_text())
    n = 0
    with conn.cursor() as cur:
        raw.register(cur, ODDS_API, path)
        for ev in events:
            kickoff = pd.Timestamp(ev["commence_time"]).tz_convert(LONDON).to_pydatetime()
            mid = _match(cur, ODDS_API, div, kickoff.date(), ev["home_team"], ev["away_team"], kickoff)
            names = {ev["home_team"]: "H", "Draw": "D", ev["away_team"]: "A", "Over": "O", "Under": "U"}
            rows = []
            for b in ev.get("bookmakers", []):
                for mk in b.get("markets", []):
                    for o in mk.get("outcomes", []):
                        if mk["key"] == "h2h" and o["name"] in names:
                            rows.append((mid, "1x2", names[o["name"]], b["key"], "snapshot", taken_at, o["price"], ODDS_API))
                        elif mk["key"] == "totals" and o.get("point") == 2.5 and o["name"] in ("Over", "Under"):
                            rows.append((mid, "ou2.5", names[o["name"]], b["key"], "snapshot", taken_at, o["price"], ODDS_API))
            n += store.insert_odds(cur, rows)
    conn.commit()
    return n


def save_weekend(conn, matches: pd.DataFrame, source: str, taken_at: dt.datetime,
                 model_version: str = "market-v1") -> int:
    """Righe di partite.csv di weekend.py (Div, Date, Time, HomeTeam, AwayTeam, quote medie, lam, mu) ->
    quote medie e massime come snapshot + probabilità del modello. Ritorna le partite salvate."""
    from weekend import score_grid, selection_mask  # import qui: evita il ciclo weekend -> db -> weekend

    with conn.cursor() as cur:
        for _, m in matches.iterrows():
            day = pd.Timestamp(m["Date"]).date()
            kickoff = dt.datetime.combine(day, dt.time.fromisoformat(m["Time"]), LONDON) if isinstance(m["Time"], str) else None
            mid = _match(cur, source, m["Div"], day, m["HomeTeam"], m["AwayTeam"], kickoff)
            rows = []
            for book in ("Avg", "Max"):
                for o, col in zip("HDA", (f"{book}H", f"{book}D", f"{book}A")):
                    if col in m.index:
                        rows.append((mid, "1x2", o, book.lower(), "snapshot", taken_at, store_float(m[col]), source))
                for o, col in zip("OU", (f"{book}>2.5", f"{book}<2.5")):
                    if col in m.index:
                        rows.append((mid, "ou2.5", o, book.lower(), "snapshot", taken_at, store_float(m[col]), source))
            store.insert_odds(cur, rows)
            P = score_grid(m["lam"], m["mu"])
            p = [float((P * selection_mask(s)).sum()) for s in ("1", "X", "2", "O2.5")]
            store.insert_prediction(cur, mid, model_version, taken_at, float(m["lam"]), float(m["mu"]), *p)
    conn.commit()
    return len(matches)


def store_float(v):
    return None if pd.isna(v) else float(v)
