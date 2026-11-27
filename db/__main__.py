"""Comandi del database.

    docker compose up -d                          # avvia Postgres
    python -m db migrate                          # crea/aggiorna tabelle e viste
    python -m db import-football-data --download  # scarica 2016/17 -> oggi e importa risultati e quote
    python -m db import-football-data data/I1_2526.csv   # importa CSV già scaricati
    python -m db alias                            # nomi squadra abbinati in automatico, da controllare
    python -m db alias the-odds-api "Inter Milan" Inter  # corregge (o conferma) un abbinamento
    python -m db alias the-odds-api "Club Brugge" "Club Brugge" --nuova  # squadra che football-data non ha
    python -m db import-transfermarkt --download  # giocatori, presenze, titolari, valori (fino al 2025/26)
    python -m db risultati                        # risultati delle partite previste (football-data + Champions)
    python -m db verifica                         # previsioni salvate contro risultati reali
    python -m db import-understat                 # stagione in corso da Understat: xG, minuti, titolari
    python -m db import-understat --from 2016     # anche le passate (lungo: migliaia di pagine, riprende da dove era)
    python -m db import-fotmob                    # stagione in corso da FotMob: xG (Opta), minuti, titolari, anche P1
    python -m db stato                            # righe per tabella
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import store
from .config import ROOT, SQL_DIR, connect, database_url


def cmd_migrate(args):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, "
                    "applied_at timestamptz NOT NULL DEFAULT now())")
        cur.execute("SELECT name FROM schema_migrations")
        done = {r[0] for r in cur.fetchall()}
        todo = [p for p in sorted(SQL_DIR.glob("*.sql")) if p.name not in done]
        for p in todo:
            cur.execute(p.read_text())
            cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (p.name,))
            print(f"  applicata {p.name}")
        conn.commit()
    print("Schema aggiornato." if todo else "Schema già aggiornato.")


def cmd_import_football_data(args):
    from bet_model import download, season_codes

    from .football_data import import_csv

    data_dir = ROOT / "data"
    if args.download:
        today = dt.date.today()
        last = today.year if today.month >= 7 else today.year - 1
        download(data_dir, args.leagues, season_codes(args.first_season, last))
    files = args.files or sorted(f for f in data_dir.glob("*.csv") if f.name.split("_")[0] in args.leagues)
    if not files:
        sys.exit(f"Nessun CSV in {data_dir}: usa --download o passa i file.")
    with connect() as conn:
        for f in files:
            n, o = import_csv(conn, f)
            print(f"  {f.name:<14} {n:4d} partite, {o:6d} quote")
    print(f"Importati {len(files)} file.")


def cmd_import_transfermarkt(args):
    from . import transfermarkt as tm

    if args.dir:
        files = tm.read_local(args.dir)
    elif args.download:
        files = tm.download()
    else:
        files = tm.latest_files()
    with connect() as conn:
        stats = tm.import_all(conn, files, args.first_season)
    for k, v in stats.items():
        print(f"  {k:<22} {v:>9}")
    if stats["partite Transfermarkt"] and stats["partite abbinate"] < 0.9 * stats["partite Transfermarkt"]:
        print("Meno del 90% delle partite abbinate: mancano stagioni di football-data? "
              "(python -m db import-football-data --download)")


def cmd_import_understat(args):
    from . import understat as us
    _import_xg(args, us, "Understat")


def cmd_import_fotmob(args):
    from . import fotmob
    _import_xg(args, fotmob, "FotMob")


def _import_xg(args, us, label: str):
    """Import stagione per stagione da una fonte di xG (moduli understat e fotmob: stessa interfaccia)."""
    last = us.current_season()
    seasons = range(args.first_season if args.first_season is not None else last, last + 1)
    client = None if args.offline else us.Client()
    leagues = [c for c in args.leagues if c in us.LEAGUES]
    print(f"{'':<4} {'stag.':<6} {'giocate':>7} {'abbinate':>8} {'con xG':>7} {'con gioc.':>9} {'collegati':>9} "
          f"{'nuovi':>6}")
    low = []
    with connect() as conn:
        for season in seasons:
            for comp in leagues:
                try:
                    s = us.import_season(conn, client, comp, season)
                except ValueError as e:
                    conn.rollback()
                    print(f"{comp:<4} {season:<6} {e}")
                    continue
                except us.SiteError as e:  # sito giù: inutile continuare con gli altri campionati
                    conn.rollback()
                    print(f"{comp:<4} {season:<6} {e}")
                    return
                except Exception as e:  # rete, sito cambiato...: si passa alla stagione successiva
                    conn.rollback()
                    print(f"{comp:<4} {season:<6} errore {type(e).__name__}: {e}")
                    continue
                print(f"{comp:<4} {season:<6} {s['partite giocate']:>7} {s['abbinate']:>8} "
                      f"{s.get('con xG', s['abbinate']):>7} {s['con giocatori']:>9} "
                      f"{s['giocatori collegati']:>9} {s['giocatori nuovi']:>6}")
                if s["partite giocate"] and s["abbinate"] < 0.9 * s["partite giocate"]:
                    low.append(f"{comp} {season}")
    if low:
        print(f"Meno del 90% delle partite abbinate in {', '.join(low)}: aggiorna prima i risultati "
              "(python -m db import-football-data --download).")
    missing = [c for c in args.leagues if c not in us.LEAGUES]
    if missing:
        print(f"{', '.join(missing)}: {label} non li copre.")


def cmd_alias(args):
    with connect() as conn, conn.cursor() as cur:
        if args.source:
            cur.execute("SELECT team_id FROM teams WHERE name = %s", (args.team,))
            row = cur.fetchone()
            if row is None and args.nuova:
                cur.execute("INSERT INTO teams (name) VALUES (%s) RETURNING team_id", (args.team,))
                row = cur.fetchone()
            if row is None:
                sys.exit(f"Squadra '{args.team}' non trovata: usa il nome di football-data, "
                         f"o aggiungi --nuova se è una squadra che lì non c'è.")
            new = row[0]
            cur.execute("SELECT team_id FROM team_aliases WHERE source = %s AND alias = %s", (args.source, args.alias))
            old = (cur.fetchone() or [None])[0]
            cur.execute("""INSERT INTO team_aliases (source, alias, team_id, confirmed) VALUES (%s, %s, %s, true)
                           ON CONFLICT (source, alias) DO UPDATE SET team_id = EXCLUDED.team_id, confirmed = true""",
                        (args.source, args.alias, new))
            if old is not None and old != new:
                cur.execute("SELECT count(*) FROM team_aliases WHERE team_id = %s", (old,))
                if cur.fetchone()[0] == 0:  # squadra creata solo per questo nome: si unisce a quella giusta
                    store.merge_team(cur, old, new)
            conn.commit()
            print(f"{args.source} '{args.alias}' -> {args.team}")
            return
        cur.execute("""SELECT a.source, a.alias, t.name,
                              NOT EXISTS (SELECT 1 FROM team_aliases f WHERE f.team_id = t.team_id
                                          AND f.source = 'football-data') AS new_team
                       FROM team_aliases a JOIN teams t USING (team_id)
                       WHERE NOT a.confirmed ORDER BY a.source, t.name""")
        rows = cur.fetchall()
    if not rows:
        print("Nessun abbinamento da controllare.")
        return
    print("Abbinamenti automatici da controllare (fonte, nome nella fonte -> squadra):")
    for s, a, t, new_team in rows:
        flag = "   (squadra nuova: non c'è in football-data)" if new_team else ""
        print(f"  {s:<14} {a:<28} -> {t}{flag}")
    print("\nSe uno è sbagliato: python -m db alias FONTE \"NOME NELLA FONTE\" \"NOME FOOTBALL-DATA\"")


def cmd_risultati(args):
    import weekend

    from .risultati import update_all

    key = weekend.load_config().get("ODDS_API_KEY")
    with connect() as conn:
        update_all(conn, args.leagues, args.odds_api, key)


def cmd_verifica(args):
    import pandas as pd

    import valutazione as v

    with connect() as conn:
        df = v.load(conn, args.model)
    d = v.score(v.pick(df, args.quale))
    if d.empty:
        sys.exit("Nessuna previsione con risultato: lancia prima 'python weekend.py lista' e, dopo le partite, "
                 "'python -m db risultati'.")
    print(f"{len(d)} partite, {args.quale} rilevazione, dal {d['match_date'].min():%d/%m/%Y} al {d['match_date'].max():%d/%m/%Y}\n")
    with pd.option_context("display.width", 160, "display.float_format", "{:.4f}".format):
        print(v.summary(d).to_string(index=False))
        print("\nGiocate (prevista dal modello dei gol contro uscita davvero; |z| > 2 = scarto sospetto)")
        print(v.markets(d).to_string(index=False))


def cmd_stato(args):
    with connect() as conn, conn.cursor() as cur:
        print(f"Database: {database_url().split('@')[-1]}")
        for t in ["competitions", "teams", "team_aliases", "matches", "odds_snapshots", "predictions",
                  "players", "player_match", "player_value", "predicted_lineups", "raw_files"]:
            cur.execute(f"SELECT count(*) FROM {t}")
            print(f"  {t:<18} {cur.fetchone()[0]:>9}")
        cur.execute("""SELECT competition, min(season), max(season), count(*) FROM matches
                       WHERE home_goals IS NOT NULL GROUP BY 1 ORDER BY 1""")
        for c, a, b, n in cur.fetchall():
            print(f"  {c:<4} stagioni {a}-{b}: {n} partite giocate")
        cur.execute("SELECT to_regclass('match_xg') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("""SELECT x.source, m.competition, m.season, count(*) FROM match_xg x JOIN matches m USING (match_id)
                           GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")
            for src, c, s, n in cur.fetchall():
                print(f"  xG {src:<9} {c:<4} {s}: {n} partite")


def main():
    from bet_model import LEAGUES

    ap = argparse.ArgumentParser(prog="python -m db", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="crea o aggiorna lo schema").set_defaults(func=cmd_migrate)

    p = sub.add_parser("import-football-data", help="risultati e quote storiche da football-data.co.uk")
    p.add_argument("files", nargs="*", type=Path, help="CSV da importare (default tutti quelli in data/)")
    p.add_argument("--download", action="store_true", help="scarica prima i CSV in data/")
    p.add_argument("--first-season", type=int, default=2016, help="prima stagione da scaricare (2016 = 2016/17)")
    p.add_argument("--leagues", nargs="+", default=list(LEAGUES))
    p.set_defaults(func=cmd_import_football_data)

    p = sub.add_parser("import-transfermarkt", help="storico calciatori da transfermarkt-datasets")
    p.add_argument("--download", action="store_true", help="scarica i file (qualche centinaio di MB)")
    p.add_argument("--dir", type=Path, help="cartella con i CSV già scaricati (es. da Kaggle)")
    p.add_argument("--first-season", type=int, default=2016, help="prima stagione (2016 = 2016/17)")
    p.set_defaults(func=cmd_import_transfermarkt)

    p = sub.add_parser("import-understat", help="xG e presenze da Understat (default: stagione in corso)")
    p.add_argument("--from", dest="first_season", type=int, help="prima stagione (2016 = 2016/17)")
    p.add_argument("--leagues", nargs="+", default=list(LEAGUES))
    p.add_argument("--offline", action="store_true", help="usa solo i file già scaricati in raw/understat")
    p.set_defaults(func=cmd_import_understat)

    p = sub.add_parser("import-fotmob", help="xG e presenze da FotMob, tutti e 6 i campionati (default: stagione in corso)")
    p.add_argument("--from", dest="first_season", type=int, help="prima stagione (2020 = 2020/21)")
    p.add_argument("--leagues", nargs="+", default=list(LEAGUES))
    p.add_argument("--offline", action="store_true", help="usa solo i file già scaricati in raw/fotmob")
    p.set_defaults(func=cmd_import_fotmob)

    p = sub.add_parser("alias", help="controlla o correggi i nomi squadra delle altre fonti")
    p.add_argument("source", nargs="?", help="fonte, es. the-odds-api")
    p.add_argument("alias", nargs="?", help="nome nella fonte")
    p.add_argument("team", nargs="?", help="nome della squadra (quello di football-data)")
    p.add_argument("--nuova", action="store_true", help="crea la squadra se non esiste (es. squadre di Champions)")
    p.set_defaults(func=cmd_alias)

    p = sub.add_parser("risultati", help="risultati finali delle partite su cui c'è una previsione")
    p.add_argument("--leagues", nargs="+", default=list(LEAGUES), help="campionati da football-data")
    p.add_argument("--odds-api", nargs="*", default=["CL"],
                   help="competizioni da The Odds API (2 crediti l'una, ultimi 3 giorni); default CL")
    p.set_defaults(func=cmd_risultati)

    p = sub.add_parser("verifica", help="previsioni salvate contro risultati reali")
    p.add_argument("--quale", choices=["prima", "ultima"], default="prima", help="rilevazione da valutare")
    p.add_argument("--model", default="market-v1")
    p.set_defaults(func=cmd_verifica)

    sub.add_parser("stato", help="righe per tabella").set_defaults(func=cmd_stato)

    args = ap.parse_args()
    if args.cmd == "alias" and args.source and not args.team:
        ap.error("alias: servono FONTE, NOME NELLA FONTE e SQUADRA")
    args.func(args)


if __name__ == "__main__":
    main()
