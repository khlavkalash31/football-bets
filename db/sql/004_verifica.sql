-- Verifica delle previsioni: ogni previsione salvata prima del calcio d'inizio, con il risultato
-- (quando c'è) e le probabilità eque di apertura e chiusura dei CSV di football-data per confronto.

CREATE INDEX IF NOT EXISTS predictions_match ON predictions (match_id, model_version, created_at);

CREATE VIEW v_prediction_results AS
WITH p AS (
    SELECT p.*,
           row_number() OVER w AS n_rilevazione,          -- 1 = prima rilevazione (di solito il venerdì)
           count(*) OVER (PARTITION BY p.match_id, p.model_version) AS n_rilevazioni
    FROM predictions p
    JOIN matches m USING (match_id)
    WHERE m.kickoff IS NULL OR p.created_at < m.kickoff  -- solo previsioni fatte prima della partita
    WINDOW w AS (PARTITION BY p.match_id, p.model_version ORDER BY p.created_at)
)
SELECT p.prediction_id, p.model_version, p.created_at, p.n_rilevazione, p.n_rilevazioni,
       oc.match_id, oc.competition, oc.season, oc.match_date, oc.kickoff, oc.home_team, oc.away_team,
       oc.home_goals, oc.away_goals, oc.result,
       p.lam, p.mu, p.p_home, p.p_draw, p.p_away, p.p_over25,
       oc.open_fair_h, oc.open_fair_d, oc.open_fair_a, oo.fair_over25 AS open_fair_over25,
       oc.close_fair_h, oc.close_fair_d, oc.close_fair_a,
       coalesce(cop.fair_over25, coa.fair_over25) AS close_fair_over25
FROM p
JOIN v_open_close oc ON oc.match_id = p.match_id
LEFT JOIN v_odds_wide oo  ON oo.match_id = p.match_id AND oo.phase = 'open' AND oo.bookmaker = 'avg'
LEFT JOIN v_odds_wide cop ON cop.match_id = p.match_id AND cop.phase = 'close' AND cop.bookmaker = 'pinnacle'
LEFT JOIN v_odds_wide coa ON coa.match_id = p.match_id AND coa.phase = 'close' AND coa.bookmaker = 'avg';
