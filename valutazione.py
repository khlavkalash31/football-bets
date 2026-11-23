"""Previsioni contro risultati reali: le metriche dietro la pagina "Verifica previsioni" dell'app
e il comando `python -m db verifica`.

Le previsioni sono quelle salvate da 'weekend.py lista' (tabella predictions, vista v_prediction_results).
Il modello 'market-v1' prende 1X2 e Under/Over 2.5 dalle quote medie del mercato e ne ricava i gol attesi;
tutte le altre giocate (GG, multigol, combo, risultato esatto) vengono dalla griglia Poisson con rho fisso.
Quindi:
- su 1X2 e U/O 2.5 il confronto giusto è con la chiusura: quanto ne sapeva il mercato in più al fischio
  d'inizio rispetto alla nostra rilevazione (formazioni, infortuni, soldi informati);
- sulle giocate derivate si misura il nostro modello dei gol: se GG o Over 1.5 escono sistematicamente
  più o meno spesso del previsto, l'errore è nostro, non del mercato.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import weekend

SELS = ["1", "X", "2", "1X", "X2", "12", "O1.5", "O2.5", "O3.5", "U2.5", "GG", "NG", "MG2-4",
        "1+O1.5", "2+O1.5", "1+GG", "2+GG"]
FLOAT_COLS = ["lam", "mu", "p_home", "p_draw", "p_away", "p_over25", "open_fair_h", "open_fair_d", "open_fair_a",
              "open_fair_over25", "close_fair_h", "close_fair_d", "close_fair_a", "close_fair_over25"]
EPS = 1e-12


def load(conn, model_version: str = "market-v1") -> pd.DataFrame:
    """Tutte le previsioni fatte prima del calcio d'inizio, giocate e non."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM v_prediction_results WHERE model_version = %s ORDER BY match_date, kickoff",
                    (model_version,))
        cols = [c.name for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    return prepare(df)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in FLOAT_COLS:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def pick(df: pd.DataFrame, quale: str = "prima") -> pd.DataFrame:
    """Una previsione per partita giocata: la prima rilevazione (quella su cui si gioca il venerdì) o l'ultima."""
    d = df[df["result"].notna()]
    d = d[d["n_rilevazione"] == 1] if quale == "prima" else d[d["n_rilevazione"] == d["n_rilevazioni"]]
    return d.reset_index(drop=True)


def _ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Log loss per riga: p (n, k) probabilità, y (n,) indice dell'esito uscito."""
    return -np.log(np.clip(p[np.arange(len(p)), y], EPS, 1))


def _brier(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    onehot = np.eye(p.shape[1])[y]
    return ((p - onehot) ** 2).sum(axis=1)


def score(d: pd.DataFrame) -> pd.DataFrame:
    """Aggiunge le metriche partita per partita (righe con risultato)."""
    d = d.copy()
    if d.empty:
        return d
    y = d["result"].map({"H": 0, "D": 1, "A": 2}).to_numpy()
    ours = d[["p_home", "p_draw", "p_away"]].to_numpy()
    ours = ours / ours.sum(axis=1, keepdims=True)
    opn = d[["open_fair_h", "open_fair_d", "open_fair_a"]].to_numpy()
    cls = d[["close_fair_h", "close_fair_d", "close_fair_a"]].to_numpy()
    d["ll"], d["brier"] = _ll(ours, y), _brier(ours, y)
    d["ll_apertura"] = np.where(np.isnan(opn).any(1), np.nan, _ll(np.nan_to_num(opn, nan=1 / 3), y))
    d["ll_chiusura"] = np.where(np.isnan(cls).any(1), np.nan, _ll(np.nan_to_num(cls, nan=1 / 3), y))
    d["brier_chiusura"] = np.where(np.isnan(cls).any(1), np.nan, _brier(np.nan_to_num(cls, nan=1 / 3), y))

    over = (d["home_goals"] + d["away_goals"] > 2.5).to_numpy()
    yo = np.where(over, 0, 1)
    po = d["p_over25"].to_numpy()
    d["ll_ou"] = _ll(np.c_[po, 1 - po], yo)
    pc = d["close_fair_over25"].to_numpy()
    d["ll_ou_chiusura"] = np.where(np.isnan(pc), np.nan, _ll(np.c_[np.nan_to_num(pc, nan=.5), 1 - np.nan_to_num(pc, nan=.5)], yo))
    # quanto si è mosso il mercato dalla nostra rilevazione alla chiusura (distanza tra le due distribuzioni 1X2)
    d["movimento"] = np.abs(ours - cls).sum(axis=1) / 2
    d["p_uscito"] = ours[np.arange(len(d)), y]
    d["partita"] = d["home_team"] + " - " + d["away_team"]
    d["risultato"] = d["home_goals"].astype(int).astype(str) + "-" + d["away_goals"].astype(int).astype(str)
    return d


def _diff(a: pd.Series, b: pd.Series) -> tuple[float, float, int]:
    """Media di a - b sulle partite dove ci sono entrambi, con mezza ampiezza dell'IC 95%."""
    x = (a - b).dropna()
    if len(x) < 2:
        return np.nan, np.nan, len(x)
    return x.mean(), 1.96 * x.std(ddof=1) / np.sqrt(len(x)), len(x)


def summary(d: pd.DataFrame) -> pd.DataFrame:
    """Log loss e Brier medi: nostra rilevazione, apertura football-data, chiusura (Pinnacle o media)."""
    rows = []
    for name, ours, opn, cls in [("Log loss 1X2", "ll", "ll_apertura", "ll_chiusura"),
                                 ("Brier 1X2", "brier", None, "brier_chiusura"),
                                 ("Log loss U/O 2.5", "ll_ou", None, "ll_ou_chiusura")]:
        diff, ci, n = _diff(d[ours], d[cls])
        rows.append({"Metrica": name, "Partite": len(d), "Nostra": d[ours].mean(),
                     "Apertura": d[opn].mean() if opn else np.nan, "Chiusura": d[cls].mean(),
                     "Nostra − chiusura": diff, "± IC 95%": ci, "Partite con chiusura": n})
    return pd.DataFrame(rows)


def by_group(d: pd.DataFrame, col: str) -> pd.DataFrame:
    g = d.groupby(col)
    out = pd.DataFrame({"Partite": g.size(), "Log loss nostra": g["ll"].mean(),
                        "Log loss chiusura": g["ll_chiusura"].mean(),
                        "Log loss U/O nostra": g["ll_ou"].mean(), "Log loss U/O chiusura": g["ll_ou_chiusura"].mean(),
                        "Movimento medio": g["movimento"].mean()})
    out["Nostra − chiusura"] = out["Log loss nostra"] - out["Log loss chiusura"]
    return out.reset_index()


def calibration(d: pd.DataFrame, step: float = 0.1) -> pd.DataFrame:
    """Esiti 1X2 raggruppati per probabilità prevista: quante volte sono usciti davvero."""
    p = np.r_[d["p_home"], d["p_draw"], d["p_away"]]
    y = np.r_[d["result"] == "H", d["result"] == "D", d["result"] == "A"].astype(float)
    esito = np.repeat(["1", "X", "2"], len(d))
    bins = np.minimum((p / step).astype(int), int(1 / step) - 1)
    t = pd.DataFrame({"bin": bins, "p": p, "y": y, "esito": esito})
    g = t.groupby("bin")
    out = pd.DataFrame({"Fascia": [f"{b * step:.0%}–{(b + 1) * step:.0%}" for b in g.size().index],
                        "Esiti": g.size().values, "Prevista": g["p"].mean().values, "Uscita": g["y"].mean().values})
    out["Scarto"] = out["Uscita"] - out["Prevista"]
    out["z"] = (g["y"].sum() - g["p"].sum()).values / np.sqrt((g["p"].apply(lambda s: (s * (1 - s)).sum())).values)
    return out


def _grids(d: pd.DataFrame, rho: float = weekend.RHO) -> np.ndarray:
    return np.stack([weekend.score_grid(lam, mu, rho) for lam, mu in zip(d["lam"], d["mu"])])


def markets(d: pd.DataFrame, sels: list[str] = SELS) -> pd.DataFrame:
    """Per ogni giocata: probabilità media prevista dal nostro modello dei gol contro frequenza reale.
    z oltre ±2 = scarto difficilmente dovuto al caso."""
    if d.empty:
        return pd.DataFrame()
    P = _grids(d)
    hg = np.clip(d["home_goals"].astype(int).to_numpy(), 0, weekend.G - 1)
    ag = np.clip(d["away_goals"].astype(int).to_numpy(), 0, weekend.G - 1)
    rows = []
    for s in sels:
        m = weekend.selection_mask(s)
        p = (P * m).sum(axis=(1, 2))
        y = m[hg, ag].astype(float)
        var = (p * (1 - p)).sum()
        rows.append({"Giocata": s, "Partite": len(d), "Prevista": p.mean(), "Uscita": y.mean(),
                     "Scarto": y.mean() - p.mean(), "z": (y.sum() - p.sum()) / np.sqrt(var) if var > 0 else np.nan,
                     "Brier": ((p - y) ** 2).mean(), "Dal mercato": s in ("1", "X", "2", "O2.5", "U2.5")})
    return pd.DataFrame(rows)


def rho_check(d: pd.DataFrame, rhos=(0.0, -0.05, -0.1, -0.15, -0.2)) -> pd.DataFrame:
    """Log loss del risultato esatto al variare di rho, a gol attesi fissi. È solo un controllo:
    con poche centinaia di partite non si cambia rho inseguendo il valore migliore."""
    if d.empty:
        return pd.DataFrame()
    hg = np.clip(d["home_goals"].astype(int).to_numpy(), 0, weekend.G - 1)
    ag = np.clip(d["away_goals"].astype(int).to_numpy(), 0, weekend.G - 1)
    rows = []
    for r in rhos:
        P = _grids(d, r)
        rows.append({"rho": r, "In uso": r == weekend.RHO,
                     "Log loss risultato esatto": -np.log(np.clip(P[np.arange(len(d)), hg, ag], EPS, 1)).mean()})
    return pd.DataFrame(rows)


def timeline(d: pd.DataFrame) -> pd.DataFrame:
    """Per settimana: log loss medio e cumulato, nostra rilevazione contro chiusura."""
    t = d.assign(settimana=pd.to_datetime(d["match_date"]).dt.to_period("W-SUN").dt.start_time)
    t = t.dropna(subset=["ll_chiusura"])
    if t.empty:
        return pd.DataFrame()
    g = t.groupby("settimana").agg(n=("ll", "size"), ll=("ll", "sum"), llc=("ll_chiusura", "sum"))
    return pd.DataFrame({"Partite": g["n"], "Nostra (settimana)": g["ll"] / g["n"],
                         "Chiusura (settimana)": g["llc"] / g["n"],
                         "Nostra (cumulato)": g["ll"].cumsum() / g["n"].cumsum(),
                         "Chiusura (cumulato)": g["llc"].cumsum() / g["n"].cumsum()})


OURS = ("p_home", "p_draw", "p_away")
CLOSE = ("close_fair_h", "close_fair_d", "close_fair_a")


def _fmt_probs(r: pd.Series, cols: tuple[str, ...]) -> str:
    return "" if r[list(cols)].isna().any() else " / ".join(f"{r[c]:.0%}" for c in cols)


def surprises(d: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Le partite in cui il risultato era meno atteso dalla nostra rilevazione."""
    t = d.nlargest(n, "ll")
    return pd.DataFrame({"Data": t["match_date"], "Competizione": t["competition"], "Partita": t["partita"],
                         "Risultato": t["risultato"], "Prob. data all'esito": t["p_uscito"],
                         "Nostra 1/X/2": t.apply(_fmt_probs, axis=1, cols=OURS),
                         "Chiusura 1/X/2": t.apply(_fmt_probs, axis=1, cols=CLOSE)})


def moves(d: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Le partite in cui il mercato si è mosso di più tra la nostra rilevazione e la chiusura: lì è arrivata
    un'informazione (formazioni, infortuni) che noi non avevamo. Se la chiusura era migliore, era un nostro errore."""
    t = d.dropna(subset=["movimento"]).nlargest(n, "movimento")
    return pd.DataFrame({"Data": t["match_date"], "Competizione": t["competition"], "Partita": t["partita"],
                         "Risultato": t["risultato"], "Movimento": t["movimento"],
                         "Nostra 1/X/2": t.apply(_fmt_probs, axis=1, cols=OURS),
                         "Chiusura 1/X/2": t.apply(_fmt_probs, axis=1, cols=CLOSE),
                         "Chi ci ha preso": np.where(t["ll"] < t["ll_chiusura"], "noi", "chiusura")})
