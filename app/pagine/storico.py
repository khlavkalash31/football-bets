import pandas as pd
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

st.subheader("Le nostre rilevazioni del venerdì")
ours = core.query("""
    WITH p AS (SELECT DISTINCT ON (match_id) * FROM predictions WHERE model_version = 'market-v1'
               ORDER BY match_id, created_at)
    SELECT oc.competition, oc.result, p.p_home::float AS p_h, p.p_draw::float AS p_d, p.p_away::float AS p_a,
           oc.close_fair_h::float AS c_h, oc.close_fair_d::float AS c_d, oc.close_fair_a::float AS c_a
    FROM p JOIN v_open_close oc USING (match_id)
    WHERE oc.result IS NOT NULL AND oc.close_h IS NOT NULL""")
if ours.empty:
    st.info("Ancora nessuna partita rilevata con 'weekend.py lista' che abbia anche risultato e quota di chiusura. "
            "Si riempie importando i CSV di football-data dopo il weekend.")
else:
    res = pd.DataFrame({
        "Partite": [len(ours)],
        "Log loss nostra rilevazione": [core.logloss(ours[["p_h", "p_d", "p_a"]], ours["result"]).mean()],
        "Log loss chiusura": [core.logloss(ours[["c_h", "c_d", "c_a"]], ours["result"]).mean()],
    })
    st.dataframe(res, hide_index=True, column_config={"Log loss nostra rilevazione": f3, "Log loss chiusura": f3})
    st.caption("Con poche centinaia di partite la differenza è quasi tutta rumore.")
