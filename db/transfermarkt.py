"""Import dello storico calciatori da transfermarkt-datasets (github.com/dcaribou/transfermarkt-datasets).

Prende giocatori, presenze (minuti, gol, assist, cartellini), titolari e valori di mercato delle partite
dei campionati che ci sono già nel database (quelli di football-data).

Le partite di Transfermarkt si abbinano alle nostre senza usare i nomi delle squadre, che tra le due fonti
sono molto diversi ("FC Internazionale Milano" contro "Inter"):
1. si accoppiano le partite con stessa competizione, data e risultato, quando l'accoppiamento è unico;
2. da queste coppie ogni club Transfermarkt riceve la squadra che compare più spesso (voto a maggioranza);
3. con la tabella club -> squadra si abbinano tutte le partite per competizione, data e squadre.

Attenzione: il dataset è fermo al 6 luglio 2026 (vedi il suo README), quindi copre le stagioni fino alla
2025/26 ma non quella in corso.
"""
from __future__ import annotations

import datetime as dt
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from . import raw

SOURCE = "transfermarkt"
BASE_URL = "https://pub-e682421888d945d684bcae8890b0ec20.r2.dev/data/{name}.csv.gz"
TABLES = ["games", "appearances", "game_lineups", "players", "player_valuations"]
# competition_id di Transfermarkt -> codice football-data
COMPETITIONS = {"IT1": "I1", "ES1": "SP1", "GB1": "E0", "FR1": "F1", "L1": "D1", "PO1": "P1"}
MIN_VOTES, MIN_SHARE = 3, 0.8  # un club è abbinato se almeno 3 partite e l'80% indicano la stessa squadra


def download(names: list[str] = TABLES) -> dict[str, Path]:
    """Scarica le tabelle in raw/transfermarkt/ (file identici non vengono duplicati)."""
    out = {}
    for name in names:
        req = urllib.request.Request(BASE_URL.format(name=name), headers={"User-Agent": "football-bets"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out[name] = raw.save(SOURCE, f"{name}.csv.gz", r.read())
        print(f"  ok  {name}")
    return out


def latest_files() -> dict[str, Path]:
    """Ultimo file scaricato di ogni tabella."""
    out = {}
    for name in TABLES:
        files = sorted(raw.raw_dir().glob(f"{SOURCE}/*/*_{name}.csv.gz"))
        if files:
            out[name] = files[-1]
    return out


def _read(path: Path, cols: list[str] | None = None) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    return df[[c for c in cols if c in df.columns]] if cols else df


def match_games(tm_games: pd.DataFrame, ours: pd.DataFrame) -> tuple[pd.DataFrame, dict[int, int], list[str]]:
    """Abbina le partite Transfermarkt alle nostre.

    tm_games: game_id, competition (codice nostro), date, home_club_id, away_club_id, home_goals, away_goals
    ours: match_id, competition, match_date, home_team_id, away_team_id, home_goals, away_goals
    Ritorna (game_id -> match_id, club_id -> team_id, avvisi)."""
    notes: list[str] = []
    t = tm_games.dropna(subset=["home_goals", "away_goals"]).copy()
    o = ours.dropna(subset=["home_goals", "away_goals"]).copy()
    key = ["competition", "date", "home_goals", "away_goals"]
    o = o.rename(columns={"match_date": "date"})
    t = t[~t.duplicated(key, keep=False)]
    o = o[~o.duplicated(key, keep=False)]
    pairs = t.merge(o, on=key)

    votes: dict[int, Counter] = defaultdict(Counter)
    for r in pairs.itertuples():
        votes[int(r.home_club_id)][int(r.home_team_id)] += 1
        votes[int(r.away_club_id)][int(r.away_team_id)] += 1
    clubs: dict[int, int] = {}
    for club, c in votes.items():
        team, n = c.most_common(1)[0]
        if n >= MIN_VOTES and n / sum(c.values()) >= MIN_SHARE:
            clubs[club] = team
    teams_taken = Counter(clubs.values())
    for team, n in teams_taken.items():
        if n > 1:  # due club sulla stessa squadra: abbinamento dubbio, li si scarta entrambi
            for club in [c for c, t_ in clubs.items() if t_ == team]:
                del clubs[club]
            notes.append(f"squadra {team}: più club Transfermarkt indicano la stessa squadra, scartati")

    g = tm_games.assign(home_team_id=tm_games["home_club_id"].map(clubs),
                        away_team_id=tm_games["away_club_id"].map(clubs)).dropna(subset=["home_team_id", "away_team_id"])
    g = g.astype({"home_team_id": int, "away_team_id": int})
    m = g.merge(ours.rename(columns={"match_date": "date"})[["match_id", "competition", "date", "home_team_id", "away_team_id"]],
                on=["competition", "date", "home_team_id", "away_team_id"], how="left")
    missing = m[m["match_id"].isna()]
    if len(missing):  # data spostata di un giorno tra le fonti (fuso orario, rinvii)
        alt = []
        for shift in (-1, 1):
            s = missing.drop(columns="match_id").assign(date=missing["date"] + dt.timedelta(days=shift))
            alt.append(s.merge(ours.rename(columns={"match_date": "date"})[["match_id", "competition", "date", "home_team_id",
                                                                             "away_team_id"]],
                               on=["competition", "date", "home_team_id", "away_team_id"]))
        alt = pd.concat(alt).drop_duplicates("game_id")
        m = pd.concat([m[m["match_id"].notna()], alt])
    games = m.dropna(subset=["match_id"])[["game_id", "match_id"]].astype(int).drop_duplicates("game_id")
    unmatched_clubs = set(tm_games["home_club_id"]) | set(tm_games["away_club_id"])
    unmatched_clubs -= set(clubs)
    if unmatched_clubs:
        notes.append(f"{len(unmatched_clubs)} club Transfermarkt senza squadra abbinata (le loro partite sono saltate)")
    return games, clubs, notes


def import_all(conn, files: dict[str, Path], first_season: int = 2016) -> dict[str, int]:
    """Importa le tabelle Transfermarkt scaricate. Rieseguibile: aggiorna senza duplicare."""
    missing = [t for t in TABLES if t not in files]
    if missing:
        raise ValueError(f"mancano i file Transfermarkt: {', '.join(missing)} (usa --download)")
    stats: dict[str, int] = {}

    tm = _read(files["games"], ["game_id", "competition_id", "season", "date", "home_club_id", "away_club_id",
                               "home_club_goals", "away_club_goals", "home_club_name", "away_club_name"])
    tm = tm[tm["competition_id"].isin(COMPETITIONS) & (tm["season"] >= first_season)].copy()
    tm["competition"] = tm["competition_id"].map(COMPETITIONS)
    tm["date"] = pd.to_datetime(tm["date"]).dt.date
    tm = tm.rename(columns={"home_club_goals": "home_goals", "away_club_goals": "away_goals"})

    with conn.cursor() as cur:
        for name, path in files.items():
            raw.register(cur, SOURCE, path)
        cur.execute("""SELECT match_id, competition, match_date, home_team_id, away_team_id, home_goals, away_goals
                       FROM matches WHERE competition = ANY(%s)""", (list(COMPETITIONS.values()),))
        ours = pd.DataFrame(cur.fetchall(), columns=["match_id", "competition", "match_date", "home_team_id",
                                                     "away_team_id", "home_goals", "away_goals"])
        if ours.empty:
            raise ValueError("nessuna partita nel database: prima 'python -m db import-football-data'")
        games, clubs, notes = match_games(tm, ours)
        stats["partite Transfermarkt"] = len(tm)
        stats["partite abbinate"] = len(games)
        stats["club abbinati"] = len(clubs)
        for n in notes:
            print(f"  nota: {n}")

        # nomi dei club come alias (per leggibilità); la chiave è l'id del club
        names = pd.concat([tm[["home_club_id", "home_club_name"]].set_axis(["club_id", "name"], axis=1),
                           tm[["away_club_id", "away_club_name"]].set_axis(["club_id", "name"], axis=1)])
        names = names.dropna().drop_duplicates("club_id", keep="last").set_index("club_id")["name"]
        cur.executemany("""INSERT INTO team_aliases (source, alias, team_id, confirmed) VALUES (%s, %s, %s, true)
                           ON CONFLICT (source, alias) DO UPDATE SET team_id = EXCLUDED.team_id""",
                        [(SOURCE, f"{club}|{names.get(club, '')}", team) for club, team in clubs.items()])

        # presenze delle partite abbinate
        ap = _read(files["appearances"], ["game_id", "player_id", "player_club_id", "minutes_played", "goals", "assists",
                                          "yellow_cards", "red_cards"])
        ap = ap.merge(games, on="game_id")
        ap["team_id"] = ap["player_club_id"].map(clubs)
        ap = ap.dropna(subset=["team_id"])
        lu = _read(files["game_lineups"], ["game_id", "player_id", "type"])
        starters = set(map(tuple, lu.loc[lu["type"] == "starting_lineup", ["game_id", "player_id"]].to_numpy()))
        ap["started"] = [(g, p) in starters for g, p in zip(ap["game_id"], ap["player_id"])]

        # giocatori: solo quelli con almeno una presenza importata
        pl = _read(files["players"], ["player_id", "name", "date_of_birth", "position", "sub_position", "foot",
                                      "country_of_citizenship"])
        pl = pl[pl["player_id"].isin(ap["player_id"].unique())]
        pid = _upsert_players(cur, pl)
        stats["giocatori"] = len(pid)

        ap["pid"] = ap["player_id"].map(pid)
        ap = ap.dropna(subset=["pid"]).drop_duplicates(["match_id", "pid"])
        rows = [(int(r.match_id), int(r.pid), int(r.team_id), bool(r.started), _i(r.minutes_played), _i(r.goals),
                 _i(r.assists), _i(r.yellow_cards), _i(r.red_cards)) for r in ap.itertuples()]
        _copy_upsert(cur, "player_match", ["match_id", "player_id", "team_id", "started", "minutes", "goals", "assists",
                                           "yellow", "red"], rows, key=["match_id", "player_id"], extra={"source": SOURCE})
        stats["presenze"] = len(rows)

        pv = _read(files["player_valuations"], ["player_id", "date", "market_value_in_eur"])
        pv = pv[pv["player_id"].isin(pid.index)].dropna()
        pv["date"] = pd.to_datetime(pv["date"]).dt.date
        pv = pv[pv["date"] >= dt.date(first_season - 1, 1, 1)].drop_duplicates(["player_id", "date"], keep="last")
        rows = [(int(pid[r.player_id]), r.date, int(r.market_value_in_eur)) for r in pv.itertuples()]
        _copy_upsert(cur, "player_value", ["player_id", "value_date", "value_eur"], rows, key=["player_id", "value_date"])
        stats["valori di mercato"] = len(rows)
    conn.commit()
    return stats


def _i(v):
    return None if pd.isna(v) else int(v)


def _upsert_players(cur, pl: pd.DataFrame) -> pd.Series:
    """Crea o aggiorna i giocatori; ritorna player_id Transfermarkt -> player_id nostro."""
    cur.execute("SELECT source_id, player_id FROM player_aliases WHERE source = %s", (SOURCE,))
    known = {int(s): p for s, p in cur.fetchall()}
    for r in pl.itertuples():
        pos = r.sub_position if isinstance(getattr(r, "sub_position", None), str) else getattr(r, "position", None)
        dob = pd.to_datetime(r.date_of_birth).date() if isinstance(getattr(r, "date_of_birth", None), str) else None
        vals = (r.name, dob, pos if isinstance(pos, str) else None,
                r.foot if isinstance(getattr(r, "foot", None), str) else None,
                r.country_of_citizenship if isinstance(getattr(r, "country_of_citizenship", None), str) else None)
        tm_id = int(r.player_id)
        if tm_id in known:
            cur.execute("""UPDATE players SET name = %s, birth_date = %s, position = %s, foot = %s, nationality = %s
                           WHERE player_id = %s""", (*vals, known[tm_id]))
        else:
            cur.execute("""INSERT INTO players (name, birth_date, position, foot, nationality)
                           VALUES (%s, %s, %s, %s, %s) RETURNING player_id""", vals)
            known[tm_id] = cur.fetchone()[0]
            cur.execute("INSERT INTO player_aliases (source, source_id, player_id) VALUES (%s, %s, %s)",
                        (SOURCE, str(tm_id), known[tm_id]))
    return pd.Series(known)[lambda s: s.index.isin(pl["player_id"])]


def _copy_upsert(cur, table: str, cols: list[str], rows: list[tuple], key: list[str], extra: dict | None = None) -> None:
    """Molte righe in poco tempo: COPY in una tabella temporanea, poi INSERT ... ON CONFLICT DO UPDATE."""
    if not rows:
        return
    extra = extra or {}
    cols, rows = cols + list(extra), [(*r, *extra.values()) for r in rows]
    tmp = f"tmp_{table}"
    cur.execute(f"CREATE TEMP TABLE {tmp} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
    with cur.copy(f"COPY {tmp} ({', '.join(cols)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in key)
    cur.execute(f"""INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM {tmp}
                    ON CONFLICT ({', '.join(key)}) DO UPDATE SET {upd}""")
    cur.execute(f"DROP TABLE {tmp}")


def read_local(path: Path) -> dict[str, Path]:
    """File già scaricati a mano (es. lo zip di Kaggle estratto): cartella con games.csv(.gz), appearances.csv(.gz)..."""
    out = {}
    for name in TABLES:
        for ext in (".csv.gz", ".csv"):
            f = Path(path) / f"{name}{ext}"
            if f.exists():
                out[name] = raw.save(SOURCE, f.name, f.read_bytes())
                break
    return out

