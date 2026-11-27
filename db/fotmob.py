"""Import da FotMob (fotmob.com): xG di squadra e, per ogni giocatore, minuti, titolare, gol, assist, xG e xA.

È la seconda fonte di xG, accanto a Understat: copre tutti i nostri sei campionati (anche il Portogallo) e la
stagione in corso. Gli xG di FotMob vengono dal modello di Opta, diverso da quello di Understat: ogni fonte
tiene i suoi valori in match_xg e player_match_xg (vedi db/sql/005_xg_sources.sql).

FotMob non ha un'API pubblica: si leggono gli stessi dati JSON che usano le sue pagine. Si prova prima
l'indirizzo JSON (/api/...), poi la pagina web, da cui si estrae il blocco __NEXT_DATA__. Il formato non è
documentato e può cambiare: per questo si salva sempre la risposta in raw/fotmob/ e la lettura cerca i
campi per nome ovunque si trovino, invece di seguire un percorso fisso.

Partite e squadre si abbinano a quelle di football-data con data e risultato (transfermarkt.match_games),
i giocatori per nome come per Understat (understat.PlayerLinker).
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import raw
from .store import write_match_xg, write_player_match
from .transfermarkt import match_games
from .understat import PlayerLinker, SiteError, current_season

SOURCE = "fotmob"
URL = "https://www.fotmob.com"
# codice football-data -> id del campionato su FotMob
LEAGUES = {"I1": 55, "SP1": 87, "E0": 47, "F1": 53, "D1": 54, "P1": 61}
DELAY = 1.5  # secondi tra una richiesta e l'altra
ROME = ZoneInfo("Europe/Rome")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip",
}
_NEXT_DATA = re.compile(rb'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S)


def season_label(season: int) -> str:
    """2026 -> '2026/2027', il formato delle stagioni su FotMob."""
    return f"{season}/{season + 1}"


class Client:
    def __init__(self, delay: float = DELAY):
        self.delay = delay
        self._last = 0.0
        self._api_broken = False  # se l'API non dà dati e la pagina sì, per il resto dell'import si usa la pagina

    def _get(self, path: str) -> bytes:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(URL + path, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                return gzip.decompress(data) if r.headers.get("Content-Encoding") == "gzip" else data
        finally:
            self._last = time.monotonic()

    def _fetch(self, api_path: str | None, page_path: str, name: str, need: str) -> Path:
        """JSON dall'API; se non va, dalla pagina web (__NEXT_DATA__). Vale solo un JSON che contiene il campo
        'need'. Se nessuna delle due dà dati, salva l'ultima risposta come .html (così non viene scambiata per
        dati) e si ferma."""
        last, err = b"", ""
        paths = [p for p in (None if self._api_broken else api_path, page_path) if p]
        for path in paths:
            try:
                data = self._get(path)
            except urllib.error.HTTPError as e:
                last, err = e.read() or b"", f"HTTP {e.code} su {path}"
                continue
            m = _NEXT_DATA.search(data)
            for candidate in (data, m.group(1) if m else None):
                if candidate is None or candidate.lstrip()[:1] not in (b"{", b"["):
                    continue
                try:
                    if _first(json.loads(candidate), need) is not None:
                        if path == page_path and api_path and path is not paths[0]:
                            self._api_broken = True
                        return raw.save(SOURCE, name, candidate)
                except ValueError:
                    pass
            last, err = data, f"risposta senza dati su {path}"
        bad = raw.save(SOURCE, name.replace(".json", ".html"), last)
        raise SiteError(f"FotMob non ha dato i dati ({err}): forse ha cambiato formato o blocca le richieste. "
                        f"Risposta salvata in {bad}")

    def league(self, competition: str, season: int) -> Path:
        lid, label = LEAGUES[competition], season_label(season)
        q = "" if season == current_season() else f"&season={label}"
        return self._fetch(f"/api/leagues?id={lid}&tab=fixtures&type=league&timeZone=Europe/Rome{q}",
                           f"/leagues/{lid}/fixtures/x" + ("" if not q else f"?season={label}"),
                           f"league_{lid}_{season}.json", "allMatches")

    def lineup(self, game_id: int) -> Path:
        """Pagina di una partita da giocare, con la formazione prevista (l'API per queste risponde 404)."""
        return self._fetch(None, f"/match/{game_id}", f"lineup_{game_id}.json", "general")

    def match(self, game_id: int) -> Path:
        return self._fetch(f"/api/matchDetails?matchId={game_id}", f"/match/{game_id}", f"match_{game_id}.json",
                           "playerStats")


def cached_match(game_id: int) -> Path | None:
    files = sorted(raw.raw_dir().glob(f"{SOURCE}/*/*_match_{game_id}.json"))
    return files[-1] if files else None


# --- lettura dei JSON ---------------------------------------------------------------------------------------

def _walk(obj):
    """Tutti i dizionari contenuti in obj, in profondità."""
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            yield o
            stack.extend(reversed(list(o.values())))
        elif isinstance(o, list):
            stack.extend(reversed(o))


def _first(obj, key: str, kind=None):
    """Primo valore di 'key' trovato in obj (del tipo kind, se dato)."""
    for d in _walk(obj):
        if key in d and (kind is None or isinstance(d[key], kind)):
            return d[key]
    return None


def _num(v, kind):
    if isinstance(v, dict):  # {"value": 0.42, ...}
        v = v.get("value")
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return kind(float(v)) if kind is int else round(kind(v), 3)
    except (TypeError, ValueError):
        return None


def _score(m: dict) -> tuple[int | None, int | None]:
    h, a = _num((m.get("home") or {}).get("score"), int), _num((m.get("away") or {}).get("score"), int)
    if h is None or a is None:
        s = (m.get("status") or {}).get("scoreStr") or ""
        if mm := re.match(r"\s*(\d+)\s*-\s*(\d+)", s):
            h, a = int(mm.group(1)), int(mm.group(2))
    return h, a


def _date(m: dict) -> dt.date | None:
    t = (m.get("status") or {}).get("utcTime") or m.get("utcTime")
    if not t:
        return None
    try:
        when = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(ROME).date()


def parse_league(path: Path, competition: str) -> pd.DataFrame:
    """Partite di una stagione: game_id, competition, date, club, gol (solo se giocata), played."""
    data = json.loads(Path(path).read_text())
    matches = _first(data, "allMatches", list)
    if matches is None:
        raise ValueError("nessuna lista di partite (allMatches) nel file")
    rows = []
    for m in matches:
        if not isinstance(m, dict) or "home" not in m or "away" not in m:
            continue
        st = m.get("status") or {}
        played = bool(st.get("finished")) and not st.get("cancelled") and not st.get("awarded")
        h, a = _score(m) if played else (None, None)
        day = _date(m)
        if day is None:
            continue
        rows.append({
            "game_id": int(m["id"]), "competition": competition, "date": day,
            "home_club_id": int(m["home"]["id"]), "away_club_id": int(m["away"]["id"]),
            "home_club_name": m["home"].get("name", ""), "away_club_name": m["away"].get("name", ""),
            "home_goals": h, "away_goals": a, "played": played and h is not None,
        })
    return pd.DataFrame(rows, columns=["game_id", "competition", "date", "home_club_id", "away_club_id",
                                       "home_club_name", "away_club_name", "home_goals", "away_goals", "played"])


# chiavi delle statistiche dei giocatori su FotMob, con i titoli usati quando manca la chiave
_PSTATS = {
    "minutes": (("minutes_played",), ("minutes played",)),
    "goals": (("goals",), ("goals",)),
    "assists": (("assists", "goal_assist"), ("assists",)),
    "xg": (("expected_goals",), ("expected goals (xg)", "xg")),
    "xa": (("expected_assists",), ("expected assists (xa)", "xa")),
}
_KINDS = {"minutes": int, "goals": int, "assists": int, "xg": float, "xa": float}


def _player_stats(p: dict) -> dict:
    """Statistiche di un giocatore da playerStats: gruppi [{title, stats: {titolo: {key, stat: {value}}}}]."""
    found: dict[str, object] = {}
    for d in _walk(p.get("stats") or []):
        for title, item in d.items():
            if not isinstance(item, dict) or "stat" not in item:
                continue
            key, t = item.get("key"), str(title).lower()
            for name, (keys, titles) in _PSTATS.items():
                if name not in found and (key in keys or t in titles):
                    found[name] = _num(item["stat"], _KINDS[name])
    return found


def parse_match(path: Path) -> dict:
    """xG di squadra e giocatori di una partita: {'home_xg', 'away_xg', 'home_id', 'away_id', 'players': [...]},
    ogni giocatore con side 'h' o 'a'."""
    data = json.loads(Path(path).read_text())
    general = _first(data, "general", dict) or {}
    home_id = _num((general.get("homeTeam") or {}).get("id"), int)
    away_id = _num((general.get("awayTeam") or {}).get("id"), int)

    home_xg = away_xg = None
    periods = _first(data, "Periods", dict) or {}
    for d in _walk(periods.get("All") or data):
        if d.get("key") == "expected_goals" and isinstance(d.get("stats"), list) and len(d["stats"]) == 2:
            home_xg, away_xg = _num(d["stats"][0], float), _num(d["stats"][1], float)
            break

    starters: set[int] = set()
    lineup = _first(data, "lineup", (dict, list))
    for d in _walk(lineup or []):
        for k in ("starters", "optaLineup"):
            if isinstance(d.get(k), list):
                starters |= {int(x["id"]) for x in d[k] if isinstance(x, dict) and "id" in x}
    has_lineup = bool(starters)

    pstats = _first(data, "playerStats", dict)
    if pstats is None:
        raise ValueError("nessuna statistica dei giocatori (playerStats) nel file")
    players = []
    for key, p in pstats.items():
        if not isinstance(p, dict):
            continue
        pid = _num(p.get("id") or p.get("playerId") or key, int)
        team = _num(p.get("teamId"), int)
        if pid is None or team is None or team not in (home_id, away_id):
            continue
        s = _player_stats(p)
        if not s.get("minutes"):
            continue  # in panchina senza entrare
        players.append({
            "side": "h" if team == home_id else "a", "fm_player_id": pid, "name": p.get("name") or "",
            "started": (pid in starters) if has_lineup else None,
            # FotMob omette le statistiche a zero (xG senza tiri, xA senza passaggi chiave, gol dei portieri)
            "minutes": s.get("minutes"), "goals": s.get("goals") or 0, "assists": s.get("assists") or 0,
            "xg": s.get("xg") or 0.0, "xa": s.get("xa") or 0.0,
        })
    return {"home_xg": home_xg, "away_xg": away_xg, "home_id": home_id, "away_id": away_id, "players": players}


def _f(v):
    return None if v is None or pd.isna(v) else float(v)


# --- import ---------------------------------------------------------------------------------------------------

def import_season(conn, client: Client | None, competition: str, season: int, log=print) -> dict[str, int]:
    """Importa una stagione di un campionato. client None: usa solo i file già scaricati in raw/."""
    lid = LEAGUES[competition]
    if client is not None:
        league_path = client.league(competition, season)
    else:
        files = sorted(raw.raw_dir().glob(f"{SOURCE}/*/*_league_{lid}_{season}.json"))
        if not files:
            raise ValueError("nessun file scaricato")
        league_path = files[-1]
    fm = parse_league(league_path, competition)
    played = fm[fm["played"]]
    stats = {"partite giocate": len(played), "abbinate": 0, "con xG": 0, "con giocatori": 0,
             "giocatori collegati": 0, "giocatori nuovi": 0}
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
            log(f"  nota: {n.replace('Transfermarkt', 'FotMob')}")
        stats["abbinate"] = len(games)
        names = dict(zip(played["home_club_id"], played["home_club_name"])) | \
            dict(zip(played["away_club_id"], played["away_club_name"]))
        cur.executemany("""INSERT INTO team_aliases (source, alias, team_id, confirmed) VALUES (%s, %s, %s, true)
                           ON CONFLICT (source, alias) DO UPDATE SET team_id = EXCLUDED.team_id""",
                        [(SOURCE, f"{c}|{names.get(c, '')}", t) for c, t in clubs.items()])

        g = played.merge(games, on="game_id").merge(
            ours[["match_id", "match_date", "home_team_id", "away_team_id"]], on="match_id")
        todo = sum(cached_match(r.game_id) is None for r in g.itertuples())
        if client is not None and todo:
            log(f"  {competition} {season}: scarico {todo} partite (circa {todo * client.delay / 60:.0f} min)")

        linker = PlayerLinker(cur, SOURCE)
        xg_rows: list[tuple] = []
        rows: list[tuple] = []
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
                    log(f"  nota: partita FotMob {r.game_id} non scaricata ({type(e).__name__})")
                    continue
            try:
                m = parse_match(path)
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
                log(f"  nota: partita FotMob {r.game_id} in un formato che non conosco ({e}), saltata")
                path.rename(path.with_suffix(".bad"))  # non è più nella cache: al prossimo import si riscarica
                continue
            raw.register(cur, SOURCE, path)
            if m["home_id"] is not None and m["home_id"] != r.home_club_id:  # file di un'altra partita
                log(f"  nota: partita FotMob {r.game_id}: squadre diverse dal calendario, saltata")
                continue
            if m["home_xg"] is not None:
                xg_rows.append((int(r.match_id), m["home_xg"], m["away_xg"]))
                stats["con xG"] += 1
            seen = set()
            for p in m["players"]:
                team = r.home_team_id if p["side"] == "h" else r.away_team_id
                pid = linker.get(p["fm_player_id"], p["name"], None, int(team), r.match_date)
                if pid in seen:
                    continue
                seen.add(pid)
                rows.append((int(r.match_id), pid, int(team), p["started"], p["minutes"], p["goals"], p["assists"],
                             p["xg"], p["xa"], None, None))
            if m["players"]:
                stats["con giocatori"] += 1
        write_match_xg(cur, SOURCE, xg_rows)
        write_player_match(cur, SOURCE, rows)
        stats["giocatori collegati"], stats["giocatori nuovi"] = linker.linked, linker.created
    conn.commit()
    if client is not None and stats["abbinate"] and not stats["con xG"]:
        log(f"  attenzione: nessun xG letto per {competition} {season}: controlla i file in raw/{SOURCE}/")
    return stats
