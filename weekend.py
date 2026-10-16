#!/usr/bin/env python3
"""
Quote eque per i mercati sui gol e confronto con le quote del tuo book (inserite a mano).

Dalle quote medie di mercato (1X2 e Under/Over 2.5: da The Odds API se nel file .env
del progetto c'è ODDS_API_KEY, che copre anche la Champions League; altrimenti da fixtures.csv di football-data.co.uk)
si ricavano i gol attesi delle due squadre e da lì la probabilità di ogni risultato esatto.
Ogni giocata che dipende solo dai gol (1X2, doppia chance, U/O, Goal/NoGoal, multigol,
risultato esatto e le loro combo) ha così una quota equa, senza margine.

Flusso del weekend:
    python weekend.py lista                 # quote eque delle partite dei prossimi 4 giorni
    python weekend.py valuta                # scrivi le quote del tuo book, una per riga:
        roma 1+O1.5 1.95                    #   squadra, giocata, quota
        bayern 1X+GG 2,10
    python weekend.py consiglia             # le giocate migliori per 5 euro tra quelle inserite
    python weekend.py proponi               # schedine proposte senza inserire nulla (quote stimate)

Giocate: 1 X 2 1X X2 12 · O2.5 U3.5 (soglie .5) · GG NG · MG2-4 · 2-1 (risultato esatto),
combinabili con "+": 1+O1.5, X2+GG, 1X+U3.5+GG.

Valore = quota book × probabilità equa − 1: quanto rende in media ogni euro giocato.
Negativo (quasi sempre) = paghi il margine del book; positivo = il book paga troppo.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import itertools
import re
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

from bet_model import LEAGUES, fair_probs

FIXTURES_URL = "https://www.football-data.co.uk/fixtures.csv"
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
ODDS_API_SPORTS = {"I1": "soccer_italy_serie_a", "SP1": "soccer_spain_la_liga", "E0": "soccer_epl",
                   "F1": "soccer_france_ligue_one", "D1": "soccer_germany_bundesliga",
                   "P1": "soccer_portugal_primeira_liga", "CL": "soccer_uefa_champs_league"}
COMPETITIONS = {**LEAGUES, "CL": "Champions League"}  # la Champions c'è solo con The Odds API
CONFIG_FILE = Path(__file__).resolve().parent / ".env"  # chiavi e credenziali, escluso da git
# Correzione Dixon-Coles per i punteggi bassi, scelta su 2.500 partite 2022-2026 confrontando
# rho = 0 / -0.1 / -0.2: con -0.1 GG, O1.5, NG+U2.5, 1X+U3.5 stanno tutti entro 1 punto dal reale
# (con 0 GG era sottostimato di 1,7 punti, con -0.2 O1.5 sovrastimato di 1,5).
RHO = -0.1
G = 11  # gol massimi per squadra nella griglia
H, A = np.meshgrid(np.arange(G), np.arange(G), indexing="ij")


# ---------------------------------------------------------------- modello dei gol

def score_grid(lam: float, mu: float, rho: float = RHO) -> np.ndarray:
    P = np.outer(poisson.pmf(np.arange(G), lam), poisson.pmf(np.arange(G), mu))
    P[0, 0] *= 1 - lam * mu * rho
    P[0, 1] *= 1 + lam * rho
    P[1, 0] *= 1 + mu * rho
    P[1, 1] *= 1 - rho
    return P / P.sum()


def fit_goals(p1x2: np.ndarray, p_over25: float) -> tuple[float, float]:
    """Gol attesi (casa, trasferta) che riproducono le probabilità 1X2 e Over 2.5 del mercato."""
    masks = [H > A, H == A, H < A, H + A > 2.5]
    target = [*p1x2, p_over25]

    def loss(x):
        P = score_grid(*np.exp(x))
        return sum(((P * m).sum() - t) ** 2 for m, t in zip(masks, target))

    res = minimize(loss, [0.3, 0.1], method="Nelder-Mead", options={"xatol": 1e-5, "fatol": 1e-10})
    return tuple(np.exp(res.x))


# ---------------------------------------------------------------- giocate

OUTCOMES = {"1": H > A, "X": H == A, "2": H < A, "1X": H >= A, "X2": H <= A, "12": H != A,
            "GG": (H > 0) & (A > 0), "NG": (H == 0) | (A == 0)}


def selection_mask(sel: str) -> np.ndarray:
    """'1X+O1.5+GG' -> maschera booleana sulla griglia dei risultati."""
    mask = np.ones((G, G), bool)
    for atom in sel.upper().replace(",", ".").split("+"):
        if atom in OUTCOMES:
            m = OUTCOMES[atom]
        elif r := re.fullmatch(r"(O|U|OVER|UNDER)(\d+\.5)", atom):
            line = float(r[2])
            m = H + A > line if r[1].startswith("O") else H + A < line
        elif r := re.fullmatch(r"MG(\d+)-(\d+)", atom):
            m = (H + A >= int(r[1])) & (H + A <= int(r[2]))
        elif r := re.fullmatch(r"(\d+)-(\d+)", atom):
            m = (H == int(r[1])) & (A == int(r[2]))
        else:
            raise ValueError(f"giocata non riconosciuta: {atom!r}")
        mask &= m
    return mask


def prob(match: pd.Series, sel: str) -> float:
    return float((score_grid(match["lam"], match["mu"]) * selection_mask(sel)).sum())


# Giocate considerate da "proponi". Le quote di 1X2 e U/O 2.5 sono quelle medie di mercato;
# per le altre la quota è stimata applicando alla quota equa il margine 1X2 della partita.
MENU = ["1", "X", "2", "1X", "X2", "12", "O1.5", "U1.5", "O2.5", "U2.5", "O3.5", "U3.5", "GG", "NG",
        "MG1-3", "MG2-3", "MG2-4", "GG+O2.5", "NG+U2.5",
        *[f"{e}+{g}" for e in ["1", "2", "1X", "X2"] for g in ["O1.5", "U3.5", "O2.5", "U2.5", "GG", "NG"]]]
MARKET_ODDS = {"1": "AvgH", "X": "AvgD", "2": "AvgA", "O2.5": "Avg>2.5", "U2.5": "Avg<2.5"}


def market_menu(match: pd.Series, sels: list[str] = MENU) -> pd.DataFrame:
    """Probabilità, quota equa e quota stimata del book per ogni giocata di una partita."""
    P = score_grid(match["lam"], match["mu"])
    overround = 1 / match["AvgH"] + 1 / match["AvgD"] + 1 / match["AvgA"]
    rows = []
    for sel in sels:
        p = float((P * selection_mask(sel)).sum())
        quota = match[MARKET_ODDS[sel]] if sel in MARKET_ODDS else 1 / p / overround
        rows.append({"Giocata": sel, "Prob": p, "Equa": 1 / p, "Quota": quota,
                     "Stimata": sel not in MARKET_ODDS, "Valore": quota * p - 1})
    return pd.DataFrame(rows)


def kickoff_it(match: pd.Series) -> pd.Timestamp:
    """Gli orari salvati sono inglesi (come in football-data): in Italia è un'ora dopo, tutto l'anno."""
    return pd.Timestamp(f"{match['Date']} {match['Time']}") + pd.Timedelta(hours=1)


# ---------------------------------------------------------------- file del weekend

def load_config(path: Path = CONFIG_FILE) -> dict[str, str]:
    """Righe CHIAVE=valore del file di configurazione; le variabili d'ambiente hanno la precedenza."""
    cfg = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.split("#")[0].strip()
    cfg.update({k: v for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "EMAIL_TO", "ODDS_API_KEY")
                if (v := os.environ.get(k))})
    return {k: v for k, v in cfg.items() if v}


def fetch_odds_api(key: str, divs: list[str], start: dt.date, days: int) -> pd.DataFrame:
    """Quote 1X2 e U/O 2.5 dei bookmaker europei, nello stesso formato di fixtures.csv (2 crediti per lega)."""
    rows = []
    for div in [d for d in divs if d in ODDS_API_SPORTS]:
        query = urllib.parse.urlencode({
            "apiKey": key, "regions": "eu", "markets": "h2h,totals", "oddsFormat": "decimal",
            "commenceTimeFrom": f"{start}T00:00:00Z", "commenceTimeTo": f"{start + dt.timedelta(days=days)}T00:00:00Z"})
        with urllib.request.urlopen(f"{ODDS_API_URL.format(sport=ODDS_API_SPORTS[div])}?{query}", timeout=30) as r:
            events, left = json.load(r), r.headers.get("x-requests-remaining")
        print(f"  The Odds API {div}: {len(events)} partite, crediti rimasti {left}", file=sys.stderr)
        for ev in events:
            home, away = ev["home_team"], ev["away_team"]
            x12, ou = [], []
            for b in ev["bookmakers"]:
                for mk in b["markets"]:
                    o = {(x["name"], x.get("point")): x["price"] for x in mk["outcomes"]}
                    if mk["key"] == "h2h" and all((n, None) in o for n in (home, "Draw", away)):
                        x12.append([o[(home, None)], o[("Draw", None)], o[(away, None)]])
                    elif mk["key"] == "totals" and ("Over", 2.5) in o and ("Under", 2.5) in o:
                        ou.append([o[("Over", 2.5)], o[("Under", 2.5)]])
            if not x12:
                continue
            t = pd.Timestamp(ev["commence_time"]).tz_convert("Europe/London")
            x12, ou = np.array(x12), np.array(ou) if ou else np.full((1, 2), np.nan)
            rows.append({"Div": div, "Date": f"{t:%d/%m/%Y}", "Time": f"{t:%H:%M}", "HomeTeam": home, "AwayTeam": away,
                         **dict(zip(["AvgH", "AvgD", "AvgA"], x12.mean(axis=0))),
                         **dict(zip(["MaxH", "MaxD", "MaxA"], x12.max(axis=0))),
                         **dict(zip(["Avg>2.5", "Avg<2.5"], ou.mean(axis=0))), "Books": len(x12)})
    return pd.DataFrame(rows, columns=["Div", "Date", "Time", "HomeTeam", "AwayTeam", "AvgH", "AvgD", "AvgA",
                                       "MaxH", "MaxD", "MaxA", "Avg>2.5", "Avg<2.5", "Books"])


def fetch_fixtures(path: Path | None) -> pd.DataFrame:
    if path is not None:
        return pd.read_csv(path, encoding="utf-8-sig")
    req = urllib.request.Request(FIXTURES_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return pd.read_csv(io.BytesIO(r.read()), encoding="utf-8-sig")


def weekend_dir(root: Path, name: str | None) -> Path:
    if name:
        return root / name
    dirs = sorted(d for d in root.glob("*") if (d / "partite.csv").exists())
    if not dirs:
        sys.exit("Nessun weekend salvato: lancia prima 'python weekend.py lista'.")
    return dirs[-1]


def load_matches(wdir: Path) -> pd.DataFrame:
    m = pd.read_csv(wdir / "partite.csv")
    m["Partita"] = m["HomeTeam"] + " - " + m["AwayTeam"]
    return m


def find_match(matches: pd.DataFrame, query: str) -> pd.Series:
    hits = matches[matches["Partita"].str.lower().str.contains(query.lower(), regex=False)]
    if len(hits) != 1:
        found = ", ".join(hits["Partita"]) or "nessuna"
        raise ValueError(f"'{query}' trova {len(hits)} partite ({found}): scrivi meglio il nome")
    return hits.iloc[0]


# ---------------------------------------------------------------- comandi

def cmd_lista(args):
    key = load_config().get("ODDS_API_KEY")
    if args.source == "odds-api" or (args.source == "auto" and key and args.fixtures is None):
        if not key:
            sys.exit(f"Manca ODDS_API_KEY in {CONFIG_FILE}.")
        try:
            fx = fetch_odds_api(key, args.leagues, args.start, args.days)
        except Exception as e:  # chiave errata, crediti finiti, rete: meglio quote vecchie che niente
            if args.source == "odds-api":
                raise
            print(f"The Odds API non disponibile ({e}): uso football-data, senza Champions League.")
            fx = fetch_fixtures(args.fixtures)
    else:
        fx = fetch_fixtures(args.fixtures)
    fx = fx[fx["Div"].isin(args.leagues)].copy()
    fx["Date"] = pd.to_datetime(fx["Date"], dayfirst=True).dt.date
    fx = fx[(fx["Date"] >= args.start) & (fx["Date"] < args.start + dt.timedelta(days=args.days))]
    cols = ["AvgH", "AvgD", "AvgA", "Avg>2.5", "Avg<2.5"]
    missing = fx[cols].isna().any(axis=1)
    if missing.any():
        print(f"Senza quote 1X2 o U/O 2.5, escluse: {', '.join(fx.loc[missing, 'HomeTeam'] + ' - ' + fx.loc[missing, 'AwayTeam'])}")
    fx = fx[~missing].sort_values(["Date", "Time"]).reset_index(drop=True)
    if fx.empty:
        sys.exit(f"Nessuna partita tra {args.start} e i {args.days} giorni dopo (fixtures.csv aggiornato?).")

    p1x2 = fair_probs(fx[["AvgH", "AvgD", "AvgA"]].to_numpy(float))
    pou = fair_probs(fx[["Avg>2.5", "Avg<2.5"]].to_numpy(float))
    goals = [fit_goals(p1x2[i], pou[i, 0]) for i in range(len(fx))]
    matches = fx[["Div", "Date", "Time", "HomeTeam", "AwayTeam", *cols]].assign(
        lam=[g[0] for g in goals], mu=[g[1] for g in goals])

    wdir = args.root / str(args.start)
    wdir.mkdir(parents=True, exist_ok=True)
    matches.to_csv(wdir / "partite.csv", index=False)

    sheet = ["1", "X", "2", "1X", "X2", "U2.5", "O2.5", "GG", "NG", "1+O1.5", "2+O1.5"]
    matches = load_matches(wdir)
    rows = []
    for _, m in matches.iterrows():
        P = score_grid(m["lam"], m["mu"])
        rows.append({"Ora IT": kickoff_it(m).strftime("%d/%m %H:%M"), "Lega": m["Div"], "Partita": m["Partita"],
                     "Gol": f"{m['lam']:.1f}-{m['mu']:.1f}",
                     **{s: 1 / (P * selection_mask(s)).sum() for s in sheet}})
    print(f"\nQuote eque (senza margine) · {len(rows)} partite · salvate in {wdir}/partite.csv\n")
    print(pd.DataFrame(rows).to_string(index=False, float_format="{:.2f}".format))
    print("\nGioca solo dove il tuo book paga più della quota equa (o quasi). Poi: python weekend.py valuta")


def cmd_valuta(args):
    wdir = weekend_dir(args.root, args.weekend)
    matches = load_matches(wdir)
    book_path = wdir / "book.csv"
    book = pd.read_csv(book_path) if book_path.exists() else pd.DataFrame()
    interactive = sys.stdin.isatty()
    if interactive:
        print(f"Weekend {wdir.name}. Scrivi: squadra giocata quota (es. 'roma 1+O1.5 1.95'). Riga vuota per finire.")
    new = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            if interactive:
                break
            continue
        try:
            *query, sel, odds = line.split()
            odds = float(odds.replace(",", "."))
            m = find_match(matches, " ".join(query))
            p = prob(m, sel)
        except ValueError as e:
            print(f"  ?? {e}" if str(e) else "  ?? formato: squadra giocata quota")
            continue
        value = odds * p - 1
        print(f"  {m['Partita']:<32} {sel.upper():<12} quota {odds:5.2f} · equa {1 / p:5.2f} · "
              f"prob {p:4.0%} · valore {value:+.1%}")
        if value > 0.10:
            print("     ^ troppo bello: ricontrolla la quota. Se è giusta, probabilmente il book sa qualcosa "
                  "(formazioni, infortuni) che le quote di ieri non sapevano.")
        new.append({"Partita": m["Partita"], "Giocata": sel.upper(), "Quota": odds,
                    "Prob": round(p, 4), "Valore": round(value, 4)})
    if new:
        book = pd.concat([book, pd.DataFrame(new)]).drop_duplicates(["Partita", "Giocata"], keep="last")
        book.to_csv(book_path, index=False)
        print(f"{len(new)} quote salvate in {book_path} (totale {len(book)}).")


def cmd_consiglia(args):
    wdir = weekend_dir(args.root, args.weekend)
    book_path = wdir / "book.csv"
    if not book_path.exists():
        sys.exit("Nessuna quota inserita: lancia prima 'python weekend.py valuta'.")
    book = pd.read_csv(book_path).sort_values("Valore", ascending=False).reset_index(drop=True)

    print(f"\nWeekend {wdir.name} · {len(book)} quote inserite, dalla migliore:\n")
    print(book.to_string(index=False, formatters={"Prob": "{:.0%}".format, "Valore": "{:+.1%}".format}))

    # Le multiple usano partite diverse (stessa partita = combo, va inserita come tale).
    cand = book.head(args.top)
    print(f"\nProposte per {args.stake:.0f} euro (dalle {len(cand)} quote migliori):")
    for legs in range(1, args.max_legs + 1):
        best = []
        for idx in itertools.combinations(cand.index, legs):
            c = cand.loc[list(idx)]
            if c["Partita"].nunique() < legs:
                continue
            q, p = c["Quota"].prod(), c["Prob"].prod()
            best.append((q * p - 1, q, p, c))
        if not best:
            continue
        ev, q, p, c = max(best, key=lambda t: t[0])
        name = {1: "Singola", 2: "Doppia", 3: "Tripla"}.get(legs, f"Multipla da {legs}")
        print(f"\n  {name}: quota {q:.2f} · vincita {args.stake * q:.2f} euro · prob {p:.0%} · "
              f"valore {ev:+.1%} ({args.stake * ev:+.2f} euro attesi)")
        for _, r in c.iterrows():
            print(f"    {r['Partita']:<32} {r['Giocata']:<12} {r['Quota']:.2f}")
    if book["Valore"].iloc[0] < 0:
        print("\nNessuna quota sopra l'equa: qualunque giocata costa in media il margine del book. "
              "Più partite metti in multipla, più margine paghi.")


# (nome, partite, quota totale minima, massima)
PROFILES = [("Singola", 1, 1.5, 1.9), ("Doppia", 2, 2.6, 3.6), ("Tripla", 3, 4.5, 7.0), ("Colpo", 4, 10.0, 18.0)]
GIORNI = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def weekend_menu(matches: pd.DataFrame, after: pd.Timestamp | None = None) -> pd.DataFrame:
    """Tutte le giocate di MENU per le partite che iniziano dopo 'after' (ora italiana)."""
    menus = []
    for _, m in matches.iterrows():
        k = kickoff_it(m)
        if after is not None and k <= after:
            continue
        menus.append(market_menu(m).assign(Partita=m["Partita"], Lega=COMPETITIONS.get(m["Div"], m["Div"]),
                                           Inizio=k, Ora=f"{GIORNI[k.weekday()]} {k:%d/%m %H:%M}"))
    return pd.concat(menus, ignore_index=True) if menus else pd.DataFrame()


def build_ticket(menu: pd.DataFrame, legs: int, lo: float, hi: float, exclude: set[str]) -> pd.DataFrame:
    """Giocate col margine più basso e quota vicina al bersaglio: partite diverse, tipi di giocata diversi."""
    target = np.sqrt(lo * hi) ** (1 / legs)  # quota per partita
    c = menu[~menu["Partita"].isin(exclude)].copy()
    c["costo"] = -c["Valore"] + np.abs(np.log(c["Quota"] / target))
    picked = []
    for _, r in c.sort_values("costo").iterrows():
        if len(picked) == legs:
            break
        if all(r["Partita"] != x["Partita"] and r["Giocata"] != x["Giocata"] for x in picked):
            picked.append(r)
    return pd.DataFrame(picked)


def cmd_proponi(args):
    wdir = weekend_dir(args.root, args.weekend)
    matches = load_matches(wdir)
    if "AvgH" not in matches:
        sys.exit("partite.csv è di una versione vecchia: rilancia 'python weekend.py lista'.")
    menu = weekend_menu(matches, after=pd.Timestamp.now() + pd.Timedelta(minutes=30))
    if menu.empty:
        sys.exit("Nessuna partita ancora da giocare in questo weekend: rilancia 'python weekend.py lista'.")
    menu = menu[(menu["Prob"] >= args.min_prob) & (menu["Prob"] <= args.max_prob)]

    print(f"\nWeekend {wdir.name} · proposte da {args.stake:.0f} euro, alternative tra loro "
          f"(partite tutte diverse: puoi anche dividere i {args.stake:.0f} euro su due).")
    print("Quote con * = stimate: gioca la partita solo se il tuo book paga almeno quella quota, "
          "altrimenti scegline un'altra (notebook, sezione 'Cerca giocate').")
    used, saved = set(), []
    for name, legs, lo, hi in PROFILES:
        t = build_ticket(menu, legs, lo, hi, used)
        if len(t) < legs:
            continue
        used |= set(t["Partita"])
        q, p = t["Quota"].prod(), t["Prob"].prod()
        ev = q * p - 1
        print(f"\n{name.upper()} · quota {q:.2f} · vincita {args.stake * q:.2f} euro · "
              f"probabilità {p:.0%} (1 volta su {1 / p:.1f}) · valore atteso {args.stake * ev:+.2f} euro")
        for _, r in t.sort_values("Inizio").iterrows():
            star = "*" if r["Stimata"] else " "
            print(f"   {r['Ora']}  {r['Lega']:<15} {r['Partita']:<30} {r['Giocata']:<9} "
                  f"quota {r['Quota']:5.2f}{star}  equa {r['Equa']:5.2f}  prob {r['Prob']:.0%}")
        saved.append(t.assign(Schedina=name, Puntata=args.stake))
    if saved:
        out = pd.concat(saved)[["Schedina", "Inizio", "Ora", "Lega", "Partita", "Giocata", "Prob", "Equa", "Quota",
                                 "Stimata", "Puntata"]]
        out.to_csv(wdir / "proposte.csv", index=False)
        print(f"\nSalvate in {wdir / 'proposte.csv'}. Il valore atteso è negativo per costruzione: "
              f"è il margine del book, che queste schedine cercano di tenere basso (poche partite, margini bassi).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("schedine"), help="cartella dei weekend salvati")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lista", help="quote eque delle prossime partite")
    p.add_argument("--start", type=dt.date.fromisoformat, default=dt.date.today(), help="AAAA-MM-GG, default oggi")
    p.add_argument("--days", type=int, default=4)
    p.add_argument("--leagues", nargs="+", default=list(COMPETITIONS), help="codici: " + " ".join(COMPETITIONS))
    p.add_argument("--source", choices=["auto", "odds-api", "football-data"], default="auto",
                   help="auto = The Odds API se c'è ODDS_API_KEY, altrimenti football-data")
    p.add_argument("--fixtures", type=Path, help="fixtures.csv locale invece del download")
    p.set_defaults(func=cmd_lista)

    p = sub.add_parser("valuta", help="inserisci le quote del tuo book")
    p.add_argument("--weekend", help="cartella del weekend (default l'ultima)")
    p.set_defaults(func=cmd_valuta)

    p = sub.add_parser("consiglia", help="giocate migliori per la puntata del weekend")
    p.add_argument("--weekend", help="cartella del weekend (default l'ultima)")
    p.add_argument("--stake", type=float, default=5.0)
    p.add_argument("--max-legs", type=int, default=3)
    p.add_argument("--top", type=int, default=10, help="quote migliori da combinare")
    p.set_defaults(func=cmd_consiglia)

    p = sub.add_parser("proponi", help="schedine proposte con quote stimate, senza inserire nulla")
    p.add_argument("--weekend", help="cartella del weekend (default l'ultima)")
    p.add_argument("--stake", type=float, default=5.0)
    p.add_argument("--min-prob", type=float, default=0.40, help="probabilità minima di ogni giocata")
    p.add_argument("--max-prob", type=float, default=0.88, help="probabilità massima (sotto quota ~1.15 non serve)")
    p.set_defaults(func=cmd_proponi)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
