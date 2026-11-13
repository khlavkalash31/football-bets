import streamlit as st

import core
import weekend

st.title("Schedine proposte")
st.write("Schedine alternative tra loro (partite tutte diverse), scelte tra le giocate col margine più basso. "
         "Il valore atteso è negativo per costruzione: è il margine del book, che qui si cerca di tenere basso.")

matches, note = core.current_matches()
st.caption(note)
if matches.empty:
    st.stop()

c1, c2 = st.columns(2)
stake = c1.number_input("Puntata (euro)", min_value=1.0, value=5.0, step=1.0)
lo, hi = c2.slider("Probabilità di ogni giocata", 0.10, 0.95, (0.40, 0.88), step=0.01)

tickets = weekend.propose_tickets(matches, lo, hi)
if not tickets:
    st.info("Nessuna schedina: non restano partite da giocare o i filtri sono troppo stretti.")
    st.stop()

st.caption("Quote con * = stimate dal margine 1X2: gioca solo se il tuo book paga almeno quella quota.")
for name, t in tickets:
    q, p = t["Quota"].prod(), t["Prob"].prod()
    with st.container(border=True):
        st.markdown(f"**{name}** · quota **{q:.2f}** · vincita {stake * q:.2f} euro · probabilità {p:.0%} "
                    f"(1 volta su {1 / p:.1f}) · valore atteso {stake * (q * p - 1):+.2f} euro")
        for _, r in t.sort_values("Inizio").iterrows():
            star = "*" if r["Stimata"] else ""
            st.markdown(f"- {r['Ora']} · {r['Lega']} · {r['Partita']} · **{r['Giocata']}** · "
                        f"quota {r['Quota']:.2f}{star} (equa {r['Equa']:.2f}, prob {r['Prob']:.0%})")
