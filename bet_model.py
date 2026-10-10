#!/usr/bin/env python3
"""
Backtest walk-forward di un modello Dixon-Coles sul mercato 1X2
per i principali campionati europei, contro le quote storiche di football-data.co.uk.

Domanda a cui risponde: il modello stima le probabilità meglio del mercato?
Se sì, le sue giocate "di valore" battono la quota di chiusura (CLV > 0)?

Uso tipico (sul tuo PC, serve internet):
    pip install numpy pandas scipy matplotlib
    python bet_model.py --download            # scarica i CSV in ./data
    python bet_model.py                       # esegue il backtest e scrive report.html

Prova senza internet (dati inventati, solo per verificare che tutto giri):
    python bet_model.py --demo
"""
from __future__ import annotations

import argparse
import base64
import io
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

LEAGUES = {
    "I1": "Serie A",
    "SP1": "LaLiga",
    "E0": "Premier League",
    "F1": "Ligue 1",
    "D1": "Bundesliga",
    "P1": "Liga Portugal",
}
BASE_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"

# Colonne quote, in ordine di preferenza (football-data cambia nomi tra le stagioni)
OPEN_COLS = [("AvgH", "AvgD", "AvgA"), ("BbAvH", "BbAvD", "BbAvA"), ("B365H", "B365D", "B365A")]
MAX_COLS = [("MaxH", "MaxD", "MaxA"), ("BbMxH", "BbMxD", "BbMxA")]
CLOSE_COLS = [("PSCH", "PSCD", "PSCA"), ("AvgCH", "AvgCD", "AvgCA"), ("B365CH", "B365CD", "B365CA")]
OUTCOMES = np.array(["H", "D", "A"])


def season_codes(first: int, last: int) -> list[str]:
    """2019, 2025 -> ['1920', ..., '2526']"""
    return [f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(first, last + 1)]


# --------------------------------------------------------------------------- dati
def download(data_dir: Path, leagues: list[str], seasons: list[str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for code in leagues:
        for s in seasons:
            path = data_dir / f"{code}_{s}.csv"
            if path.exists() and s != seasons[-1]:  # l'ultima stagione si riscarica (in corso)
                continue
            url = BASE_URL.format(season=s, code=code)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    path.write_bytes(r.read())
                print(f"  ok  {code} {s}")
            except Exception as e:  # stagione non ancora disponibile, ecc.
                print(f"  --  {code} {s}: {e}")
            time.sleep(0.5)  # gentilezza verso il sito


def _fill_triplet(df: pd.DataFrame, options) -> np.ndarray:
    out = np.full((len(df), 3), np.nan)
    for cols in options:
        if not all(c in df.columns for c in cols):
            continue
        vals = df[list(cols)].apply(pd.to_numeric, errors="coerce").to_numpy(float)
        missing = np.isnan(out).any(axis=1)
        out[missing] = vals[missing]
    return out


def load(data_dir: Path, leagues: list[str]) -> pd.DataFrame:
    frames = []
    for f in sorted(data_dir.glob("*.csv")):
        parts = f.stem.split("_")
        if len(parts) != 2 or parts[0] not in leagues:
            continue
        df = pd.read_csv(f, encoding="latin-1", on_bad_lines="skip")
        if "Date" not in df.columns:
            continue
        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True, format="mixed", errors="coerce")
        df["League"], df["Season"] = parts
        frames.append(df)
    if not frames:
        sys.exit(f"Nessun CSV trovato in {data_dir}. Lancia prima con --download.")
    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    df["FTHG"] = df["FTHG"].astype(int)
    df["FTAG"] = df["FTAG"].astype(int)
    df["Res"] = np.select([df.FTHG > df.FTAG, df.FTHG == df.FTAG], [0, 1], 2)
    odds = {
        "open": _fill_triplet(df, OPEN_COLS),
        "max": _fill_triplet(df, MAX_COLS),
        "close": _fill_triplet(df, CLOSE_COLS),
    }
    for k, arr in odds.items():
        for j, o in enumerate("HDA"):
            df[f"{k}_{o}"] = arr[:, j]
    keep = ["League", "Season", "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "Res"] + [
        f"{k}_{o}" for k in odds for o in "HDA"
    ]
    return df[keep].sort_values(["League", "Date"]).reset_index(drop=True)


# --------------------------------------------------------------------------- modello
class DixonColes:
    """Dixon & Coles (1997): Poisson bivariata con correzione per i punteggi bassi
    e pesi che decadono nel tempo. log(lambda_casa) = h + att[casa] + dif[trasferta]."""

    def __init__(self, ridge: float = 0.5):
        self.ridge = ridge
        self.teams: dict[str, int] = {}
        self.theta: np.ndarray | None = None

    def fit(self, home, away, hg, ag, w, warm: dict | None = None):
        teams = sorted(set(home) | set(away))
        idx = {t: i for i, t in enumerate(teams)}
        n = len(teams)
        hi = np.array([idx[t] for t in home])
        ai = np.array([idx[t] for t in away])
        x0 = np.zeros(2 * n + 2)
        x0[2 * n], x0[2 * n + 1] = 0.25, -0.05
        if warm:
            for t, i in idx.items():
                if t in warm["teams"]:
                    x0[i], x0[n + i] = warm["teams"][t]
            x0[2 * n], x0[2 * n + 1] = warm["h"], warm["rho"]
        m00, m01 = (hg == 0) & (ag == 0), (hg == 0) & (ag == 1)
        m10, m11 = (hg == 1) & (ag == 0), (hg == 1) & (ag == 1)

        def nll(theta):
            att, dif, h, rho = theta[:n], theta[n:2 * n], theta[2 * n], theta[2 * n + 1]
            eh = h + att[hi] + dif[ai]
            ea = att[ai] + dif[hi]
            lam, mu = np.exp(eh), np.exp(ea)
            tau = np.ones_like(lam)
            tau[m00] = 1 - lam[m00] * mu[m00] * rho
            tau[m01] = 1 + lam[m01] * rho
            tau[m10] = 1 + mu[m10] * rho
            tau[m11] = 1 - rho
            tau = np.maximum(tau, 1e-10)
            g_lam = np.zeros_like(lam); g_mu = np.zeros_like(lam); g_rho = np.zeros_like(lam)
            g_lam[m00] = -mu[m00] * rho / tau[m00]
            g_mu[m00] = -lam[m00] * rho / tau[m00]
            g_rho[m00] = -lam[m00] * mu[m00] / tau[m00]
            g_lam[m01] = rho / tau[m01]; g_rho[m01] = lam[m01] / tau[m01]
            g_mu[m10] = rho / tau[m10]; g_rho[m10] = mu[m10] / tau[m10]
            g_rho[m11] = -1 / tau[m11]
            ll = w * (np.log(tau) + hg * eh - lam + ag * ea - mu)
            rh = w * (hg - lam + lam * g_lam)
            ra = w * (ag - mu + mu * g_mu)
            g_att = np.bincount(hi, rh, n) + np.bincount(ai, ra, n)
            g_dif = np.bincount(ai, rh, n) + np.bincount(hi, ra, n)
            s = att.sum()
            f = -ll.sum() + 100 * s ** 2 + self.ridge * (att @ att + dif @ dif)
            grad = -np.concatenate([g_att, g_dif, [rh.sum(), (w * g_rho).sum()]])
            grad[:n] += 200 * s + 2 * self.ridge * att
            grad[n:2 * n] += 2 * self.ridge * dif
            return f, grad

        bounds = [(None, None)] * (2 * n + 1) + [(-0.25, 0.25)]
        res = minimize(nll, x0, jac=True, method="L-BFGS-B", bounds=bounds)
        self.teams, self.theta = idx, res.x
        return self

    def state(self) -> dict:
        n = len(self.teams)
        t = self.theta
        return {"teams": {k: (t[i], t[n + i]) for k, i in self.teams.items()},
                "h": t[2 * n], "rho": t[2 * n + 1]}

    def predict_1x2(self, home, away, max_goals: int = 10) -> np.ndarray:
        n = len(self.teams)
        t = self.theta
        hi = np.array([self.teams[x] for x in home])
        ai = np.array([self.teams[x] for x in away])
        h, rho = t[2 * n], t[2 * n + 1]
        lam = np.exp(h + t[hi] + t[n + ai])
        mu = np.exp(t[ai] + t[n + hi])
        g = np.arange(max_goals + 1)
        M = poisson.pmf(g[None, :], lam[:, None])[:, :, None] * poisson.pmf(g[None, :], mu[:, None])[:, None, :]
        M[:, 0, 0] *= 1 - lam * mu * rho
        M[:, 0, 1] *= 1 + lam * rho
        M[:, 1, 0] *= 1 + mu * rho
        M[:, 1, 1] *= 1 - rho
        M /= M.sum(axis=(1, 2), keepdims=True)
        pH = np.tril(M, -1).sum(axis=(1, 2))
        pD = np.trace(M, axis1=1, axis2=2)
        pA = np.triu(M, 1).sum(axis=(1, 2))
        return np.column_stack([pH, pD, pA])


# --------------------------------------------------------------------------- backtest
def walk_forward(df: pd.DataFrame, test_seasons: list[str], xi: float, window_days: int,
                 min_matches: int) -> pd.DataFrame:
    """Per ogni giornata del periodo di test, il modello vede SOLO le partite precedenti."""
    out = []
    for code, g in df.groupby("League", sort=False):
        g = g.reset_index(drop=True)
        test_dates = np.sort(g.loc[g.Season.isin(test_seasons), "Date"].unique())
        warm = None
        t0 = time.time()
        for d in test_dates:
            tr = g[(g.Date < d) & (g.Date >= d - pd.Timedelta(days=window_days))]
            if len(tr) < 200:
                continue
            days = (d - tr.Date).dt.days.to_numpy()
            w = np.exp(-xi * days)
            model = DixonColes().fit(tr.HomeTeam.to_numpy(), tr.AwayTeam.to_numpy(),
                                     tr.FTHG.to_numpy(), tr.FTAG.to_numpy(), w, warm)
            warm = model.state()
            counts = pd.concat([tr.HomeTeam, tr.AwayTeam]).value_counts()
            te = g[g.Date == d]
            ok = te.HomeTeam.map(counts).fillna(0).ge(min_matches) & te.AwayTeam.map(counts).fillna(0).ge(min_matches)
            te = te[ok]
            if te.empty:
                continue
            p = model.predict_1x2(te.HomeTeam.to_numpy(), te.AwayTeam.to_numpy())
            te = te.copy()
            te[["p_H", "p_D", "p_A"]] = p
            out.append(te)
        print(f"  {LEAGUES.get(code, code):15s} {len(test_dates):4d} giornate  ({time.time() - t0:.0f}s)")
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def fair_probs(odds: np.ndarray) -> np.ndarray:
    inv = 1 / odds
    return inv / inv.sum(axis=1, keepdims=True)


def logloss(p: np.ndarray, res: np.ndarray) -> np.ndarray:
    return -np.log(np.clip(p[np.arange(len(res)), res], 1e-12, 1))


def make_bets(pred: pd.DataFrame, price: str, edge: float, max_odds: float) -> pd.DataFrame:
    P = pred[["p_H", "p_D", "p_A"]].to_numpy()
    O = pred[[f"{price}_{o}" for o in "HDA"]].to_numpy()
    C = pred[[f"close_{o}" for o in "HDA"]].to_numpy()
    ev = P * O - 1
    ev[(O > max_odds) | np.isnan(O)] = -np.inf
    k = np.argmax(ev, axis=1)
    rows = np.arange(len(pred))
    sel = ev[rows, k] > edge
    b = pred.loc[sel, ["League", "Season", "Date", "HomeTeam", "AwayTeam", "Res"]].copy()
    k, r = k[sel], rows[sel]
    b["pick"] = OUTCOMES[k]
    b["p_model"] = P[r, k]
    b["odds"] = O[r, k]
    b["ev"] = ev[r, k]
    b["p_close_fair"] = fair_probs(C[r])[np.arange(len(r)), k]
    b["clv"] = b.odds * b.p_close_fair - 1  # >0: hai preso una quota migliore della chiusura "equa"
    b["profit"] = np.where(b.Res.to_numpy() == k, b.odds - 1, -1.0)
    return b.reset_index(drop=True)


def summarize(b: pd.DataFrame) -> dict:
    n = len(b)
    if n == 0:
        return {"bets": 0, "roi": np.nan, "ci_lo": np.nan, "ci_hi": np.nan, "clv": np.nan, "clv_pos": np.nan, "avg_odds": np.nan}
    roi = b.profit.mean()
    se = b.profit.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    return {"bets": n, "roi": roi, "ci_lo": roi - 1.96 * se, "ci_hi": roi + 1.96 * se,
            "clv": b.clv.mean(), "clv_pos": (b.clv > 0).mean(), "avg_odds": b.odds.mean()}


# --------------------------------------------------------------------------- report
def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    return base64.b64encode(buf.getvalue()).decode()


def _pct(x, d=1):
    return "–" if pd.isna(x) else f"{x * 100:+.{d}f}%"


def build_report(pred, bets, args, out: Path, demo: bool) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    res = pred.Res.to_numpy()
    P = pred[["p_H", "p_D", "p_A"]].to_numpy()
    rows = []
    for code, g in list(pred.groupby("League")) + [("ALL", pred)]:
        r = g.Res.to_numpy()
        m = g[["p_H", "p_D", "p_A"]].to_numpy()
        o = fair_probs(g[[f"open_{x}" for x in "HDA"]].to_numpy())
        c = fair_probs(g[[f"close_{x}" for x in "HDA"]].to_numpy())
        margin = (1 / g[[f"open_{x}" for x in "HDA"]].to_numpy()).sum(axis=1).mean() - 1
        bl = bets if code == "ALL" else bets[bets.League == code]
        s = summarize(bl)
        allin = g[[f"{args.price}_{x}" for x in "HDA"]].to_numpy()
        rows.append({
            "Campionato": "Totale" if code == "ALL" else LEAGUES.get(code, code),
            "Partite": len(g),
            "Margine book": f"{margin * 100:.1f}%",
            "LogLoss modello": f"{logloss(m, r).mean():.4f}",
            "LogLoss apertura": f"{logloss(o, r).mean():.4f}",
            "LogLoss chiusura": f"{logloss(c, r).mean():.4f}",
            "Giocate": s["bets"],
            "Quota media": "–" if pd.isna(s["avg_odds"]) else f"{s['avg_odds']:.2f}",
            "ROI": _pct(s["roi"]),
            "IC 95% ROI": "–" if pd.isna(s["ci_lo"]) else f"{_pct(s['ci_lo'])} / {_pct(s['ci_hi'])}",
            "CLV medio": _pct(s["clv"], 2),
            "Giocate con CLV>0": "–" if pd.isna(s["clv_pos"]) else f"{s['clv_pos'] * 100:.0f}%",
            "ROI 'gioca tutto'": _pct(np.nanmean(np.where(np.arange(3)[None, :] == r[:, None], allin - 1, -1))),
        })
    table = pd.DataFrame(rows)

    # sensibilità alla soglia di edge
    sens = []
    for e in [0.0, 0.02, 0.05, 0.10, 0.15, 0.20]:
        s = summarize(make_bets(pred, args.price, e, args.max_odds))
        sens.append({"Soglia edge": f"{e * 100:.0f}%", "Giocate": s["bets"], "ROI": _pct(s["roi"]),
                     "IC 95%": "–" if pd.isna(s["ci_lo"]) else f"{_pct(s['ci_lo'])} / {_pct(s['ci_hi'])}",
                     "CLV medio": _pct(s["clv"], 2)})
    sens = pd.DataFrame(sens)

    # grafico profitto cumulato
    fig, ax = plt.subplots(figsize=(9, 4))
    for code, g in bets.groupby("League"):
        c = g.groupby("Date").profit.sum().cumsum()
        ax.plot(c.index, c.values, lw=1, alpha=0.7, label=LEAGUES.get(code, code))
    c = bets.groupby("Date").profit.sum().cumsum()
    ax.plot(c.index, c.values, lw=2.2, color="black", label="Totale")
    ax.axhline(0, color="grey", lw=0.8)
    ax.set_ylabel("Profitto cumulato (unità, puntata 1)")
    ax.legend(fontsize=8, ncol=4, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    img_profit = _png(fig); plt.close(fig)

    # grafico calibrazione
    fig, ax = plt.subplots(figsize=(4.5, 4.5))
    pf, yf = P.ravel(), (np.arange(3)[None, :] == res[:, None]).ravel()
    bins = np.linspace(0, 1, 11)
    idx = np.digitize(pf, bins) - 1
    xs = [pf[idx == i].mean() for i in range(10) if (idx == i).sum() > 30]
    ys = [yf[idx == i].mean() for i in range(10) if (idx == i).sum() > 30]
    ax.plot([0, 1], [0, 1], color="grey", lw=0.8, ls="--")
    ax.plot(xs, ys, "o-", color="black")
    ax.set_xlabel("Probabilità stimata dal modello"); ax.set_ylabel("Frequenza osservata")
    ax.spines[["top", "right"]].set_visible(False)
    img_cal = _png(fig); plt.close(fig)

    total = summarize(bets)
    beats_close = logloss(P, res).mean() < logloss(fair_probs(pred[[f"close_{x}" for x in "HDA"]].to_numpy()), res).mean()
    if total["bets"] and total["clv"] > 0 and total["ci_lo"] > 0:
        verdict = "Il modello mostra un vantaggio: CLV positivo e ROI con intervallo sopra zero. Va confermato con il paper trading prima di metterci soldi."
    elif total["bets"] and total["clv"] > 0:
        verdict = "CLV medio positivo ma ROI non distinguibile da zero: segnale debole, serve più campione e il paper trading."
    else:
        verdict = "Nessun vantaggio dimostrato: le giocate non battono la quota di chiusura. Con questo modello, nel lungo periodo si perde circa il margine del bookmaker."

    banner = ('<div class="demo">DATI SINTETICI: questo report è generato con --demo e non dice nulla '
              'sul mercato reale. Serve solo a verificare che la pipeline funzioni.</div>') if demo else ""
    html = f"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Backtest Dixon-Coles</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#e3e3e3;--acc:#0b5;}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16171a;--fg:#ececec;--mut:#9a9a9a;--line:#33353a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:24px 16px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:18px;margin:32px 0 8px}}
p.sub{{color:var(--mut);margin:0 0 16px}}
.verdict{{border-left:4px solid var(--fg);padding:10px 14px;margin:16px 0;background:rgba(127,127,127,.08)}}
.demo{{background:#c62828;color:#fff;padding:10px 14px;border-radius:6px;margin:12px 0;font-weight:600}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;font-size:13px;white-space:nowrap}}
th,td{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:right}} th:first-child,td:first-child{{text-align:left}}
tr:last-child td{{font-weight:600}} img{{max-width:100%;background:#fff;border-radius:6px}}
.grid{{display:grid;grid-template-columns:2fr 1fr;gap:16px;align-items:start}} @media(max-width:800px){{.grid{{grid-template-columns:1fr}}}}
ul{{color:var(--mut);font-size:14px}}
</style></head><body>
<h1>Backtest Dixon-Coles · 1X2</h1>
<p class="sub">Stagioni di test: {", ".join(args.test_seasons)} · prezzo: {args.price} · soglia edge {args.edge:.0%} · quota max {args.max_odds} · puntata fissa 1 unità</p>
{banner}
<div class="verdict">{verdict}<br><small>Previsioni migliori della chiusura (log loss)? {"Sì" if beats_close else "No"}.</small></div>
<h2>Risultati per campionato</h2><div class="wrap">{table.to_html(index=False, border=0)}</div>
<ul>
<li><b>LogLoss</b>: più basso è meglio. Se il modello non batte almeno le quote di apertura, non ha informazione che il mercato non abbia già.</li>
<li><b>CLV</b> (closing line value): quanto la quota presa è migliore della probabilità "equa" di chiusura. È la metrica più affidabile: il ROI su poche centinaia di giocate è quasi tutto rumore.</li>
<li><b>ROI 'gioca tutto'</b>: puntare su tutti gli esiti di tutte le partite. È il costo del margine del bookmaker, il riferimento da battere.</li>
</ul>
<div class="grid"><div><h2>Profitto cumulato</h2><img src="data:image/png;base64,{img_profit}"></div>
<div><h2>Calibrazione</h2><img src="data:image/png;base64,{img_cal}"></div></div>
<h2>Sensibilità alla soglia di edge</h2><div class="wrap">{sens.to_html(index=False, border=0)}</div>
<ul><li>Non scegliere la soglia che dà il ROI migliore qui: sarebbe overfitting sul periodo di test. Se il vantaggio è reale, il CLV dovrebbe crescere con la soglia in modo regolare.</li></ul>
</body></html>"""
    out.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------- demo
def make_demo(data_dir: Path, leagues: list[str], seasons: list[str], seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    data_dir.mkdir(parents=True, exist_ok=True)
    for code in leagues:
        teams = [f"{code}_Team{i:02d}" for i in range(20)]
        att = rng.normal(0, 0.25, 20); att -= att.mean()
        dif = rng.normal(0, 0.2, 20)
        start = pd.Timestamp("2019-08-17")
        for si, s in enumerate(seasons):
            att += rng.normal(0, 0.05, 20); att -= att.mean()
            rows = []
            pairs = [(i, j) for i in range(20) for j in range(20) if i != j]
            rng.shuffle(pairs)
            day0 = start + pd.Timedelta(days=365 * si)
            for k, (i, j) in enumerate(pairs):
                lam, mu = np.exp(0.25 + att[i] + dif[j]), np.exp(att[j] + dif[i])
                hg, ag = rng.poisson(lam), rng.poisson(mu)
                p = DixonColes._true(lam, mu)
                q_open = p * np.exp(rng.normal(0, 0.10, 3)); q_open /= q_open.sum()
                q_close = p * np.exp(rng.normal(0, 0.04, 3)); q_close /= q_close.sum()
                o_avg = 1 / (q_open * 1.06); o_max = o_avg * rng.uniform(1.02, 1.06, 3)
                o_close = 1 / (q_close * 1.025)
                rows.append({"Div": code, "Date": (day0 + pd.Timedelta(days=7 * (k // 10))).strftime("%d/%m/%Y"),
                             "HomeTeam": teams[i], "AwayTeam": teams[j], "FTHG": hg, "FTAG": ag,
                             "AvgH": o_avg[0], "AvgD": o_avg[1], "AvgA": o_avg[2],
                             "MaxH": o_max[0], "MaxD": o_max[1], "MaxA": o_max[2],
                             "PSCH": o_close[0], "PSCD": o_close[1], "PSCA": o_close[2]})
            pd.DataFrame(rows).round(2).to_csv(data_dir / f"{code}_{s}.csv", index=False)


def _true(lam, mu, G=10):
    g = np.arange(G + 1)
    M = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
    return np.array([np.tril(M, -1).sum(), np.trace(M), np.triu(M, 1).sum()]) / M.sum()


DixonColes._true = staticmethod(_true)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default="data", type=Path)
    ap.add_argument("--leagues", nargs="+", default=list(LEAGUES))
    ap.add_argument("--first-season", type=int, default=2019, help="anno di inizio della prima stagione scaricata")
    ap.add_argument("--last-season", type=int, default=2026, help="anno di inizio dell'ultima stagione (2026 = 26/27)")
    ap.add_argument("--test-seasons", nargs="+", default=["2223", "2324", "2425", "2526", "2627"])
    ap.add_argument("--download", action="store_true", help="scarica i CSV da football-data.co.uk ed esce")
    ap.add_argument("--demo", action="store_true", help="usa dati sintetici (nessun download)")
    ap.add_argument("--price", choices=["open", "max"], default="open",
                    help="open = quota media dei book (realistica); max = miglior quota sul mercato (ottimistica)")
    ap.add_argument("--edge", type=float, default=0.05, help="valore atteso minimo per giocare (0.05 = +5%%)")
    ap.add_argument("--max-odds", type=float, default=6.0)
    ap.add_argument("--xi", type=float, default=0.0019, help="decadimento dei pesi per giorno (emivita ~1 anno)")
    ap.add_argument("--window-days", type=int, default=730)
    ap.add_argument("--min-matches", type=int, default=8)
    ap.add_argument("--out", default="report.html", type=Path)
    args = ap.parse_args()
    seasons = season_codes(args.first_season, args.last_season)

    if args.download:
        print(f"Scarico {len(args.leagues)} campionati × {len(seasons)} stagioni in {args.data_dir}/")
        download(args.data_dir, args.leagues, seasons)
        return
    if args.demo:
        args.data_dir = Path("data_demo")
        seasons = season_codes(2019, 2024)
        args.test_seasons = ["2223", "2324", "2425"]
        make_demo(args.data_dir, args.leagues, seasons)

    df = load(args.data_dir, args.leagues)
    print(f"Caricate {len(df):,} partite. Backtest walk-forward:")
    pred = walk_forward(df, args.test_seasons, args.xi, args.window_days, args.min_matches)
    pred = pred.dropna(subset=[f"open_{x}" for x in "HDA"] + [f"close_{x}" for x in "HDA"]).reset_index(drop=True)
    if pred.empty:
        sys.exit("Nessuna previsione con quote di apertura e chiusura disponibili.")
    bets = make_bets(pred, args.price, args.edge, args.max_odds)
    pred.to_csv("predictions.csv", index=False)
    bets.to_csv("bets.csv", index=False)
    build_report(pred, bets, args, args.out, args.demo)
    s = summarize(bets)
    print(f"\n{s['bets']} giocate · ROI {_pct(s['roi'])} (IC95 {_pct(s['ci_lo'])} / {_pct(s['ci_hi'])}) · CLV medio {_pct(s['clv'], 2)}")
    print(f"Report: {args.out.resolve()}")


if __name__ == "__main__":
    main()
