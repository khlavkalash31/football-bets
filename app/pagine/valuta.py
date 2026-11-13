import itertools

import pandas as pd
import streamlit as st

import core
import weekend

st.title("Valuta le mie quote")
st.write("Scrivi le quote che vedi sul tuo book: per ognuna l'app calcola la probabilità e il **valore** "
         "(quota × probabilità − 1, cioè quanto rende in media ogni euro). Negativo = paghi il margine del book.")

matches, note = core.current_matches()
st.caption(note)
if matches.empty:
    st.stop()

st.session_state.setdefault("book", [])
with st.form("nuova", clear_on_submit=True):
    c1, c2, c3 = st.columns([3, 2, 1])
    i = c1.selectbox("Partita", matches.index, format_func=lambda i: core.match_label(matches.loc[i]))
    sel = c2.text_input("Giocata", placeholder="1, X2, O2.5, GG, 1+O1.5, MG2-4, 2-1")
    odds = c3.number_input("Quota", min_value=1.01, value=2.0, step=0.05, format="%.2f")
    if st.form_submit_button("Aggiungi", type="primary") and sel:
        m = matches.loc[i]
        try:
            p = weekend.prob(m, sel)
        except ValueError as e:
            st.error(str(e))
        else:
            st.session_state["book"].append({"Partita": f"{m['HomeTeam']} - {m['AwayTeam']}", "Giocata": sel.upper(),
                                             "Quota": odds, "Prob": p, "Equa": 1 / p, "Valore": odds * p - 1})

book = pd.DataFrame(st.session_state["book"])
if book.empty:
    st.info("Nessuna quota inserita.")
    st.stop()
book = book.drop_duplicates(["Partita", "Giocata"], keep="last").sort_values("Valore", ascending=False)
pct = st.column_config.NumberColumn(format="percent")
st.dataframe(book, hide_index=True, width="stretch",
             column_config={"Prob": pct, "Valore": pct, "Equa": st.column_config.NumberColumn(format="%.2f")})
if st.button("Svuota"):
    st.session_state["book"] = []
    st.rerun()

st.subheader("Le combinazioni migliori")
stake = st.number_input("Puntata (euro)", min_value=1.0, value=5.0, step=1.0)
top = book.head(10).reset_index(drop=True)
rows = []
for legs in (1, 2, 3):
    best = None
    for idx in itertools.combinations(top.index, legs):
        c = top.loc[list(idx)]
        if c["Partita"].nunique() < legs:  # stessa partita = combo, va inserita come tale
            continue
        q, p = c["Quota"].prod(), c["Prob"].prod()
        if best is None or q * p - 1 > best[0]:
            best = (q * p - 1, q, p, c)
    if best:
        ev, q, p, c = best
        rows.append({"Schedina": {1: "Singola", 2: "Doppia", 3: "Tripla"}[legs],
                     "Giocate": " · ".join(c["Partita"] + " " + c["Giocata"]), "Quota": q,
                     "Vincita (euro)": stake * q, "Prob": p, "Valore atteso (euro)": stake * ev})
if rows:
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "Quota": st.column_config.NumberColumn(format="%.2f"), "Prob": pct,
        "Vincita (euro)": st.column_config.NumberColumn(format="%.2f"),
        "Valore atteso (euro)": st.column_config.NumberColumn(format="%+.2f")})
if book["Valore"].iloc[0] < 0:
    st.warning("Nessuna quota sopra l'equa: qualunque giocata costa in media il margine del book. "
               "Più partite metti in multipla, più margine paghi.")
