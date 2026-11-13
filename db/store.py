"""Scrittura nel database: squadre (con i nomi di ogni fonte), partite, quote, predizioni."""
from __future__ import annotations

import datetime as dt
import difflib
import re
import unicodedata

# Parole che le fonti aggiungono o tolgono ai nomi delle squadre ("AC Milan", "Inter Milan", "FC Porto").
_NOISE = {"fc", "cf", "ac", "afc", "ssc", "as", "sc", "ss", "us", "sv", "vfb", "vfl", "tsg", "fsv", "rc", "rcd",
          "ud", "cd", "sd", "club", "calcio", "de", "1", "04", "05", "1899", "1846", "1907", "1909", "1913",
          "fk", "sk", "bc", "ogc", "hotspur", "and", "albion"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", s)
    kept = [w for w in words if w not in _NOISE]
    return " ".join(kept or words)


def _prefix_match(a: str, b: str) -> bool:
    a, b = a.split(), b.split()
    if not a or not b:
        return False
    short, long_ = sorted([a, b], key=len)
    return all(x.startswith(y) or y.startswith(x) for x, y in zip(short, long_)) and len(short[0]) >= 3


def season_of(day: dt.date) -> str:
    """Stagione in formato football-data: luglio 2025 - giugno 2026 -> '2526'."""
    y = day.year if day.month >= 7 else day.year - 1
    return f"{y % 100:02d}{(y + 1) % 100:02d}"


def team_id(cur, source: str, name: str, competition: str | None = None, create: bool = True) -> int | None:
    """Squadra per nome in una fonte. Se il nome è nuovo: football-data crea la squadra (è il nome canonico);
    le altre fonti la cercano per somiglianza tra le squadre della stessa competizione e salvano l'abbinamento
    come da confermare. Senza un candidato credibile la squadra viene creata, anch'essa da confermare."""
    cur.execute("SELECT team_id FROM team_aliases WHERE source = %s AND alias = %s", (source, name))
    if row := cur.fetchone():
        return row[0]
    if source == "football-data":
        cur.execute("INSERT INTO teams (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                    "RETURNING team_id", (name,))
        tid = cur.fetchone()[0]
        cur.execute("INSERT INTO team_aliases (source, alias, team_id) VALUES (%s, %s, %s)", (source, name, tid))
        return tid

    cur.execute("""SELECT DISTINCT t.team_id, t.name FROM teams t
                   JOIN matches m ON t.team_id IN (m.home_team_id, m.away_team_id)
                   WHERE %(c)s::text IS NULL OR m.competition = %(c)s""", {"c": competition})
    cands = cur.fetchall()
    if not cands:
        cur.execute("SELECT team_id, name FROM teams")
        cands = cur.fetchall()
    target = norm(name)
    by_norm = {norm(n): tid for tid, n in cands}
    hit = by_norm.get(target)
    confirmed = hit is not None  # stesso nome a meno di accenti e sigle (FC, AC...): abbinamento sicuro
    if hit is None:
        close = difflib.get_close_matches(target, by_norm, n=1, cutoff=0.8)
        if not close:  # "man city" / "manchester city", "inter" / "inter milan": parole iniziali una prefisso dell'altra
            close = [k for k in by_norm if _prefix_match(target, k)]
            close = close if len(close) == 1 else []
        hit = by_norm[close[0]] if close else None
    if hit is None:
        if not create:
            return None
        cur.execute("INSERT INTO teams (name) VALUES (%s) ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                    "RETURNING team_id", (name,))
        hit = cur.fetchone()[0]
    cur.execute("INSERT INTO team_aliases (source, alias, team_id, confirmed) VALUES (%s, %s, %s, %s)",
                (source, name, hit, confirmed))
    return hit


MATCH_STATS = ["home_goals", "away_goals", "home_goals_ht", "away_goals_ht", "home_shots", "away_shots",
               "home_shots_target", "away_shots_target", "home_corners", "away_corners", "home_yellow",
               "away_yellow", "home_red", "away_red", "home_xg", "away_xg", "referee"]


def upsert_match(cur, competition: str, match_date: dt.date, home_id: int, away_id: int,
                 kickoff: dt.datetime | None = None, **stats) -> int:
    """Crea la partita o aggiorna i campi passati (quelli None non cancellano valori già presenti)."""
    stats = {k: v for k, v in stats.items() if k in MATCH_STATS}
    cols = ["competition", "season", "match_date", "home_team_id", "away_team_id", "kickoff", *stats]
    vals = [competition, season_of(match_date), match_date, home_id, away_id, kickoff, *stats.values()]
    updates = ", ".join(f"{c} = coalesce(EXCLUDED.{c}, matches.{c})" for c in ["kickoff", *stats])
    cur.execute(f"""INSERT INTO matches ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})
                    ON CONFLICT (competition, match_date, home_team_id, away_team_id) DO UPDATE SET {updates}
                    RETURNING match_id""", vals)
    return cur.fetchone()[0]


def insert_odds(cur, rows: list[tuple]) -> int:
    """rows: (match_id, market, outcome, bookmaker, phase, taken_at, price, source). Quote non valide saltate."""
    rows = [r for r in rows if r[6] is not None and r[6] == r[6] and r[6] > 1]  # r[6] == r[6]: scarta i NaN
    cur.executemany("""INSERT INTO odds_snapshots (match_id, market, outcome, bookmaker, phase, taken_at, price, source)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (match_id, market, outcome, bookmaker, phase, taken_at)
                       DO UPDATE SET price = EXCLUDED.price, source = EXCLUDED.source""", rows)
    return len(rows)


def insert_prediction(cur, match_id: int, model_version: str, created_at: dt.datetime, lam: float, mu: float,
                      p_home: float, p_draw: float, p_away: float, p_over25: float) -> None:
    cur.execute("""INSERT INTO predictions (match_id, model_version, created_at, lam, mu, p_home, p_draw, p_away, p_over25)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (match_id, model_version, created_at) DO NOTHING""",
                (match_id, model_version, created_at, lam, mu, p_home, p_draw, p_away, p_over25))


# colonne che, con match_id, rendono unica una riga (per non duplicarla spostandola su un'altra partita)
_MATCH_KEYS = {"odds_snapshots": ("market", "outcome", "bookmaker", "phase", "taken_at"),
               "predictions": ("model_version", "created_at"),
               "predicted_lineups": ("team_id", "player_name", "source", "taken_at"),
               "player_match": ("player_id",)}


def merge_team(cur, old: int, new: int) -> None:
    """Sposta partite, quote e predizioni della squadra old su new, poi elimina old.
    Serve quando un nome di un'altra fonte era stato creato come squadra nuova per errore."""
    cur.execute("""SELECT match_id, competition, match_date, home_team_id, away_team_id FROM matches
                   WHERE %s IN (home_team_id, away_team_id)""", (old,))
    for mid, comp, day, h, a in cur.fetchall():
        h, a = (new if h == old else h), (new if a == old else a)
        cur.execute("""SELECT match_id FROM matches WHERE competition = %s AND match_date = %s
                       AND home_team_id = %s AND away_team_id = %s""", (comp, day, h, a))
        if (row := cur.fetchone()) is None:
            cur.execute("UPDATE matches SET home_team_id = %s, away_team_id = %s WHERE match_id = %s", (h, a, mid))
            continue
        keep = row[0]  # la partita esiste già con la squadra giusta: ci si spostano quote e predizioni
        for table, key in _MATCH_KEYS.items():
            same = " AND ".join(f"k.{c} IS NOT DISTINCT FROM t.{c}" for c in key)
            cur.execute(f"DELETE FROM {table} t WHERE t.match_id = %s AND EXISTS "
                        f"(SELECT 1 FROM {table} k WHERE k.match_id = %s AND {same})", (mid, keep))
            cur.execute(f"UPDATE {table} SET match_id = %s WHERE match_id = %s", (keep, mid))
        cur.execute("DELETE FROM matches WHERE match_id = %s", (mid,))
    cur.execute("DELETE FROM team_elo e WHERE team_id = %s AND EXISTS "
                "(SELECT 1 FROM team_elo k WHERE k.team_id = %s AND k.elo_date = e.elo_date)", (old, new))
    for table in ("player_match", "predicted_lineups", "team_elo", "team_aliases"):
        cur.execute(f"UPDATE {table} SET team_id = %s WHERE team_id = %s", (new, old))
    cur.execute("DELETE FROM teams WHERE team_id = %s", (old,))
