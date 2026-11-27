"""Probabili formazioni e indisponibili delle prossime partite, da FotMob.

FotMob mostra nella pagina di ogni partita da giocare la formazione prevista (lineupType 'predicted',
fornita da Enetpulse) con gli undici titolari, il modulo e gli indisponibili: infortunati, squalificati,
convocati in nazionale e in dubbio. Il venerdì c'è già. Ogni rilevazione si salva con la sua ora (taken_at),
così il modello potrà usare solo quello che si sapeva prima della partita.

Le squadre FotMob si riconoscono dagli abbinamenti salvati da import-fotmob (team_aliases, fonte fotmob):
serve aver lanciato import-fotmob almeno una volta per quei campionati.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import raw, store
from .fotmob import LEAGUES, SOURCE, Client, _first, _num, parse_league
from .understat import PlayerLinker, current_season


def parse_lineup(path: Path) -> dict | None:
    """{'type', 'kickoff', 'home'/'away': {'fm_id', 'formation', 'starters': [...], 'unavailable': [...]}}
    oppure None se la pagina non ha ancora una formazione."""
    data = json.loads(Path(path).read_text())
    lineup = _first(data, "lineup", dict)
    if not lineup or not isinstance(lineup.get("homeTeam"), dict):
        return None
    general = _first(data, "general", dict) or {}
    out = {"type": lineup.get("lineupType"), "kickoff": None,
           "match_id": _num(general.get("matchId") or lineup.get("matchId"), int)}
    if t := general.get("matchTimeUTCDate"):
        try:
            out["kickoff"] = dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        except ValueError:
            pass
    for side in ("home", "away"):
        team = lineup.get(f"{side}Team") or {}
        starters = [{"fm_id": _num(p.get("id"), int), "name": p.get("name") or "",
                     "position_id": _num(p.get("positionId"), int)}
                    for p in team.get("starters") or [] if isinstance(p, dict)]
        unavailable = []
        for p in team.get("unavailable") or []:
            if not isinstance(p, dict):
                continue
            u = p.get("unavailability") or {}
            back = u.get("expectedReturn") or u.get("expectedReturnDate")
            reason = u.get("injuryId")
            unavailable.append({"fm_id": _num(p.get("id"), int), "name": p.get("name") or "",
                                "kind": u.get("type"), "reason": None if reason is None else str(reason),
                                "expected_return": None if back is None else str(back),
                                "doubtful": str(back or "").lower() == "doubtful"})
        out[side] = {"fm_id": _num(team.get("id"), int), "formation": team.get("formation"),
                     "starters": starters, "unavailable": unavailable}
    return out


def _teams_by_fm_id(cur) -> dict[int, int]:
    """id FotMob -> team_id, dagli alias 'id|nome' salvati da import-fotmob."""
    cur.execute("SELECT alias, team_id FROM team_aliases WHERE source = %s", (SOURCE,))
    out = {}
    for alias, team in cur.fetchall():
        head = alias.split("|", 1)[0]
        if head.isdigit():
            out[int(head)] = team
    return out


def _match_id(cur, comp: str, day: dt.date, home: int, away: int, kickoff) -> int:
    """La partita già salvata (dalle quote o da football-data, con la data che può differire di un giorno),
    altrimenti una nuova."""
    cur.execute("""SELECT match_id FROM matches WHERE competition = %s AND home_team_id = %s AND away_team_id = %s
                   AND match_date BETWEEN %s AND %s ORDER BY abs(match_date - %s::date) LIMIT 1""",
                (comp, home, away, day - dt.timedelta(days=1), day + dt.timedelta(days=1), day))
    if row := cur.fetchone():
        if kickoff:
            cur.execute("UPDATE matches SET kickoff = coalesce(kickoff, %s) WHERE match_id = %s", (kickoff, row[0]))
        return row[0]
    return store.upsert_match(cur, comp, day, home, away, kickoff=kickoff)


def import_lineups(conn, client: Client, leagues: list[str], start: dt.date, days: int, log=print) -> dict[str, int]:
    """Formazioni previste delle partite da giocare tra start e start + days - 1."""
    taken_at = dt.datetime.now(dt.timezone.utc)
    end = start + dt.timedelta(days=days - 1)
    stats = {"partite": 0, "con formazione": 0, "titolari": 0, "indisponibili": 0, "senza squadra": 0}
    season = current_season(start)
    with conn.cursor() as cur:
        teams = _teams_by_fm_id(cur)
        linker = PlayerLinker(cur, SOURCE)
        for comp in [c for c in leagues if c in LEAGUES]:
            try:
                games = parse_league(client.league(comp, season), comp)
            except ValueError as e:
                log(f"  {comp}: calendario FotMob non letto ({e})")
                continue
            games = games[~games["played"] & (games["date"] >= start) & (games["date"] <= end)]
            for g in games.itertuples():
                home, away = teams.get(int(g.home_club_id)), teams.get(int(g.away_club_id))
                if home is None or away is None:
                    stats["senza squadra"] += 1
                    log(f"  nota: {g.home_club_name} - {g.away_club_name}: squadra FotMob non abbinata "
                        f"(lancia prima python -m db import-fotmob --leagues {comp})")
                    continue
                stats["partite"] += 1
                path = client.lineup(int(g.game_id))
                lu = parse_lineup(path)
                if lu is None or not lu["home"]["starters"]:
                    continue
                if lu["match_id"] is not None and lu["match_id"] != int(g.game_id):
                    log(f"  nota: pagina FotMob {g.game_id} di un'altra partita, saltata")
                    continue
                raw.register(cur, SOURCE, path)
                if lu["kickoff"] and lu["kickoff"] <= taken_at:
                    continue  # già cominciata: non è più una previsione
                match_id = _match_id(cur, comp, g.date, home, away, lu["kickoff"])
                stats["con formazione"] += 1
                for side, team in (("home", home), ("away", away)):
                    t = lu[side]
                    for p in t["starters"]:
                        pid = linker.get(p["fm_id"], p["name"], None, team, g.date) if p["fm_id"] else None
                        cur.execute("""INSERT INTO predicted_lineups (match_id, team_id, player_name, player_id, starter,
                                           source, taken_at, lineup_type, formation, position_id)
                                       VALUES (%s, %s, %s, %s, true, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                    (match_id, team, p["name"], pid, SOURCE, taken_at, lu["type"], t["formation"],
                                     p["position_id"]))
                        stats["titolari"] += 1
                    for p in t["unavailable"]:
                        pid = linker.get(p["fm_id"], p["name"], None, team, g.date) if p["fm_id"] else None
                        cur.execute("""INSERT INTO predicted_absences (match_id, team_id, player_name, player_id, kind,
                                           reason, expected_return, doubtful, source, taken_at)
                                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                    (match_id, team, p["name"], pid, p["kind"], p["reason"], p["expected_return"],
                                     p["doubtful"], SOURCE, taken_at))
                        stats["indisponibili"] += 1
    conn.commit()
    return stats
