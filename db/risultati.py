"""Risultati finali delle partite su cui abbiamo una previsione.

- Sei campionati: CSV della stagione in corso di football-data.co.uk (gratis; ci sono anche le quote di
  chiusura, che servono per il confronto). Il sito li aggiorna due volte a settimana.
- Champions League: football-data non la copre, si usa l'endpoint scores di The Odds API
  (2 crediti per competizione, risultati degli ultimi 3 giorni al massimo).
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import pandas as pd

from . import raw, store
from .config import ROOT

LONDON = ZoneInfo("Europe/London")
ODDS_API = "the-odds-api"
SCORES_URL = "https://api.the-odds-api.com/v4/sports/{sport}/scores"


def current_season(today: dt.date | None = None) -> str:
    return store.season_of(today or dt.date.today())


def update_football_data(conn, leagues: list[str], log=print) -> int:
    """Scarica e importa i CSV della stagione in corso. Ritorna le partite lette."""
    from bet_model import download

    from .football_data import import_csv

    season = current_season()
    data_dir = ROOT / "data"
    download(data_dir, leagues, [season])
    n = 0
    for code in leagues:
        f = data_dir / f"{code}_{season}.csv"
        if f.exists():
            n += import_csv(conn, f)[0]
        else:
            log(f"  {code}: CSV {season} non disponibile")
    return n


def fetch_scores(key: str, sport: str, days_from: int = 3) -> list[dict]:
    q = urllib.parse.urlencode({"apiKey": key, "daysFrom": days_from, "dateFormat": "iso"})
    with urllib.request.urlopen(SCORES_URL.format(sport=sport) + "?" + q, timeout=30) as r:
        data = r.read()
    try:
        raw.save(ODDS_API, f"scores_{sport}.json", data)
    except Exception:  # la copia grezza è utile ma non indispensabile
        pass
    return json.loads(data)


def apply_scores(conn, div: str, events: list[dict]) -> tuple[int, list[str]]:
    """Scrive i risultati delle partite finite che sono già nel database (create da 'weekend.py lista').
    Ritorna (partite aggiornate, partite finite non trovate)."""
    n, missing = 0, []
    with conn.cursor() as cur:
        for ev in events:
            if not ev.get("completed") or not ev.get("scores"):
                continue
            goals = {s["name"]: int(s["score"]) for s in ev["scores"]}
            if ev["home_team"] not in goals or ev["away_team"] not in goals:
                continue
            day = pd.Timestamp(ev["commence_time"]).tz_convert(LONDON).date()
            h = store.team_id(cur, ODDS_API, ev["home_team"], div, create=False)
            a = store.team_id(cur, ODDS_API, ev["away_team"], div, create=False)
            if h is None or a is None:
                missing.append(f"{ev['home_team']} - {ev['away_team']}")
                continue
            cur.execute("""UPDATE matches SET home_goals = %s, away_goals = %s
                           WHERE competition = %s AND home_team_id = %s AND away_team_id = %s
                             AND match_date BETWEEN %s AND %s""",
                        (goals[ev["home_team"]], goals[ev["away_team"]], div, h, a,
                         day - dt.timedelta(days=1), day + dt.timedelta(days=1)))
            if cur.rowcount:
                n += cur.rowcount
            else:
                missing.append(f"{ev['home_team']} - {ev['away_team']}")
    conn.commit()
    return n, missing


def pending(conn) -> int:
    """Previsioni di partite già giocate (da più di 3 ore) che non hanno ancora il risultato."""
    with conn.cursor() as cur:
        cur.execute("""SELECT count(DISTINCT match_id) FROM v_prediction_results
                       WHERE result IS NULL AND coalesce(kickoff, match_date + 1) < now() - interval '3 hours'""")
        return cur.fetchone()[0]


def update_all(conn, leagues: list[str], odds_api_leagues: list[str], key: str | None, log=print) -> None:
    """Football-data per i campionati, The Odds API per le competizioni indicate (default la Champions)."""
    if leagues:
        n = update_football_data(conn, leagues, log)
        log(f"football-data: {n} partite della stagione {current_season()} lette.")
    if odds_api_leagues and not key:
        log("Manca ODDS_API_KEY: risultati di Champions non aggiornati.")
    elif odds_api_leagues:
        from weekend import ODDS_API_SPORTS

        for div in odds_api_leagues:
            try:
                n, missing = apply_scores(conn, div, fetch_scores(key, ODDS_API_SPORTS[div]))
            except Exception as e:  # crediti finiti, rete: si riprova la volta dopo
                log(f"The Odds API {div}: risultati non scaricati ({type(e).__name__}: {e}).")
                continue
            log(f"The Odds API {div}: {n} risultati scritti.")
            if missing:
                log(f"  non trovate nel database (mai rilevate o nome squadra diverso): {', '.join(missing)}")
    left = pending(conn)
    if left:
        log(f"{left} partite con previsione sono finite ma senza risultato: football-data non le ha ancora "
            f"(si aggiorna due volte a settimana) o il nome squadra non è abbinato (python -m db alias).")
