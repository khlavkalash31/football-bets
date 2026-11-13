"""Import dei CSV di football-data.co.uk: risultati, statistiche e quote di apertura e chiusura."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import raw, store

SOURCE = "football-data"
LONDON = ZoneInfo("Europe/London")  # gli orari di football-data sono inglesi

STATS = {"FTHG": "home_goals", "FTAG": "away_goals", "HTHG": "home_goals_ht", "HTAG": "away_goals_ht",
         "HS": "home_shots", "AS": "away_shots", "HST": "home_shots_target", "AST": "away_shots_target",
         "HC": "home_corners", "AC": "away_corners", "HY": "home_yellow", "AY": "away_yellow",
         "HR": "home_red", "AR": "away_red", "Referee": "referee"}

# (bookmaker, fase) -> colonne in ordine di preferenza (i nomi cambiano tra le stagioni)
ODDS_1X2 = {
    ("avg", "open"): [("AvgH", "AvgD", "AvgA"), ("BbAvH", "BbAvD", "BbAvA")],
    ("max", "open"): [("MaxH", "MaxD", "MaxA"), ("BbMxH", "BbMxD", "BbMxA")],
    ("pinnacle", "open"): [("PSH", "PSD", "PSA")],
    ("bet365", "open"): [("B365H", "B365D", "B365A")],
    ("avg", "close"): [("AvgCH", "AvgCD", "AvgCA")],
    ("max", "close"): [("MaxCH", "MaxCD", "MaxCA")],
    ("pinnacle", "close"): [("PSCH", "PSCD", "PSCA")],
    ("bet365", "close"): [("B365CH", "B365CD", "B365CA")],
}
ODDS_OU25 = {
    ("avg", "open"): [("Avg>2.5", "Avg<2.5"), ("BbAv>2.5", "BbAv<2.5")],
    ("max", "open"): [("Max>2.5", "Max<2.5"), ("BbMx>2.5", "BbMx<2.5")],
    ("pinnacle", "open"): [("P>2.5", "P<2.5")],
    ("bet365", "open"): [("B365>2.5", "B365<2.5")],
    ("avg", "close"): [("AvgC>2.5", "AvgC<2.5")],
    ("max", "close"): [("MaxC>2.5", "MaxC<2.5")],
    ("pinnacle", "close"): [("PC>2.5", "PC<2.5")],
    ("bet365", "close"): [("B365C>2.5", "B365C<2.5")],
}


def _num(v):
    return None if pd.isna(v) else float(v)


def _int(v):
    return None if pd.isna(v) else int(v)


def _pick(row: pd.Series, options: list[tuple[str, ...]]):
    for cols in options:
        if all(c in row.index and not pd.isna(row[c]) for c in cols):
            return [float(row[c]) for c in cols]
    return None


def read_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")
    df = df.dropna(subset=["HomeTeam", "AwayTeam", "Date"]) if {"HomeTeam", "AwayTeam", "Date"} <= set(df) else df.iloc[0:0]
    return df


def import_csv(conn, path: Path, competition: str | None = None) -> tuple[int, int]:
    """Importa un CSV (es. data/I1_2526.csv). Ritorna (partite, quote) scritte. Rieseguibile: aggiorna."""
    path = Path(path)
    saved = raw.save(SOURCE, path.name, path.read_bytes())
    df = read_csv(path)
    if df.empty:
        return 0, 0
    if competition is None:
        competition = df["Div"].iloc[0] if "Div" in df else re.match(r"([A-Z]+\d)", path.name)[1]
    n_matches = n_odds = 0
    with conn.cursor() as cur:
        raw.register(cur, SOURCE, saved)
        for _, r in df.iterrows():
            day = pd.to_datetime(r["Date"], dayfirst=True).date()
            kickoff = None
            if "Time" in r.index and isinstance(r["Time"], str) and re.fullmatch(r"\d\d:\d\d", r["Time"]):
                kickoff = dt.datetime.combine(day, dt.time.fromisoformat(r["Time"]), LONDON)
            home = store.team_id(cur, SOURCE, r["HomeTeam"].strip(), competition)
            away = store.team_id(cur, SOURCE, r["AwayTeam"].strip(), competition)
            stats = {col: (r[src].strip() if isinstance(r[src], str) else None) if col == "referee" else _int(r[src])
                     for src, col in STATS.items() if src in r.index}
            mid = store.upsert_match(cur, competition, day, home, away, kickoff, **stats)
            n_matches += 1
            rows = []
            for (book, phase), options in ODDS_1X2.items():
                if (p := _pick(r, options)) is not None:
                    rows += [(mid, "1x2", o, book, phase, None, v, SOURCE) for o, v in zip("HDA", p)]
            for (book, phase), options in ODDS_OU25.items():
                if (p := _pick(r, options)) is not None:
                    rows += [(mid, "ou2.5", o, book, phase, None, v, SOURCE) for o, v in zip("OU", p)]
            n_odds += store.insert_odds(cur, rows)
    conn.commit()
    return n_matches, n_odds
