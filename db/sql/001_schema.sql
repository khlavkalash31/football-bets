-- Schema iniziale. Le migrazioni si applicano in ordine di nome con `python -m db migrate`;
-- un file già applicato non va modificato: per cambiare lo schema si aggiunge 002_..., 003_...

-- Competizioni: codici football-data (I1, SP1, E0, F1, D1, P1) + CL per la Champions League.
CREATE TABLE competitions (
    code    text PRIMARY KEY,
    name    text NOT NULL,
    country text
);

INSERT INTO competitions (code, name, country) VALUES
    ('I1', 'Serie A', 'Italia'),
    ('SP1', 'LaLiga', 'Spagna'),
    ('E0', 'Premier League', 'Inghilterra'),
    ('F1', 'Ligue 1', 'Francia'),
    ('D1', 'Bundesliga', 'Germania'),
    ('P1', 'Liga Portugal', 'Portogallo'),
    ('CL', 'Champions League', NULL);

-- Squadre. Il nome canonico è quello di football-data; ogni fonte ha i suoi nomi in team_aliases.
CREATE TABLE teams (
    team_id serial PRIMARY KEY,
    name    text NOT NULL UNIQUE,
    country text
);

-- Nome (o id) di una squadra in una fonte -> squadra. confirmed = false: abbinata in automatico per
-- somiglianza del nome, da controllare con `python -m db alias`.
CREATE TABLE team_aliases (
    source    text NOT NULL,          -- football-data, the-odds-api, understat, transfermarkt, clubelo...
    alias     text NOT NULL,
    team_id   int  NOT NULL REFERENCES teams ON DELETE CASCADE,
    confirmed boolean NOT NULL DEFAULT true,
    PRIMARY KEY (source, alias)
);

CREATE TABLE players (
    player_id   serial PRIMARY KEY,
    name        text NOT NULL,
    birth_date  date,
    position    text,                 -- GK, DF, MF, FW (o il dettaglio della fonte)
    foot        text,
    nationality text
);

CREATE TABLE player_aliases (
    source    text NOT NULL,
    source_id text NOT NULL,          -- id del giocatore nella fonte (Transfermarkt, Understat...)
    player_id int  NOT NULL REFERENCES players ON DELETE CASCADE,
    PRIMARY KEY (source, source_id)
);

-- Una partita, giocata o da giocare (risultato NULL). La data è quella inglese, come in football-data.
CREATE TABLE matches (
    match_id     serial PRIMARY KEY,
    competition  text NOT NULL REFERENCES competitions,
    season       text NOT NULL,       -- '2526' = 2025/26
    match_date   date NOT NULL,
    kickoff      timestamptz,
    home_team_id int  NOT NULL REFERENCES teams,
    away_team_id int  NOT NULL REFERENCES teams,
    home_goals   smallint,
    away_goals   smallint,
    home_goals_ht smallint,
    away_goals_ht smallint,
    home_shots   smallint,
    away_shots   smallint,
    home_shots_target smallint,
    away_shots_target smallint,
    home_corners smallint,
    away_corners smallint,
    home_yellow  smallint,
    away_yellow  smallint,
    home_red     smallint,
    away_red     smallint,
    home_xg      numeric(5, 2),
    away_xg      numeric(5, 2),
    referee      text,
    UNIQUE (competition, match_date, home_team_id, away_team_id)
);
CREATE INDEX matches_date ON matches (match_date);
CREATE INDEX matches_home ON matches (home_team_id, match_date);
CREATE INDEX matches_away ON matches (away_team_id, match_date);

-- Statistiche di un giocatore in una partita.
CREATE TABLE player_match (
    match_id  int NOT NULL REFERENCES matches ON DELETE CASCADE,
    player_id int NOT NULL REFERENCES players,
    team_id   int NOT NULL REFERENCES teams,
    started   boolean,
    minutes   smallint,
    goals     smallint,
    assists   smallint,
    xg        numeric(5, 2),
    xa        numeric(5, 2),
    yellow    smallint,
    red       smallint,
    source    text NOT NULL,
    PRIMARY KEY (match_id, player_id)
);
CREATE INDEX player_match_player ON player_match (player_id);

CREATE TABLE player_value (
    player_id  int  NOT NULL REFERENCES players ON DELETE CASCADE,
    value_date date NOT NULL,
    value_eur  bigint NOT NULL,
    PRIMARY KEY (player_id, value_date)
);

-- Infortuni e squalifiche.
CREATE TABLE absences (
    absence_id serial PRIMARY KEY,
    player_id  int  NOT NULL REFERENCES players ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('injury', 'suspension', 'other')),
    reason     text,
    from_date  date NOT NULL,
    to_date    date,
    source     text NOT NULL,
    UNIQUE (player_id, from_date, source)
);

-- Rating Elo giornaliero (ClubElo).
CREATE TABLE team_elo (
    team_id  int  NOT NULL REFERENCES teams ON DELETE CASCADE,
    elo_date date NOT NULL,
    elo      numeric(7, 2) NOT NULL,
    PRIMARY KEY (team_id, elo_date)
);

-- Quote. phase: 'open' e 'close' sono le quote di apertura e chiusura dei CSV di football-data
-- (ora di rilevazione non nota: taken_at NULL); 'snapshot' è una rilevazione nostra con data e ora.
-- market: '1x2' (outcome H D A) o 'ou2.5' (outcome O U). bookmaker 'avg' e 'max' = media e massimo del mercato.
CREATE TABLE odds_snapshots (
    snapshot_id bigserial PRIMARY KEY,
    match_id    int  NOT NULL REFERENCES matches ON DELETE CASCADE,
    market      text NOT NULL,
    outcome     text NOT NULL,
    bookmaker   text NOT NULL,
    phase       text NOT NULL CHECK (phase IN ('open', 'close', 'snapshot')),
    taken_at    timestamptz,
    price       numeric(8, 3) NOT NULL CHECK (price > 1),
    source      text NOT NULL,
    UNIQUE NULLS NOT DISTINCT (match_id, market, outcome, bookmaker, phase, taken_at)
);
CREATE INDEX odds_match ON odds_snapshots (match_id, market, phase);

-- Probabili formazioni rilevate prima della partita (il venerdì). Il nome è quello della fonte;
-- player_id si riempie quando il giocatore viene abbinato.
CREATE TABLE predicted_lineups (
    lineup_id   bigserial PRIMARY KEY,
    match_id    int  NOT NULL REFERENCES matches ON DELETE CASCADE,
    team_id     int  NOT NULL REFERENCES teams,
    player_name text NOT NULL,
    player_id   int  REFERENCES players,
    starter     boolean NOT NULL DEFAULT true,
    source      text NOT NULL,
    taken_at    timestamptz NOT NULL,
    UNIQUE (match_id, team_id, player_name, source, taken_at)
);

-- Probabilità prodotte da un modello. model_version 'market-v1' = quote medie di mercato senza margine
-- (quello che fa oggi weekend.py lista).
CREATE TABLE predictions (
    prediction_id bigserial PRIMARY KEY,
    match_id      int  NOT NULL REFERENCES matches ON DELETE CASCADE,
    model_version text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    lam           numeric(6, 3),      -- gol attesi casa
    mu            numeric(6, 3),      -- gol attesi trasferta
    p_home        numeric(6, 5),
    p_draw        numeric(6, 5),
    p_away        numeric(6, 5),
    p_over25      numeric(6, 5),
    UNIQUE (match_id, model_version, created_at)
);

-- Registro dei file grezzi salvati in raw/ (scaricati così come arrivano).
CREATE TABLE raw_files (
    raw_id     bigserial PRIMARY KEY,
    source     text NOT NULL,
    path       text NOT NULL UNIQUE,  -- relativo alla cartella raw/
    sha256     text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now()
);
