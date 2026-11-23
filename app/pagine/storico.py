import streamlit as st

import core

st.title("Storico")
st.write("Quanto erano informate le quote, partita per partita. **Log loss** più basso = probabilità migliori. "
         "La chiusura (Pinnacle) è il riferimento da battere: un modello vale qualcosa solo se si avvicina "
         "alla chiusura partendo dalle quote del venerdì.")

df = core.query("""SELECT competition, season, result, open_h, open_d, open_a, close_h, close_d, close_a,
                          open_fair_h, open_fair_d, open_fair_a, close_fair_h, close_fair_d, close_fair_a
                   FROM v_open_close WHERE result IS NOT NULL AND open_h IS NOT NULL AND close_h IS NOT NULL""")
if df.empty:
    st.info("Nessuna partita giocata con quote di apertura e chiusura: lancia `python -m db import-football-data --download`.")
    st.stop()

df = df.astype({c: float for c in df.columns if c.startswith(("open", "close"))})
df["Log loss apertura"] = core.logloss(df[["open_fair_h", "open_fair_d", "open_fair_a"]], df["result"])
df["Log loss chiusura"] = core.logloss(df[["close_fair_h", "close_fair_d", "close_fair_a"]], df["result"])
df["Margine apertura"] = 1 / df["open_h"] + 1 / df["open_d"] + 1 / df["open_a"] - 1
df["Competizione"] = df["competition"].map(lambda c: core.COMPETITIONS.get(c, c))

comps = st.multiselect("Competizioni", sorted(df["Competizione"].unique()), default=sorted(df["Competizione"].unique()))
d = df[df["Competizione"].isin(comps)]
tab = (d.groupby(["Competizione", "season"])
         .agg(Partite=("result", "size"), **{"Log loss apertura": ("Log loss apertura", "mean"),
                                            "Log loss chiusura": ("Log loss chiusura", "mean"),
                                            "Margine apertura": ("Margine apertura", "mean")})
         .reset_index().rename(columns={"season": "Stagione"}).sort_values(["Competizione", "Stagione"]))
f3 = st.column_config.NumberColumn(format="%.4f")
st.dataframe(tab, hide_index=True, width="stretch", column_config={
    "Log loss apertura": f3, "Log loss chiusura": f3,
    "Margine apertura": st.column_config.NumberColumn(format="percent")})

st.caption("Le nostre rilevazioni contro i risultati veri sono nella pagina **Verifica previsioni**.")
