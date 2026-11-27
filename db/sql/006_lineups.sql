-- Probabili formazioni e indisponibili, rilevati prima della partita (di solito il venerdì).
-- Ogni rilevazione ha il suo taken_at: le formazioni cambiano nei giorni prima della partita e per un
-- backtest onesto serve sapere cosa si sapeva in quel momento.

ALTER TABLE predicted_lineups ADD COLUMN lineup_type text;   -- 'predicted' o la formazione ufficiale
ALTER TABLE predicted_lineups ADD COLUMN formation text;     -- es. '3-5-2'
ALTER TABLE predicted_lineups ADD COLUMN position_id smallint;
CREATE INDEX IF NOT EXISTS predicted_lineups_match ON predicted_lineups (match_id, taken_at);

CREATE TABLE predicted_absences (
    match_id        int  NOT NULL REFERENCES matches ON DELETE CASCADE,
    team_id         int  NOT NULL REFERENCES teams,
    player_name     text NOT NULL,
    player_id       int  REFERENCES players,
    kind            text,           -- injury, suspension, internationalDuty...
    reason          text,           -- codice o descrizione dell'infortunio, se c'è
    expected_return text,           -- testo della fonte: 'Doubtful', 'Late January 2027'...
    doubtful        boolean NOT NULL DEFAULT false,
    source          text NOT NULL,
    taken_at        timestamptz NOT NULL,
    PRIMARY KEY (match_id, team_id, player_name, source, taken_at)
);

-- Ultima rilevazione per partita e fonte: titolari previsti e indisponibili, con i nomi.
CREATE VIEW v_latest_lineups AS
WITH last AS (
    SELECT match_id, source, max(taken_at) AS taken_at FROM predicted_lineups GROUP BY 1, 2
)
SELECT m.match_id, m.competition, m.match_date, t.name AS team, pl.player_name, pl.player_id,
       pl.lineup_type, pl.formation, pl.source, pl.taken_at, pl.team_id
FROM predicted_lineups pl
JOIN last USING (match_id, source, taken_at)
JOIN matches m USING (match_id)
JOIN teams t ON t.team_id = pl.team_id;
