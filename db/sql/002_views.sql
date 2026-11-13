-- Viste di lettura, pensate per notebook e frontend (Streamlit): una riga per partita, nomi leggibili.

CREATE VIEW v_matches AS
SELECT m.match_id, m.competition, c.name AS competition_name, m.season, m.match_date, m.kickoff,
       h.name AS home_team, a.name AS away_team, m.home_goals, m.away_goals,
       CASE WHEN m.home_goals IS NULL THEN NULL
            WHEN m.home_goals > m.away_goals THEN 'H'
            WHEN m.home_goals = m.away_goals THEN 'D' ELSE 'A' END AS result,
       m.home_xg, m.away_xg, m.home_team_id, m.away_team_id
FROM matches m
JOIN competitions c ON c.code = m.competition
JOIN teams h ON h.team_id = m.home_team_id
JOIN teams a ON a.team_id = m.away_team_id;

-- Quote 1X2 e U/O 2.5 per partita, fase e bookmaker, su una riga, con le probabilità eque
-- (inverso della quota normalizzato: si toglie il margine in proporzione).
CREATE VIEW v_odds_wide AS
WITH w AS (
    SELECT match_id, phase, bookmaker, taken_at,
           max(price) FILTER (WHERE market = '1x2' AND outcome = 'H') AS odds_h,
           max(price) FILTER (WHERE market = '1x2' AND outcome = 'D') AS odds_d,
           max(price) FILTER (WHERE market = '1x2' AND outcome = 'A') AS odds_a,
           max(price) FILTER (WHERE market = 'ou2.5' AND outcome = 'O') AS odds_over25,
           max(price) FILTER (WHERE market = 'ou2.5' AND outcome = 'U') AS odds_under25
    FROM odds_snapshots
    GROUP BY match_id, phase, bookmaker, taken_at
)
SELECT w.*,
       (1 / odds_h) / (1 / odds_h + 1 / odds_d + 1 / odds_a) AS fair_h,
       (1 / odds_d) / (1 / odds_h + 1 / odds_d + 1 / odds_a) AS fair_d,
       (1 / odds_a) / (1 / odds_h + 1 / odds_d + 1 / odds_a) AS fair_a,
       (1 / odds_over25) / (1 / odds_over25 + 1 / odds_under25) AS fair_over25,
       1 / odds_h + 1 / odds_d + 1 / odds_a - 1 AS margin_1x2
FROM w;

-- Ultima rilevazione nostra (phase 'snapshot') della quota media, per partita.
CREATE VIEW v_latest_snapshot AS
SELECT DISTINCT ON (match_id) *
FROM v_odds_wide
WHERE phase = 'snapshot' AND bookmaker = 'avg'
ORDER BY match_id, taken_at DESC;

-- Apertura (media mercato) contro chiusura (Pinnacle, se manca la media) per partita giocata:
-- la base per misurare CLV e log loss.
CREATE VIEW v_open_close AS
SELECT m.*,
       o.odds_h AS open_h, o.odds_d AS open_d, o.odds_a AS open_a,
       o.fair_h AS open_fair_h, o.fair_d AS open_fair_d, o.fair_a AS open_fair_a,
       coalesce(cp.odds_h, ca.odds_h) AS close_h,
       coalesce(cp.odds_d, ca.odds_d) AS close_d,
       coalesce(cp.odds_a, ca.odds_a) AS close_a,
       coalesce(cp.fair_h, ca.fair_h) AS close_fair_h,
       coalesce(cp.fair_d, ca.fair_d) AS close_fair_d,
       coalesce(cp.fair_a, ca.fair_a) AS close_fair_a
FROM v_matches m
LEFT JOIN v_odds_wide o  ON o.match_id = m.match_id AND o.phase = 'open' AND o.bookmaker = 'avg'
LEFT JOIN v_odds_wide cp ON cp.match_id = m.match_id AND cp.phase = 'close' AND cp.bookmaker = 'pinnacle'
LEFT JOIN v_odds_wide ca ON ca.match_id = m.match_id AND ca.phase = 'close' AND ca.bookmaker = 'avg';
