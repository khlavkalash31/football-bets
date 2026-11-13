# Backtest Dixon-Coles 1X2

Serie A, LaLiga, Premier League, Ligue 1, Bundesliga, Liga Portugal.
Dati: CSV gratuiti di football-data.co.uk (risultati + quote di apertura e di chiusura).

## Avvio

```bash
pip install numpy pandas scipy matplotlib
python bet_model.py --download     # scarica stagioni 2019/20 → 2026/27 in ./data
python bet_model.py                # backtest → report.html, predictions.csv, bets.csv
```

Prova senza internet: `python bet_model.py --demo` (dati inventati, solo per verificare l'ambiente).

## Cosa fa

1. **Modello**: Dixon-Coles (Poisson con correzione per 0-0, 1-0, 0-1, 1-1), attacco e difesa per squadra,
   vantaggio casa, pesi che decadono nel tempo (emivita ~1 anno), finestra di 2 anni.
2. **Walk-forward**: per ogni giornata delle stagioni di test il modello viene riaddestrato usando solo
   le partite precedenti. Nessuna informazione dal futuro.
3. **Giocate**: si gioca l'esito con valore atteso più alto se supera la soglia (`--edge`, default 5%),
   alla quota media dei bookmaker (`--price open`), puntata fissa 1 unità.
4. **Valutazione**: log loss del modello contro quote di apertura e chiusura, ROI con intervallo di
   confidenza, e soprattutto il **CLV** (la quota presa contro la probabilità equa di chiusura Pinnacle).

## Come leggere il report

- Se il log loss del modello è peggiore di quello delle quote di **apertura**, il modello non sa niente
  che il mercato non sappia già: fermati lì e migliora il modello.
- Il ROI su qualche migliaio di giocate ha un intervallo di confidenza largo diversi punti: un +4% può
  essere solo fortuna. Il CLV medio converge molto prima ed è il segnale da guardare.
- Non scegliere la soglia di edge che massimizza il ROI nella tabella di sensibilità: è overfitting.

## Parametri utili

| Opzione | Default | Note |
|---|---|---|
| `--leagues I1 E0` | tutti e sei | codici football-data |
| `--test-seasons` | 2223 → 2627 | stagioni valutate |
| `--price max` | `open` | miglior quota sul mercato: ottimistico |
| `--edge 0.10` | 0.05 | soglia di valore atteso |
| `--max-odds` | 6.0 | evita le quote alte (longshot bias) |
| `--xi` | 0.0019 | decadimento dei pesi per giorno |

## Database (Postgres in Docker)

Storico di partite, quote e (in seguito) calciatori, più le rilevazioni del venerdì, in un Postgres locale.
Serve Docker Desktop (Windows/Mac) o Docker Engine (Linux).

```bash
pip install -r requirements.txt
cp .env.example .env               # poi scegli POSTGRES_PASSWORD e scrivila anche in DATABASE_URL
docker compose up -d               # avvia Postgres (porta 5432, solo da questo PC)
python -m db migrate               # crea le tabelle
python -m db import-football-data --download   # risultati e quote dal 2016/17, sei campionati
python -m db stato                 # quante righe ci sono
```

Da quel momento `python weekend.py lista` salva anche nel database le quote di ogni bookmaker
(The Odds API), le quote medie e le probabilità, con data e ora (`--no-db` per saltare). Se Postgres
è spento il weekend funziona lo stesso. Ogni download resta inoltre in `raw/` così come è arrivato.

Le altre fonti chiamano le squadre in modo diverso ("Inter Milan" contro "Inter"): gli abbinamenti fatti
in automatico vanno controllati con `python -m db alias` e corretti con
`python -m db alias the-odds-api "Inter Milan" Inter` (`--nuova` per una squadra che football-data non ha,
come quelle di Champions).

Tabelle e viste: `db/sql/`. Per leggere i dati (notebook, frontend) conviene partire dalle viste:
`v_matches` (una riga per partita), `v_odds_wide` (quote e probabilità eque per fase e bookmaker),
`v_open_close` (apertura contro chiusura, per CLV e log loss), `v_latest_snapshot`. Da pandas o Streamlit
(SQLAlchemy) l'indirizzo va scritto `postgresql+psycopg://...`.

Per spostare il database in cloud basta lo stesso `docker-compose.yml` su un altro server, oppure un Postgres
gestito: cambia solo `DATABASE_URL`.

## App (Streamlit)

Interfaccia web con le stesse funzioni di `weekend.py`: **Partite** (quote eque), **Valuta le mie quote**,
**Schedine proposte**, e, quando c'è il database, **Storico** (log loss apertura contro chiusura) e **Database**.

Sul PC, insieme al database:

```bash
docker compose up -d               # avvia Postgres e l'app; le tabelle si creano da sole
# poi apri http://localhost:8501
```

Senza Docker: `pip install -r requirements.txt` e `streamlit run app/app.py`.

Con il database, la pagina Partite mostra l'ultima rilevazione salvata e ha un pulsante per aggiornarla
(scarica le quote e le salva, come `weekend.py lista`). Senza database l'app scarica le quote al momento e le
tiene in memoria un'ora: è così che gira su **Streamlit Community Cloud**, per dare un link a chi non vuole
installare niente:

1. su share.streamlit.io: New app, repository `football-bets`, file `app/app.py`;
2. in Advanced settings → Secrets: `ODDS_API_KEY = "..."`;
3. rendila privata e invita solo chi deve usarla (ogni download consuma i crediti della chiave).

Il Postgres sul PC da lì non si raggiunge: l'app pubblicata mostra solo le partite dei prossimi giorni, senza
Storico. Per averlo anche online serve un database in cloud (vedi sopra) e il suo `DATABASE_URL` nei secrets.

## Limiti noti

- Le neopromosse vengono ignorate finché non hanno 8 partite nella finestra di addestramento.
- Le quote "di apertura" di football-data sono rilevate il venerdì (o il martedì per le infrasettimanali),
  non sono necessariamente quelle disponibili su un book ADM.
- Nessun dato su infortuni, formazioni, xG: è una baseline, non un modello competitivo.
