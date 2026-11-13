"""App Streamlit: streamlit run app/app.py (oppure docker compose up -d e http://localhost:8501)."""
import streamlit as st

import core

st.set_page_config(page_title="Football bets", page_icon="⚽", layout="wide")
core.secrets_to_env()
s = core.settings()

pages = [
    st.Page("pagine/partite.py", title="Partite", icon="📅", default=True),
    st.Page("pagine/valuta.py", title="Valuta le mie quote", icon="🧮"),
    st.Page("pagine/proposte.py", title="Schedine proposte", icon="🎟️"),
]
if core.db_ok():
    pages += [st.Page("pagine/storico.py", title="Storico", icon="📈"),
              st.Page("pagine/database.py", title="Database", icon="🗄️")]
nav = st.navigation(pages)

with st.sidebar:
    st.multiselect("Competizioni", list(core.COMPETITIONS), key="leagues",
                   format_func=lambda c: core.COMPETITIONS[c])
    st.slider("Giorni da oggi", 1, 7, key="days")
    ok, msg = core.db_status()
    st.caption(("🟢 Database: " if ok else "⚪ ") + msg)
    if not core.odds_key():
        st.caption("⚠️ Manca ODDS_API_KEY: uso le quote di football-data (senza Champions).")

nav.run()
