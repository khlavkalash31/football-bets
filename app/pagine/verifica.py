import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

import core
import valutazione as v

st.title("Verifica previsioni")
st.write("Le probabilità salvate da `weekend.py lista` confrontate con i risultati veri, per costruire lo storico "
         "e capire dove sbagliamo. **Log loss** e **Brier** più bassi = previsioni migliori. Il riferimento è la "
         "**chiusura** (Pinnacle): la differenza tra noi e la chiusura è quello che il mercato ha saputo dopo la "
         "nostra rilevazione (formazioni, infortuni).")

df = v.prepare(core.query("SELECT * FROM v_prediction_results WHERE model_version = %s ORDER BY match_date, kickoff",
                          ("market-v1",)))
if df.empty:
    st.info("Nessuna previsione salvata: si riempie da sola con `python weekend.py lista` (o con *Aggiorna le quote* "
            "nella pagina Partite) se il database è configurato.")
    st.stop()

s = core.settings()
c1, c2, c3 = st.columns([1, 1, 1])
quale = c1.radio("Rilevazione da valutare", ["prima", "ultima"], horizontal=True,
                 help="Prima = quella su cui si gioca (di solito il venerdì). Ultima = la più vicina al calcio d'inizio.")
periodo = c2.selectbox("Periodo", ["Tutto", "Ultimi 30 giorni", "Ultimi 90 giorni"])
df = df[df["competition"].isin(s["leagues"])]
if periodo != "Tutto":
    since = dt.date.today() - dt.timedelta(days=30 if periodo == "Ultimi 30 giorni" else 90)
    df = df[pd.to_datetime(df["match_date"]).dt.date >= since]

matches = df.drop_duplicates("match_id")
kick = pd.to_datetime(matches["kickoff"], utc=True).fillna(pd.to_datetime(matches["match_date"]).dt.tz_localize("UTC")
                                                             + pd.Timedelta(days=1))
finite_senza = int((matches["result"].isna() & (kick < pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=3))).sum())
future = int((matches["result"].isna()).sum()) - finite_senza
d = v.score(v.pick(df, quale))

with c3:
    if not core.read_only() and st.button(
            "Aggiorna risultati", help="Scarica i CSV della stagione da football-data e i risultati di "
                                       "Champions da The Odds API (2 crediti)."):
        import db
        import weekend
        from db.risultati import update_all

        msgs: list[str] = []
        with st.spinner("Scarico i risultati..."), db.connect() as conn:
            update_all(conn, list(weekend.LEAGUES), ["CL"], core.odds_key(), log=msgs.append)
        core.query.clear()
        st.session_state["msg_risultati"] = msgs
        st.rerun()
for m in st.session_state.pop("msg_risultati", []):
    st.caption(m)

st.caption(f"{len(d)} partite con risultato · {future} da giocare"
           + (f" · **{finite_senza} finite senza risultato** (football-data si aggiorna due volte a settimana; "
              f"se restano, controlla gli alias nella pagina Database)" if finite_senza else ""))
if d.empty:
    st.info("Ancora nessuna partita prevista con risultato. Dopo il weekend: *Aggiorna risultati* qui sopra "
            "(o `python -m db risultati` dal terminale).")
    st.stop()

# ------------------------------------------------------------------ riepilogo
summ = v.summary(d)
ll = summ.iloc[0]
m1, m2, m3 = st.columns(3)
m1.metric("Log loss 1X2 nostra", f"{ll['Nostra']:.4f}")
m2.metric("Log loss 1X2 chiusura", f"{ll['Chiusura']:.4f}" if pd.notna(ll["Chiusura"]) else "—")
if pd.notna(ll["Nostra − chiusura"]):
    m3.metric("Distacco dalla chiusura", f"{ll['Nostra − chiusura']:+.4f}",
              help=f"Nostra meno chiusura, ± {ll['± IC 95%']:.4f} (intervallo di confidenza al 95%). "
                   "Positivo = la chiusura prevedeva meglio.")
    lo, hi = ll["Nostra − chiusura"] - ll["± IC 95%"], ll["Nostra − chiusura"] + ll["± IC 95%"]
    if lo > 0:
        st.write("La chiusura è **significativamente migliore** della nostra rilevazione: tra il momento in cui "
                 "rileviamo e il fischio d'inizio arrivano informazioni che non abbiamo. È il margine che un modello "
                 "con le probabili formazioni può provare a recuperare.")
    elif hi < 0:
        st.write("La nostra rilevazione è **migliore della chiusura**: con questi numeri sarebbe sorprendente, "
                 "controlla che le quote di chiusura siano abbinate alle partite giuste.")
    else:
        st.write("La differenza con la chiusura **non si distingue dal caso** con queste partite: serve più storico.")

f4 = st.column_config.NumberColumn(format="%.4f")
st.dataframe(summ, hide_index=True, width="stretch", column_config={
    c: f4 for c in ["Nostra", "Apertura", "Chiusura", "Nostra − chiusura", "± IC 95%"]})
st.caption("Apertura = quote medie di football-data (del venerdì). Il ROI non c'è di proposito: su qualche centinaio "
           "di partite è quasi tutto rumore, log loss e distacco dalla chiusura convergono molto prima.")

# ------------------------------------------------------------------ nel tempo e per competizione
st.subheader("Andamento")
tl = v.timeline(d)
if len(tl) >= 2:
    long = tl[["Nostra (cumulato)", "Chiusura (cumulato)"]].reset_index().melt("settimana", var_name="Serie",
                                                                              value_name="Log loss")
    st.altair_chart(alt.Chart(long).mark_line(point=True).encode(
        x=alt.X("settimana:T", title="Settimana"),
        y=alt.Y("Log loss:Q", title="Log loss 1X2 cumulato", scale=alt.Scale(zero=False)),
        color=alt.Color("Serie:N", title=None, legend=alt.Legend(orient="bottom"))), width="stretch")
    with st.expander("Settimana per settimana"):
        st.dataframe(tl.reset_index().rename(columns={"settimana": "Settimana"}), hide_index=True, width="stretch",
                     column_config={c: f4 for c in tl.columns if c != "Partite"})
else:
    st.caption("Il grafico compare con almeno due settimane di partite con quota di chiusura.")

grp = v.by_group(d, "competition")
grp["competition"] = grp["competition"].map(lambda c: core.COMPETITIONS.get(c, c))
st.dataframe(grp.rename(columns={"competition": "Competizione"}), hide_index=True, width="stretch",
             column_config={c: f4 for c in grp.columns if c not in ("competition", "Partite")}
             | {"Movimento medio": st.column_config.NumberColumn(format="percent")})
st.caption("Movimento = quanta probabilità si è spostata tra la nostra rilevazione e la chiusura. "
           "La Champions non ha chiusura (football-data non la copre).")

# ------------------------------------------------------------------ calibrazione
st.subheader("Calibrazione 1X2")
st.write("Gli esiti a cui davamo, per esempio, il 40-50% sono usciti davvero il 40-50% delle volte?")
cal = v.calibration(d)
pct = st.column_config.NumberColumn(format="percent")
st.dataframe(cal, hide_index=True, width="stretch", column_config={
    "Prevista": pct, "Uscita": pct, "Scarto": pct, "z": st.column_config.NumberColumn(format="%.1f")})
diag = alt.Chart(pd.DataFrame({"x": [0, 1]})).mark_line(strokeDash=[4, 4], color="gray").encode(x="x:Q", y="x:Q")
pts = alt.Chart(cal).mark_circle().encode(
    x=alt.X("Prevista:Q", title="Probabilità prevista", axis=alt.Axis(format="%")),
    y=alt.Y("Uscita:Q", title="Frequenza reale", axis=alt.Axis(format="%")),
    size=alt.Size("Esiti:Q", legend=None), tooltip=["Fascia", "Esiti", alt.Tooltip("Prevista", format=".1%"),
                                                     alt.Tooltip("Uscita", format=".1%")])
st.altair_chart(diag + pts, width="stretch")

# ------------------------------------------------------------------ giocate derivate
st.subheader("Giocate: il nostro modello dei gol")
st.write("1X2 e U/O 2.5 vengono dal mercato; tutte le altre giocate dalla griglia dei gol (Poisson con rho fisso). "
         "Se una giocata esce sistematicamente più o meno del previsto, l'errore è nostro. "
         "**|z| > 2** = scarto difficilmente dovuto al caso.")
mk = v.markets(d)
st.dataframe(mk.style.apply(lambda r: ["background-color: rgba(230,80,60,.25)" if abs(r["z"]) > 2 else ""] * len(r),
                            axis=1),
             hide_index=True, width="stretch", column_config={
                 "Prevista": pct, "Uscita": pct, "Scarto": pct,
                 "z": st.column_config.NumberColumn(format="%.1f"), "Brier": f4,
                 "Dal mercato": st.column_config.CheckboxColumn("Dal mercato")})
with st.expander("Controllo di rho (correzione per 0-0, 1-0, 0-1, 1-1)"):
    st.dataframe(v.rho_check(d), hide_index=True, column_config={"Log loss risultato esatto": f4})
    st.caption("Solo un controllo: rho si cambia se lo scarto si conferma su molte centinaia di partite, "
               "non inseguendo il valore migliore del momento.")

# ------------------------------------------------------------------ errori
st.subheader("Dove abbiamo sbagliato")
if True:  # una tabella sotto l altra: affiancate sono troppo strette
    st.markdown("**Mercato mosso di più dopo la nostra rilevazione**")
    st.dataframe(v.moves(d), hide_index=True, width="stretch",
                 column_config={"Movimento": pct, "Data": st.column_config.DateColumn(format="DD/MM")})
    st.caption("Qui è arrivata un'informazione che non avevamo: sono i casi da studiare con formazioni e assenze.")
if True:  # una tabella sotto l altra: affiancate sono troppo strette
    st.markdown("**Risultati più inattesi**")
    st.dataframe(v.surprises(d), hide_index=True, width="stretch",
                 column_config={"Prob. data all'esito": pct, "Data": st.column_config.DateColumn(format="DD/MM")})
    st.caption("Se anche la chiusura dava poco a quell'esito, è sfortuna e non errore.")

with st.expander("Partita per partita"):
    cols = {"match_date": "Data", "competition": "Comp.", "partita": "Partita", "risultato": "Risultato",
            "created_at": "Rilevata", "p_home": "1", "p_draw": "X", "p_away": "2", "p_over25": "O2.5",
            "close_fair_h": "1 chius.", "close_fair_d": "X chius.", "close_fair_a": "2 chius.",
            "ll": "Log loss", "ll_chiusura": "Log loss chius.", "movimento": "Movimento"}
    table = d[list(cols)].rename(columns=cols).sort_values("Data", ascending=False)
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        **{c: pct for c in ["1", "X", "2", "O2.5", "1 chius.", "X chius.", "2 chius.", "Movimento"]},
        "Log loss": f4, "Log loss chius.": f4,
        "Rilevata": st.column_config.DatetimeColumn(format="DD/MM HH:mm", timezone="Europe/Rome")})
    st.download_button("Scarica CSV", d.to_csv(index=False).encode(), "verifica_previsioni.csv", "text/csv")
