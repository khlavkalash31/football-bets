-- Calciatori: indici per le query per squadra e una vista leggibile delle presenze.

CREATE INDEX IF NOT EXISTS player_match_team ON player_match (team_id, match_id);
CREATE INDEX IF NOT EXISTS player_value_date ON player_value (player_id, value_date DESC);

-- Una riga per giocatore e partita, con nomi, data e valore di mercato più recente prima della partita.
CREATE VIEW v_player_match AS
SELECT pm.match_id, m.competition, m.season, m.match_date, t.name AS team, p.name AS player, p.position,
       pm.started, pm.minutes, pm.goals, pm.assists, pm.xg, pm.xa, pm.yellow, pm.red,
       (SELECT v.value_eur FROM player_value v
        WHERE v.player_id = pm.player_id AND v.value_date <= m.match_date
        ORDER BY v.value_date DESC LIMIT 1) AS value_eur,
       pm.player_id, pm.team_id
FROM player_match pm
JOIN matches m USING (match_id)
JOIN teams t ON t.team_id = pm.team_id
JOIN players p ON p.player_id = pm.player_id;
