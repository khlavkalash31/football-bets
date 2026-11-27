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
               "match_xg": ("source",),
               "player_match_xg": ("player_id", "source"),
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


def copy_upsert(cur, table: str, cols: list[str], rows: list[tuple], key: list[str], extra: dict | None = None,
                update: list[str] | None = None) -> None:
    """Molte righe in poco tempo: COPY in una tabella temporanea, poi INSERT ... ON CONFLICT DO UPDATE.

    update: colonne da aggiornare se la riga c'è già (default tutte tranne la chiave)."""
    if not rows:
        return
    extra = extra or {}
    cols, rows = cols + list(extra), [(*r, *extra.values()) for r in rows]
    tmp = f"tmp_{table}"
    cur.execute(f"CREATE TEMP TABLE {tmp} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
    with cur.copy(f"COPY {tmp} ({', '.join(cols)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in (update or [c for c in cols if c not in key]))
    cur.execute(f"""INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM {tmp}
                    ON CONFLICT ({', '.join(key)}) DO UPDATE SET {upd}""")
    cur.execute(f"DROP TABLE {tmp}")


# Fonti degli xG, dalla preferita: matches.home_xg/away_xg e player_match.xg/xa tengono la prima disponibile.
XG_PRIORITY = ["understat", "fotmob"]
_CAN_SET_XG = "({t}.xg_source IS NULL OR array_position(%(prio)s::text[], {t}.xg_source) >= " \
              "array_position(%(prio)s::text[], %(src)s))"


def write_match_xg(cur, source: str, rows: list[tuple]) -> None:
    """rows: (match_id, home_xg, away_xg). I valori della fonte vanno in match_xg; in matches solo se la fonte
    non ha una priorità più bassa di quella già presente."""
    rows = [r for r in rows if r[1] is not None or r[2] is not None]
    if not rows:
        return
    cur.executemany("""INSERT INTO match_xg (match_id, source, home_xg, away_xg) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (match_id, source) DO UPDATE SET home_xg = EXCLUDED.home_xg,
                       away_xg = EXCLUDED.away_xg""", [(m, source, h, a) for m, h, a in rows])
    cur.executemany(f"""UPDATE matches SET home_xg = %(h)s, away_xg = %(a)s, xg_source = %(src)s
                        WHERE match_id = %(m)s AND {_CAN_SET_XG.format(t="matches")}""",
                    [{"h": h, "a": a, "m": m, "src": source, "prio": XG_PRIORITY} for m, h, a in rows])


PM_COLS = ["match_id", "player_id", "team_id", "started", "minutes", "goals", "assists", "xg", "xa", "yellow", "red"]


def write_player_match(cur, source: str, rows: list[tuple]) -> None:
    """rows: valori nell'ordine di PM_COLS. Righe nuove: si inseriscono intere. Righe di un'altra fonte (es.
    Transfermarkt): minuti, titolari, gol... restano quelli; xG e xA si scrivono solo se la fonte non ha una
    priorità più bassa di quella già presente. Righe della stessa fonte: si aggiornano.
    Gli xG di ogni fonte restano comunque in player_match_xg."""
    if not rows:
        return
    cur.execute("CREATE TEMP TABLE tmp_pm (LIKE player_match INCLUDING DEFAULTS) ON COMMIT DROP")
    with cur.copy(f"COPY tmp_pm ({', '.join(PM_COLS)}, source, xg_source) FROM STDIN") as cp:
        for r in rows:
            has_xg = r[7] is not None or r[8] is not None
            cp.write_row((*r, source, source if has_xg else None))
    same = "player_match.source = EXCLUDED.source"
    stats = ", ".join(f"{c} = CASE WHEN {same} THEN EXCLUDED.{c} ELSE player_match.{c} END"
                      for c in PM_COLS[2:] if c not in ("xg", "xa"))
    can = f"(EXCLUDED.xg_source IS NOT NULL AND {_CAN_SET_XG.format(t='player_match')})"
    xg = ", ".join(f"{c} = CASE WHEN {can} THEN EXCLUDED.{c} ELSE player_match.{c} END"
                   for c in ("xg", "xa", "xg_source"))
    cur.execute(f"""INSERT INTO player_match ({', '.join(PM_COLS)}, source, xg_source)
                    SELECT {', '.join(PM_COLS)}, source, xg_source FROM tmp_pm
                    ON CONFLICT (match_id, player_id) DO UPDATE SET {stats}, {xg}""",
                {"src": source, "prio": XG_PRIORITY})
    cur.execute("DROP TABLE tmp_pm")
    xrows = [(r[0], r[1], source, r[7], r[8]) for r in rows if r[7] is not None or r[8] is not None]
    cur.executemany("""INSERT INTO player_match_xg (match_id, player_id, source, xg, xa) VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (match_id, player_id, source) DO UPDATE SET xg = EXCLUDED.xg, xa = EXCLUDED.xa""",
                    xrows)
