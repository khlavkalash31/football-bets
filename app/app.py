"""App Streamlit: streamlit run app/app.py (oppure docker compose up -d e http://localhost:8501)."""
import datetime as dt

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
    pages += [st.Page("pagine/verifica.py", title="Verifica previsioni", icon="🎯"),
              st.Page("pagine/storico.py", title="Storico quote", icon="📈"),
              st.Page("pagine/database.py", title="Database", icon="🗄️")]
nav = st.navigation(pages)

with st.sidebar:
    st.multiselect("Competizioni", list(core.COMPETITIONS), key="leagues",
                   format_func=lambda c: core.COMPETITIONS[c])
    st.date_input("Dal giorno", key="start", min_value=dt.date.today(), format="DD/MM/YYYY")
    st.slider("Giorni", 1, 7, key="days")
    ok, msg = core.db_status()
    if core.read_only():
        st.caption(f"🟢 Dati copiati dal PC il {core.synced_at().astimezone(core.ROME):%d/%m alle %H:%M}")
    else:
        st.caption(("🟢 Database: " if ok else "⚪ ") + msg)
    if not core.odds_key() and not core.db_ok():
        st.caption("⚠️ Manca ODDS_API_KEY: uso le quote di football-data (senza Champions).")

nav.run()
