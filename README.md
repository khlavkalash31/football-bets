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

Storico di partite, quote e calciatori, più le rilevazioni del venerdì, in un Postgres locale.
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

### Calciatori (storico Transfermarkt)

```bash
python -m db import-transfermarkt --download   # dopo import-football-data; scarica qualche centinaio di MB
```

Importa da [transfermarkt-datasets](https://github.com/dcaribou/transfermarkt-datasets) giocatori, presenze
(minuti, gol, assist, cartellini), titolari e valori di mercato dei sei campionati dal 2016/17. Se il download
non va, si possono scaricare i CSV da Kaggle ("transfermarkt player scores") e passare la cartella con
`--dir PERCORSO`. Partite e squadre si abbinano da sole a quelle di football-data (data e risultato); se ne
abbina meno del 90% il comando lo dice. La vista `v_player_match` dà una riga per giocatore e partita, con il
valore di mercato più recente prima della partita.

Limite: il dataset non si aggiorna più da luglio 2026, quindi copre fino al 2025/26.

### Stagione in corso e xG (Understat)

```bash
python -m db import-football-data --download   # prima i risultati aggiornati
python -m db import-understat                  # poi xG, minuti e titolari della stagione in corso (o import-fotmob)
```

Da [Understat](https://understat.com) arrivano gli xG di squadra (in `matches`) e, per ogni giocatore e
partita, minuti, titolare, gol, assist, xG e xA. Copre Serie A, Liga, Premier, Ligue 1 e Bundesliga (il
Portogallo no). I giocatori si collegano a quelli di Transfermarkt per nome dentro la stessa squadra; chi non
si trova (per esempio un acquisto dell'estate) viene creato. Dove una presenza c'è già da Transfermarkt si
aggiungono solo xG e xA.

Ogni partita si scarica una sola volta (resta in `raw/understat/`), quindi rilanciarlo ogni settimana prende
solo le nuove: conviene farlo dopo l'import di football-data, prima di preparare le giocate. Con
`--from 2016` aggiunge gli xG delle stagioni passate: sono circa 19.000 pagine (più di 5 ore, una al secondo);
se lo interrompi riparte da dove era.

### xG da FotMob

```bash
python -m db migrate                           # una volta: tabelle per gli xG di più fonti
python -m db import-fotmob                     # xG, minuti e titolari della stagione in corso, tutti e 6 i campionati
```

Seconda fonte di xG, utile quando Understat è giù e per il Portogallo, che Understat non ha. Gli xG di
[FotMob](https://www.fotmob.com) vengono dal modello di Opta, diverso da quello di Understat, quindi i valori
non si mescolano: ogni fonte ha i suoi in `match_xg` e `player_match_xg` (una riga per fonte). Le colonne
`matches.home_xg/away_xg` e `player_match.xg/xa` tengono per comodità una fonte sola, Understat se c'è,
altrimenti FotMob, indicata in `xg_source`. Per un modello conviene leggere una fonte sola da `match_xg`.

FotMob non ha un'API pubblica: il comando legge gli stessi dati che usano le sue pagine, e il formato può
cambiare senza preavviso. Ogni risposta resta in `raw/fotmob/`; se FotMob non dà i dati il comando si ferma e
dice dove ha salvato la risposta. Come per Understat ogni partita si scarica una volta sola, `--from 2020`
aggiunge le stagioni passate e `--offline` usa solo i file già scaricati.

Tabelle e viste: `db/sql/`. Per leggere i dati (notebook, frontend) conviene partire dalle viste:
`v_matches` (una riga per partita), `v_odds_wide` (quote e probabilità eque per fase e bookmaker),
`v_open_close` (apertura contro chiusura, per CLV e log loss), `v_latest_snapshot`. Da pandas o Streamlit
(SQLAlchemy) l'indirizzo va scritto `postgresql+psycopg://...`.

Per spostare il database in cloud basta lo stesso `docker-compose.yml` su un altro server, oppure un Postgres
gestito: cambia solo `DATABASE_URL`.

### Verifica delle previsioni (storico contro risultati reali)

Ogni `python weekend.py lista` salva nel database le probabilità del giorno. Dopo le partite:

```bash
python -m db risultati     # risultati: CSV della stagione da football-data + Champions da The Odds API (2 crediti)
python -m db verifica      # riepilogo nel terminale (--quale ultima per l'ultima rilevazione prima del fischio)
```

`invio.py` lo fa da solo a ogni invio (giovedì e lunedì), se il database è configurato. Nell'app la pagina
**Verifica previsioni** mostra: log loss e Brier della nostra rilevazione contro apertura e chiusura (con
intervallo di confidenza del distacco), andamento settimana per settimana, risultati per competizione,
calibrazione 1X2, le giocate derivate (GG, Over 1.5, multigol, combo) prevista contro uscita con z-score,
le partite dove il mercato si è mosso di più dopo la nostra rilevazione e i risultati più inattesi.
La vista `v_prediction_results` ha una riga per previsione fatta prima del calcio d'inizio, con risultato e
probabilità eque di apertura e chiusura.

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
