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

## Limiti noti

- Le neopromosse vengono ignorate finché non hanno 8 partite nella finestra di addestramento.
- Le quote "di apertura" di football-data sono rilevate il venerdì (o il martedì per le infrasettimanali),
  non sono necessariamente quelle disponibili su un book ADM.
- Nessun dato su infortuni, formazioni, xG: è una baseline, non un modello competitivo.
