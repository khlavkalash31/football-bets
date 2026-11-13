import streamlit as st

import core

st.title("Database")
st.caption(core.db_status()[1])

counts = core.query("""
    SELECT 'Partite giocate' AS "Cosa", count(*) FILTER (WHERE home_goals IS NOT NULL) AS "Righe" FROM matches
    UNION ALL SELECT 'Partite future', count(*) FILTER (WHERE home_goals IS NULL) FROM matches
    UNION ALL SELECT 'Quote', count(*) FROM odds_snapshots
    UNION ALL SELECT 'Rilevazioni nostre (quote)', count(*) FROM odds_snapshots WHERE phase = 'snapshot'
    UNION ALL SELECT 'Predizioni', count(*) FROM predictions
    UNION ALL SELECT 'Squadre', count(*) FROM teams
    UNION ALL SELECT 'Calciatori', count(*) FROM players
    UNION ALL SELECT 'File grezzi', count(*) FROM raw_files""")
st.dataframe(counts, hide_index=True)

seasons = core.query("""SELECT competition AS "Competizione", min(season) AS "Dalla stagione",
                               max(season) AS "Alla stagione", count(*) AS "Partite"
                        FROM matches WHERE home_goals IS NOT NULL GROUP BY 1 ORDER BY 1""")
if not seasons.empty:
    st.dataframe(seasons, hide_index=True)

st.subheader("Nomi squadra da controllare")
aliases = core.query("""SELECT a.source AS "Fonte", a.alias AS "Nome nella fonte", t.name AS "Abbinata a",
                               NOT EXISTS (SELECT 1 FROM team_aliases f WHERE f.team_id = t.team_id
                                           AND f.source = 'football-data') AS "Squadra nuova"
                        FROM team_aliases a JOIN teams t USING (team_id)
                        WHERE NOT a.confirmed ORDER BY 1, 3""")
if aliases.empty:
    st.success("Nessun abbinamento da controllare.")
else:
    st.dataframe(aliases, hide_index=True, width="stretch")
    st.write("Se uno è sbagliato, dal terminale:")
    st.code('python -m db alias the-odds-api "Inter Milan" Inter\n'
            'python -m db alias the-odds-api "Club Brugge" "Club Brugge" --nuova', language="bash")
if st.button("Ricarica"):
    core.query.clear()
    st.rerun()
