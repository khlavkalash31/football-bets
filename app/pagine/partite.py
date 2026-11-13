import streamlit as st

import core
import weekend

st.title("Partite")
st.write("Quote **eque** (senza margine del bookmaker) ricavate dalle quote medie di mercato. "
         "Conviene giocare solo dove il tuo book paga più della quota equa.")

if core.db_ok():
    with st.expander("Aggiorna le quote"):
        st.write("Scarica le quote di adesso e le salva nel database, come `python weekend.py lista`. "
                 "Con The Odds API costa circa 2 crediti per competizione.")
        if st.button("Aggiorna ora", type="primary"):
            s = core.settings()
            with st.spinner("Scarico e salvo..."):
                try:
                    for m in core.refresh_into_db(s["leagues"], s["start"], s["days"]):
                        st.info(m)
                except Exception as e:
                    st.error(f"Aggiornamento non riuscito: {e}")
else:
    if st.button("Riscarica le quote"):
        core.live_matches.clear()

matches, note = core.current_matches()
st.caption(note)
if matches.empty:
    st.stop()

sheet = weekend.fair_sheet(matches)
sheet.insert(1, "Competizione", sheet.pop("Lega").map(lambda c: core.COMPETITIONS.get(c, c)))
st.dataframe(sheet, hide_index=True, width="stretch",
             column_config={s: st.column_config.NumberColumn(s, format="%.2f") for s in weekend.SHEET})

st.subheader("Tutte le giocate di una partita")
i = st.selectbox("Partita", matches.index, format_func=lambda i: core.match_label(matches.loc[i]))
menu = weekend.market_menu(matches.loc[i])
st.dataframe(menu, hide_index=True, width="stretch", column_config={
    "Prob": st.column_config.NumberColumn(format="percent"),
    "Equa": st.column_config.NumberColumn(format="%.2f"),
    "Quota": st.column_config.NumberColumn("Quota mercato", format="%.2f",
                                           help="media di mercato per 1X2 e U/O 2.5, stimata per le altre"),
    "Stimata": st.column_config.CheckboxColumn(),
    "Valore": st.column_config.NumberColumn(format="percent"),
})
