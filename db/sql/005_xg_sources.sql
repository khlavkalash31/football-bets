-- xG da più fonti (Understat, FotMob): modelli diversi, quindi ogni fonte tiene i suoi valori.
--
-- match_xg e player_match_xg hanno i valori di ogni fonte, una riga per fonte.
-- matches.home_xg/away_xg e player_match.xg/xa restano comodi da usare: tengono il valore di una sola fonte,
-- quella con la priorità più alta tra quelle disponibili (Understat, poi FotMob), indicata in xg_source.
-- Per un modello che deve essere coerente nel tempo conviene leggere una fonte sola da match_xg.

CREATE TABLE match_xg (
    match_id int  NOT NULL REFERENCES matches ON DELETE CASCADE,
    source   text NOT NULL,
    home_xg  numeric(5, 2),
    away_xg  numeric(5, 2),
    PRIMARY KEY (match_id, source)
);

CREATE TABLE player_match_xg (
    match_id  int  NOT NULL REFERENCES matches ON DELETE CASCADE,
    player_id int  NOT NULL REFERENCES players,
    source    text NOT NULL,
    xg        numeric(5, 2),
    xa        numeric(5, 2),
    PRIMARY KEY (match_id, player_id, source)
);

ALTER TABLE matches ADD COLUMN xg_source text;
ALTER TABLE player_match ADD COLUMN xg_source text;

-- Finora gli xG potevano arrivare solo da Understat.
UPDATE matches SET xg_source = 'understat' WHERE home_xg IS NOT NULL OR away_xg IS NOT NULL;
UPDATE player_match SET xg_source = 'understat' WHERE xg IS NOT NULL OR xa IS NOT NULL;
INSERT INTO match_xg (match_id, source, home_xg, away_xg)
SELECT match_id, 'understat', home_xg, away_xg FROM matches WHERE xg_source = 'understat';
INSERT INTO player_match_xg (match_id, player_id, source, xg, xa)
SELECT match_id, player_id, 'understat', xg, xa FROM player_match WHERE xg_source = 'understat';
