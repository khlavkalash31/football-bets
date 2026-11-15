"""Import da Understat (understat.com): xG di squadra e, per ogni giocatore, minuti, titolare, gol, assist, xG e xA.

Serve soprattutto per la stagione in corso, che transfermarkt-datasets non ha più. Copre cinque dei nostri
campionati (il Portogallo su Understat non c'è) dal 2014/15.

Come per Transfermarkt, partite e squadre si abbinano a quelle di football-data con data e risultato
(transfermarkt.match_games). I giocatori si collegano a quelli già nel database per nome, cercando prima tra
chi ha giocato nella stessa squadra nelle ultime stagioni, poi tra tutti; chi non si trova viene creato.

Le pagine delle partite giocate non cambiano più: si scaricano una volta sola (restano in raw/understat/).
"""
from __future__ import annotations

import datetime as dt
import difflib
import http.cookiejar
import json
import re
import time
import unicodedata
import urllib.request
from collections import defaultdict
from pathlib import Path

import pandas as pd

from . import raw
from .transfermarkt import match_games

SOURCE = "understat"
URL = "https://understat.com"
# codice football-data -> nome del campionato su Understat
LEAGUES = {"I1": "Serie_A", "SP1": "La_liga", "E0": "EPL", "F1": "Ligue_1", "D1": "Bundesliga"}
DELAY = 1.0  # secondi tra una richiesta e l'altra, per non pesare sul sito


def current_season(today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


class SiteError(RuntimeError):
    """Understat ha risposto, ma non con i dati (manutenzione, pagina di errore)."""


class Client:
    """Scarica dalle API JSON che usano le pagine di Understat (servono i cookie della home)."""

    def __init__(self, delay: float = DELAY):
        self.delay = delay
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._ready = False
        self._last = 0.0

    def _get(self, path: str, ajax: bool = True) -> bytes:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        headers = {"User-Agent": "Mozilla/5.0 (football-bets)"}
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        req = urllib.request.Request(URL + path, headers=headers)
        try:
            with self._opener.open(req, timeout=60) as r:
                return r.read()
        finally:
            self._last = time.monotonic()

    def get_json(self, path: str, name: str) -> Path:
        """Scarica e salva in raw/. Se la risposta non è JSON (sito in manutenzione, pagina di errore)
        la salva come .html, così non viene scambiata per dati, e si ferma."""
        if not self._ready:
            self._get("/", ajax=False)
            self._ready = True
        data = self._get(path)
        try:
            json.loads(data)
        except ValueError:
            bad = raw.save(SOURCE, name.replace(".json", ".html"), data)
            text = " ".join(re.sub(r"<[^>]+>", " ", data.decode("utf-8", "replace")).split())[-120:]
            raise SiteError(f"Understat non ha dato i dati ('{text}'): forse è in manutenzione, riprova più tardi. "
                            f"Risposta salvata in {bad}") from None
        return raw.save(SOURCE, name, data)

    def league(self, competition: str, season: int) -> Path:
        return self.get_json(f"/getLeagueData/{LEAGUES[competition]}/{season}",
                             f"league_{LEAGUES[competition]}_{season}.json")

    def match(self, game_id: int) -> Path:
        return self.get_json(f"/getMatchData/{game_id}", f"match_{game_id}.json")


def cached_match(game_id: int) -> Path | None:
    files = sorted(raw.raw_dir().glob(f"{SOURCE}/*/*_match_{game_id}.json"))
    return files[-1] if files else None


def parse_league(path: Path, competition: str) -> pd.DataFrame:
    """Partite di una stagione: game_id, competition, date, club, gol (solo se giocata), xG."""
    data = json.loads(Path(path).read_text())
    rows = []
    for m in data["dates"]:
        played = bool(m.get("isResult"))
        rows.append({
            "game_id": int(m["id"]), "competition": competition,
            "date": dt.datetime.strptime(m["datetime"], "%Y-%m-%d %H:%M:%S").date(),
            "home_club_id": int(m["h"]["id"]), "away_club_id": int(m["a"]["id"]),
            "home_club_name": m["h"]["title"], "away_club_name": m["a"]["title"],
            "home_goals": _num(m["goals"]["h"], int) if played else None,
            "away_goals": _num(m["goals"]["a"], int) if played else None,
            "home_xg": _num(m["xG"]["h"], float) if played else None,
            "away_xg": _num(m["xG"]["a"], float) if played else None,
            "played": played,
        })
    return pd.DataFrame(rows)


def parse_match(path: Path) -> list[dict]:
    """Giocatori di una partita, con side 'h' o 'a'."""
    data = json.loads(Path(path).read_text())
    out = []
    for side, players in data["rosters"].items():
        for p in players.values():
            out.append({
                "side": side, "us_player_id": int(p["player_id"]), "name": p["player"],
                "position": p.get("position"), "started": p.get("position") != "Sub",
                "minutes": _num(p.get("time"), int), "goals": _num(p.get("goals"), int),
                "assists": _num(p.get("assists"), int), "xg": _num(p.get("xG"), float),
                "xa": _num(p.get("xA"), float), "yellow": _num(p.get("yellow_card"), int),
                "red": _num(p.get("red_card"), int),
            })
    return out


def _num(v, kind):
    if v is None or v == "":
        return None
    try:
        return kind(float(v)) if kind is int else round(kind(v), 3)
    except (TypeError, ValueError):
        return None


def _f(v):
    return None if v is None or pd.isna(v) else float(v)


def _pnorm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", s))


def name_score(a: str, b: str) -> float:
    """Somiglianza tra due nomi di giocatore (1 = uguali a meno di accenti e maiuscole)."""
    na, nb = _pnorm(a), _pnorm(b)
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and (ta <= tb or tb <= ta):  # "Vinicius Junior" e "Vinicius Jose Paixao de Oliveira Junior"
        return 0.92
    return difflib.SequenceMatcher(None, na, nb).ratio()


def best_match(name: str, candidates: dict[int, str], threshold: float = 0.85) -> int | None:
    """player_id del candidato col nome più simile, solo se supera la soglia e non è a pari merito con un altro."""
    scored = sorted(((name_score(name, n), pid) for pid, n in candidates.items()), reverse=True)
    if not scored or scored[0][0] < threshold:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0] and scored[1][1] != scored[0][1]:
        return None
    return scored[0][1]


class PlayerLinker:
    """Collega gli id dei giocatori Understat ai nostri player_id (o crea il giocatore)."""

    def __init__(self, cur):
        self.cur = cur
        cur.execute("SELECT source_id, player_id FROM player_aliases WHERE source = %s", (SOURCE,))
        self.known = {int(s): p for s, p in cur.fetchall()}
        cur.execute("SELECT player_id, name FROM players")
        self.by_norm: dict[str, list[int]] = defaultdict(list)
        for pid, name in cur.fetchall():
            self.by_norm[_pnorm(name)].append(pid)
        self._rosters: dict[tuple[int, dt.date], dict[int, str]] = {}
        self.linked = self.created = 0

    def roster(self, team_id: int, since: dt.date) -> dict[int, str]:
        """Giocatori già nel database che hanno giocato per la squadra da 'since' in poi."""
        k = (team_id, since)
        if k not in self._rosters:
            self.cur.execute("""SELECT DISTINCT p.player_id, p.name FROM player_match pm
                                JOIN players p USING (player_id) JOIN matches m USING (match_id)
                                WHERE pm.team_id = %s AND m.match_date >= %s""", (team_id, since))
            self._rosters[k] = dict(self.cur.fetchall())
        return self._rosters[k]

    def get(self, us_id: int, name: str, position: str | None, team_id: int, day: dt.date) -> int:
        if us_id in self.known:
            return self.known[us_id]
        roster = self.roster(team_id, dt.date(day.year - 2, 7, 1))
        pid = best_match(name, roster)
        if pid is None:
            same = self.by_norm.get(_pnorm(name), [])
            pid = same[0] if len(same) == 1 else None  # trasferito da una squadra che non abbiamo: nome identico e unico
        if pid is None:
            pos = position if position and position != "Sub" else None
            self.cur.execute("INSERT INTO players (name, position) VALUES (%s, %s) RETURNING player_id", (name, pos))
            pid = self.cur.fetchone()[0]
            self.by_norm[_pnorm(name)].append(pid)
            self.created += 1
        else:
            self.linked += 1
        self.cur.execute("INSERT INTO player_aliases (source, source_id, player_id) VALUES (%s, %s, %s) "
                         "ON CONFLICT DO NOTHING", (SOURCE, str(us_id), pid))
        self.known[us_id] = pid
        roster[pid] = name
        return pid


PM_COLS = ["match_id", "player_id", "team_id", "started", "minutes", "goals", "assists", "xg", "xa", "yellow", "red"]


def _write_player_match(cur, rows: list[tuple]) -> None:
    """Righe nuove: si inseriscono intere. Righe già importate da Transfermarkt: si aggiungono solo xG e xA,
    tenendo minuti e titolari di Transfermarkt. Righe già di Understat: si aggiornano."""
    if not rows:
        return
    cur.execute("CREATE TEMP TABLE tmp_us_pm (LIKE player_match INCLUDING DEFAULTS) ON COMMIT DROP")
    with cur.copy(f"COPY tmp_us_pm ({', '.join(PM_COLS)}, source) FROM STDIN") as cp:
        for r in rows:
            cp.write_row((*r, SOURCE))
    upd = ", ".join(f"{c} = CASE WHEN player_match.source = '{SOURCE}' THEN EXCLUDED.{c} ELSE player_match.{c} END"
                    for c in PM_COLS[2:] if c not in ("xg", "xa"))
    cur.execute(f"""INSERT INTO player_match ({', '.join(PM_COLS)}, source)
                    SELECT {', '.join(PM_COLS)}, source FROM tmp_us_pm
                    ON CONFLICT (match_id, player_id) DO UPDATE SET xg = EXCLUDED.xg, xa = EXCLUDED.xa, {upd}""")
    cur.execute("DROP TABLE tmp_us_pm")


def import_season(conn, client: Client | None, competition: str, season: int, log=print) -> dict[str, int]:
    """Importa una stagione di un campionato. client None: usa solo i file già scaricati in raw/."""
    if client is not None:
        league_path = client.league(competition, season)
    else:
        files = sorted(raw.raw_dir().glob(f"{SOURCE}/*/*_league_{LEAGUES[competition]}_{season}.json"))
        if not files:
            raise ValueError("nessun file scaricato")
        league_path = files[-1]  # i file che non erano JSON sono salvati come .html e qui non compaiono
    us = parse_league(league_path, competition)
    played = us[us["played"]]
    stats = {"partite giocate": len(played), "abbinate": 0, "con giocatori": 0, "giocatori collegati": 0,
             "giocatori nuovi": 0, "da scaricare": 0}
    if played.empty:
        return stats

    with conn.cursor() as cur:
        raw.register(cur, SOURCE, league_path)
        first, last = played["date"].min() - dt.timedelta(days=2), played["date"].max() + dt.timedelta(days=2)
        cur.execute("""SELECT match_id, competition, match_date, home_team_id, away_team_id, home_goals, away_goals
                       FROM matches WHERE competition = %s AND match_date BETWEEN %s AND %s""",
                    (competition, first, last))
        ours = pd.DataFrame(cur.fetchall(), columns=["match_id", "competition", "match_date", "home_team_id",
                                                     "away_team_id", "home_goals", "away_goals"])
        if ours.empty:
            log(f"  nota: nessuna partita {competition} {season} nel database (manca import-football-data?)")
            return stats
        games, clubs, notes = match_games(played, ours)
        for n in notes:
            log(f"  nota: {n}")
        stats["abbinate"] = len(games)
        names = dict(zip(played["home_club_id"], played["home_club_name"])) | \
            dict(zip(played["away_club_id"], played["away_club_name"]))
        cur.executemany("""INSERT INTO team_aliases (source, alias, team_id, confirmed) VALUES (%s, %s, %s, true)
                           ON CONFLICT (source, alias) DO UPDATE SET team_id = EXCLUDED.team_id""",
                        [(SOURCE, f"{c}|{names.get(c, '')}", t) for c, t in clubs.items()])

        g = played.merge(games, on="game_id").merge(
            ours[["match_id", "match_date", "home_team_id", "away_team_id"]], on="match_id")
        cur.executemany("UPDATE matches SET home_xg = %s, away_xg = %s WHERE match_id = %s",
                        [(_f(r.home_xg), _f(r.away_xg), int(r.match_id)) for r in g.itertuples()])

        linker = PlayerLinker(cur)
        rows: list[tuple] = []
        todo = [r for r in g.itertuples() if cached_match(r.game_id) is None]
        stats["da scaricare"] = len(todo) if client is None else 0
        if client is not None and todo:
            log(f"  {competition} {season}: scarico {len(todo)} partite (circa {len(todo) * client.delay / 60:.0f} min)")
        for r in g.itertuples():
            path = cached_match(r.game_id)
            if path is None:
                if client is None:
                    continue
                try:
                    path = client.match(r.game_id)
                except SiteError:
                    raise
                except Exception as e:  # una partita che non si scarica non blocca le altre
                    log(f"  nota: partita Understat {r.game_id} non scaricata ({type(e).__name__})")
                    continue
            try:
                players = parse_match(path)
            except (KeyError, ValueError, json.JSONDecodeError):
                log(f"  nota: partita Understat {r.game_id} in un formato che non conosco, saltata")
                continue
            raw.register(cur, SOURCE, path)
            seen = set()
            for p in players:
                team = r.home_team_id if p["side"] == "h" else r.away_team_id
                pid = linker.get(p["us_player_id"], p["name"], p["position"], int(team), r.match_date)
                if pid in seen:
                    continue
                seen.add(pid)
                rows.append((int(r.match_id), pid, int(team), p["started"], p["minutes"], p["goals"], p["assists"],
                             p["xg"], p["xa"], p["yellow"], p["red"]))
            stats["con giocatori"] += 1
        _write_player_match(cur, rows)
        stats["giocatori collegati"], stats["giocatori nuovi"] = linker.linked, linker.created
    conn.commit()
    return stats
