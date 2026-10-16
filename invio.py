#!/usr/bin/env python3
"""
Schedine via email (Gmail SMTP), pensato per girare da cron.

    python invio.py                  # giovedì: weekend dei 6 campionati, da venerdì a lunedì
    python invio.py --modo champions # lunedì: Champions e turni infrasettimanali, da martedì a giovedì
                                     #   (niente email se non si gioca)

Se parte in ritardo (PC spento all'ora del timer) prende le partite che restano della stessa finestra:
il giovedì lanciato sabato propone sabato-lunedì; dopo la fine della finestra non fa nulla.
    python invio.py --dry-run        # scrive l'email in schedine/<data>/email.html senza inviarla
    python invio.py --allegato       # allega anche l'analisi completa (notebook in HTML)

Credenziali nel file .env del progetto (escluso da git):
    GMAIL_ADDRESS=tuoindirizzo@gmail.com
    GMAIL_APP_PASSWORD=abcdabcdabcdabcd     # password per le app di Google, non quella dell'account
    EMAIL_TO=tuoindirizzo@gmail.com         # facoltativo, default GMAIL_ADDRESS
    ODDS_API_KEY=...                        # The Odds API: quote fresche, necessaria per la Champions

Se qualcosa va storto (errore, The Odds API non raggiungibile) arriva una breve email di avviso.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import html
import io
import os
import re
import shutil
import smtplib
import subprocess
import sys
import traceback
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

import weekend
from bet_model import LEAGUES

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")  # percorso esplicito: cron parte da un'altra cartella
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO") or GMAIL_ADDRESS

# modo -> (competizioni, giorno dell'invio programmato (0 = lunedì), primo e ultimo giorno di partite
#          contati da quel giorno, partite minime per inviare)
MODES = {
    "weekend": (list(LEAGUES), 3, 1, 4, 4),               # giovedì -> venerdì..lunedì
    "champions": (["CL", *LEAGUES], 0, 1, 3, 4),          # lunedì -> martedì..giovedì
}
API_DOWN = "The Odds API non disponibile"  # messaggio di weekend.cmd_lista quando ripiega su football-data
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]


def run(func, **kwargs) -> tuple[str, bool]:
    """Esegue un comando di weekend.py catturandone l'output; False se è uscito con errore."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            func(argparse.Namespace(**kwargs))
        except SystemExit as e:
            print(e.code)
            return out.getvalue(), False
    return out.getvalue(), True


def notebook_html(wdir: Path) -> Path | None:
    """Esegue analisi.ipynb (senza codice nell'output) e lo salva come HTML nella cartella del weekend."""
    cmd = [sys.executable, "-m", "jupyter", "nbconvert", "--to", "html", "--execute", "--no-input",
           str(ROOT / "analisi.ipynb"), "--output-dir", str(wdir), "--output", "analisi"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Notebook non eseguito:\n{r.stderr[-1500:]}")
        return None
    return wdir / "analisi.html"


# ---------------------------------------------------------------- testo dell'email

def euro(x: float) -> str:
    return f"{x:.2f} €".replace(".", ",")


def describe(giocata: str, home: str, away: str) -> str:
    """'X2+U3.5' -> 'Vince Genoa o pareggio, al massimo 3 gol'."""
    words = {"1": f"vince {home}", "X": "pareggio", "2": f"vince {away}", "1X": f"vince {home} o pareggio",
             "X2": f"vince {away} o pareggio", "12": "non finisce pari", "GG": "segnano entrambe",
             "NG": "almeno una non segna"}
    parts = []
    for atom in giocata.split("+"):
        if atom in words:
            parts.append(words[atom])
        elif r := re.fullmatch(r"([OU])(\d+)\.5", atom):
            n = int(r[2])
            parts.append(f"almeno {n + 1} gol" if r[1] == "O" else ("nessun gol" if n == 0 else f"al massimo {n} gol"))
        elif r := re.fullmatch(r"MG(\d+)-(\d+)", atom):
            parts.append(f"da {r[1]} a {r[2]} gol")
        elif r := re.fullmatch(r"(\d+)-(\d+)", atom):
            parts.append(f"finisce {r[1]}-{r[2]}")
        else:
            parts.append(atom)
    text = ", ".join(parts)
    return text[0].upper() + text[1:]


def tickets(proposte: pd.DataFrame) -> list[dict]:
    out = []
    for name, t in proposte.groupby("Schedina", sort=False):
        if "Inizio" in t:
            t = t.sort_values("Inizio")
        q, p, stake = t["Quota"].prod(), t["Prob"].prod(), t["Puntata"].iloc[0]
        legs = []
        for r in t.itertuples():
            home, away = r.Partita.split(" - ", 1)
            legs.append({"ora": r.Ora, "partita": r.Partita, "lega": r.Lega, "giocata": r.Giocata,
                         "spiega": describe(r.Giocata, home, away), "quota": r.Quota, "stimata": bool(r.Stimata)})
        out.append({"nome": name, "quota": q, "prob": p, "puntata": stake, "vincita": q * stake, "legs": legs})
    return out


def period(matches: pd.DataFrame) -> str:
    days = sorted({weekend.kickoff_it(m).date() for _, m in matches.iterrows()})
    a, b = days[0], days[-1]
    g = weekend.GIORNI
    if a == b:
        return f"{g[a.weekday()]} {a.day} {MESI[a.month - 1]}"
    return f"{g[a.weekday()]} {a.day} – {g[b.weekday()]} {b.day} {MESI[b.month - 1]}"


def title_for(mode: str, divs: set[str]) -> str:
    if mode == "weekend":
        return "Schedine del weekend"
    if divs == {"CL"}:
        return "Schedine di Champions"
    return "Schedine di Champions e campionato" if "CL" in divs else "Schedine del turno infrasettimanale"


def email_text(title: str, when: str, ts: list[dict], note: str = "") -> str:
    lines = [f"{title} · {when}", "Scegli una schedina e gioca 5 euro.", ""]
    for t in ts:
        lines.append(f"{t['nome'].upper()}: quota {t['quota']:.2f}, vinci {euro(t['vincita'])}, "
                     f"probabilità {t['prob']:.0%}")
        for l in t["legs"]:
            star = "*" if l["stimata"] else ""
            lines.append(f"  {l['ora']}  {l['partita']}: {l['giocata']} ({l['spiega']}) a {l['quota']:.2f}{star}")
        lines.append("")
    lines.append("* quota stimata: gioca solo se il tuo book paga almeno questa quota.")
    if note:
        lines.append(note)
    return "\n".join(lines)


def email_html(title: str, when: str, ts: list[dict], note: str = "") -> str:
    e = html.escape
    font = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    cards = []
    for t in ts:
        rows = []
        for l in t["legs"]:
            star = '<span style="color:#9ca3af">*</span>' if l["stimata"] else ""
            rows.append(f"""
        <tr>
          <td style="padding:10px 0;border-top:1px solid #eef0f3;vertical-align:top">
            <div style="font-size:15px;font-weight:600;color:#111827">{e(l['partita'])}</div>
            <div style="font-size:13px;color:#6b7280;margin-top:2px">{e(l['ora'])} · {e(l['lega'])}</div>
            <div style="font-size:14px;color:#111827;margin-top:6px">
              <span style="display:inline-block;background:#eef2ff;color:#3730a3;font-weight:700;padding:2px 8px;border-radius:6px">{e(l['giocata'])}</span>
              <span style="color:#4b5563;margin-left:6px">{e(l['spiega'])}</span>
            </div>
          </td>
          <td style="padding:10px 0 10px 12px;border-top:1px solid #eef0f3;vertical-align:top;text-align:right;white-space:nowrap;font-size:16px;font-weight:600;color:#111827">{l['quota']:.2f}{star}</td>
        </tr>""")
        legs = len(t["legs"])
        cards.append(f"""
    <tr><td style="padding:0 24px 16px">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e5e7eb;border-radius:12px">
        <tr><td style="padding:16px 16px 4px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>
            <td style="vertical-align:top">
              <div style="font-size:18px;font-weight:700;color:#111827">{e(t['nome'])}</div>
              <div style="font-size:13px;color:#6b7280;margin-top:2px">{legs} partit{'a' if legs == 1 else 'e'} · probabilità {t['prob']:.0%}</div>
            </td>
            <td style="vertical-align:top;text-align:right;white-space:nowrap">
              <div style="font-size:13px;color:#6b7280">con 5 € vinci</div>
              <div style="font-size:22px;font-weight:700;color:#047857">{euro(t['vincita'])}</div>
              <div style="font-size:13px;color:#6b7280">quota {t['quota']:.2f}</div>
            </td>
          </tr></table>
        </td></tr>
        <tr><td style="padding:0 16px 6px">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{''.join(rows)}
          </table>
        </td></tr>
      </table>
    </td></tr>""")
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f3f4f6;{font}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f4f6">
<tr><td align="center" style="padding:24px 8px">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;background:#ffffff;border-radius:14px">
    <tr><td style="padding:24px 24px 16px">
      <div style="font-size:22px;font-weight:700;color:#111827">{e(title)}</div>
      <div style="font-size:14px;color:#6b7280;margin-top:4px">{e(when)} · scegli una schedina e gioca 5 €</div>
    </td></tr>{''.join(cards)}
    <tr><td style="padding:4px 24px 24px;font-size:12px;line-height:1.5;color:#6b7280">
      * Quota stimata: gioca la partita solo se il tuo book paga almeno questa quota.<br>
      Le probabilità vengono dalle quote dei bookmaker senza margine: in media ogni schedina rende un po' meno di quanto giochi.
      {f'<br><span style="color:#b45309">{e(note)}</span>' if note else ''}
    </td></tr>
  </table>
</td></tr>
</table>
</body></html>"""


# ---------------------------------------------------------------- invio

def send(msg: EmailMessage, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry run, non inviata] {msg['Subject']}")
        return
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD.replace(" ", ""))
        s.send_message(msg)
    print(f"Email inviata a {msg['To']}: {msg['Subject']}")


def alert(problem: str, details: str, dry_run: bool) -> None:
    """Breve email quando le schedine non partono per un motivo diverso dal calendario."""
    msg = EmailMessage()
    msg["Subject"] = f"Schedine: {problem}"
    msg["From"] = GMAIL_ADDRESS or "dry-run"
    msg["To"] = EMAIL_TO or "dry-run"
    msg.set_content(f"{problem}.\n\nUltime righe del log ({ROOT / 'logs' / 'invio.log'}):\n\n{details[-3000:]}")
    print(f"AVVISO: {problem}")
    try:
        send(msg, dry_run)
    except Exception as e:  # se non funziona nemmeno Gmail resta solo il log
        print(f"Avviso non inviato: {e}")


def window(mode: str, today: dt.date) -> tuple[dt.date, dt.date]:
    """Primo e ultimo giorno di partite per l'invio programmato più recente (oggi o nei giorni scorsi)."""
    _, weekday, first, last, _ = MODES[mode]
    scheduled = today - dt.timedelta(days=(today.weekday() - weekday) % 7)
    start, end = scheduled + dt.timedelta(days=first), scheduled + dt.timedelta(days=last)
    return max(start, today), end


def invia(args) -> None:
    leagues, *_, min_matches = MODES[args.modo]
    if args.start:
        start, end = args.start, args.start + dt.timedelta(days=MODES[args.modo][3] - MODES[args.modo][2])
    else:
        start, end = window(args.modo, dt.date.today())
    if start > end:
        print(f"Finestra {args.modo} già finita: niente da fare.")
        return
    args.start, days = start, (end - start).days + 1
    print(f"Partite dal {start} al {end}", flush=True)
    root = Path("schedine")
    wdir = root / str(args.start)
    existed = wdir.exists()
    lista, ok = run(weekend.cmd_lista, fixtures=None, source="auto", leagues=leagues, start=args.start,
                    days=days, root=root)
    api_down = API_DOWN in lista
    matches = weekend.load_matches(wdir) if ok else pd.DataFrame()
    if len(matches) < min_matches:
        print(f"{len(matches)} partite: niente da inviare.\n{lista}")
        if not existed and wdir.exists():
            shutil.rmtree(wdir)  # altrimenti diventerebbe "l'ultimo weekend" per weekend.py
        if api_down:  # forse le partite c'erano: football-data non ha la Champions
            alert("nessuna email, The Odds API non disponibile", lista, args.dry_run)
        return
    out, ok = run(weekend.cmd_proponi, root=root, weekend=str(args.start), stake=args.stake,
                  min_prob=0.40, max_prob=0.88)
    if not ok:
        raise RuntimeError(f"nessuna proposta:\n{out}")
    ts = tickets(pd.read_csv(wdir / "proposte.csv"))
    when = period(matches)
    title = title_for(args.modo, set(matches["Div"]))
    note = (f"{API_DOWN}: quote di football-data (aggiornate il martedì e il venerdì), "
            "senza Champions League." if api_down else "")

    msg = EmailMessage()
    msg["Subject"] = f"{title} · {when}"
    msg["From"] = GMAIL_ADDRESS or "dry-run"
    msg["To"] = EMAIL_TO or "dry-run"
    msg.set_content(email_text(title, when, ts, note))
    msg.add_alternative(email_html(title, when, ts, note), subtype="html")
    if args.allegato and (attachment := notebook_html(wdir)):
        msg.add_attachment(attachment.read_bytes(), maintype="text", subtype="html",
                           filename=f"analisi_{args.start}.html")
    if args.dry_run:
        (wdir / "email.html").write_text(email_html(title, when, ts, note))
        print(f"Email in {wdir / 'email.html'}\n\n{email_text(title, when, ts, note)}")
    send(msg, args.dry_run)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modo", choices=list(MODES), default="weekend")
    ap.add_argument("--start", type=dt.date.fromisoformat,
                    help="primo giorno di partite (default: la finestra del modo, vedi sopra)")
    ap.add_argument("--stake", type=float, default=5.0)
    ap.add_argument("--allegato", action="store_true", help="allega l'analisi completa del notebook")
    ap.add_argument("--dry-run", action="store_true", help="non invia nulla: scrive l'email su file")
    args = ap.parse_args()
    os.chdir(ROOT)
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] invio.py {args.modo}", flush=True)
    if not args.dry_run and not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):  # senza credenziali niente avvisi
        sys.exit(f"{ROOT / '.env'}: GMAIL_ADDRESS o GMAIL_APP_PASSWORD mancanti.")
    try:
        invia(args)
    except Exception:
        details = traceback.format_exc()
        print(details)
        alert(f"invio {args.modo} non riuscito", details, args.dry_run)
        sys.exit(1)

if __name__ == "__main__":
    main()
