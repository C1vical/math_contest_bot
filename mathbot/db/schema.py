"""SQLite schema, expressed as ordered migrations.

Add a new ``(version, sql)`` entry to :data:`MIGRATIONS` to change the schema;
never edit an applied one. The SQL sticks to features PostgreSQL also has
(``ON CONFLICT`` upserts, foreign keys, ``CHECK``), with ``INTEGER PRIMARY KEY``
being the one SQLite-ism to translate (``BIGSERIAL``) if the project moves.

Layout
------
contest_series    one row per contest series (AMC 10A, Euclid, ...)
contest_aliases   normalized alias -> series, for case-insensitive lookup
contest_editions  one sitting of a series in a year
problems          current normalized statement of each problem
problem_versions  every normalized statement a problem has ever had
source_records    raw source payloads + provenance, one row per distinct payload
assets            remote images referenced by problems, cached locally by hash
problem_assets    which problem uses which asset
renders           PNG render manifest keyed by (problem, content hash, renderer)
solutions         official solutions, one per (problem, provider), versioned by hash  (v2)
problem_tags      free-form tags per problem, with where each came from              (v2)
                  (v3 adds unused columns to source_records; see the migration)
ingestion_runs    one row per ingest invocation with its counters / report
ingestion_errors  per-problem / per-edition failures of a run
ingestion_checkpoints  editions finished by a run (drives --resume)
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE contest_series (
            id            TEXT PRIMARY KEY,
            key           TEXT NOT NULL UNIQUE,
            name          TEXT NOT NULL,
            family        TEXT NOT NULL,
            provider      TEXT NOT NULL,
            source_name   TEXT NOT NULL,
            enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            notes         TEXT NOT NULL DEFAULT '',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );
        CREATE INDEX idx_series_family ON contest_series (family);
        CREATE INDEX idx_series_provider ON contest_series (provider);

        CREATE TABLE contest_aliases (
            alias         TEXT PRIMARY KEY,
            series_id     TEXT NOT NULL REFERENCES contest_series (id) ON DELETE CASCADE
        );

        CREATE TABLE contest_editions (
            id                 TEXT PRIMARY KEY,
            series_id          TEXT NOT NULL REFERENCES contest_series (id) ON DELETE CASCADE,
            year               INTEGER NOT NULL CHECK (year BETWEEN 1800 AND 2200),
            label              TEXT NOT NULL,
            source_url         TEXT NOT NULL,
            source_identifier  TEXT,
            source_hash        TEXT,
            expected_problems  INTEGER,
            origin             TEXT NOT NULL DEFAULT 'discovered',
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            UNIQUE (series_id, year)
        );
        CREATE INDEX idx_editions_year ON contest_editions (year);

        CREATE TABLE problems (
            id              TEXT PRIMARY KEY,
            edition_id      TEXT NOT NULL REFERENCES contest_editions (id) ON DELETE CASCADE,
            number          INTEGER NOT NULL CHECK (number > 0),
            section         TEXT,
            statement_html  TEXT NOT NULL CHECK (length(trim(statement_html)) > 0),
            statement_text  TEXT NOT NULL,
            content_hash    TEXT NOT NULL,
            version         INTEGER NOT NULL DEFAULT 1,
            answer          TEXT,
            answer_source   TEXT,
            source_url      TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE (edition_id, number)
        );
        CREATE INDEX idx_problems_number ON problems (number);
        CREATE INDEX idx_problems_hash ON problems (content_hash);

        CREATE TABLE problem_versions (
            problem_id      TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
            version         INTEGER NOT NULL,
            content_hash    TEXT NOT NULL,
            statement_html  TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            PRIMARY KEY (problem_id, version)
        );

        CREATE TABLE source_records (
            id                 INTEGER PRIMARY KEY,
            problem_id         TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
            provider           TEXT NOT NULL,
            source_url         TEXT NOT NULL,
            source_identifier  TEXT,
            source_revision    TEXT,
            raw_payload        TEXT NOT NULL,
            raw_hash           TEXT NOT NULL,
            first_seen_at      TEXT NOT NULL,
            last_seen_at       TEXT NOT NULL,
            run_id             INTEGER REFERENCES ingestion_runs (id) ON DELETE SET NULL,
            UNIQUE (problem_id, provider, raw_hash)
        );
        CREATE INDEX idx_source_problem ON source_records (problem_id, last_seen_at);

        CREATE TABLE assets (
            id            INTEGER PRIMARY KEY,
            source_url    TEXT NOT NULL UNIQUE,
            status        TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'failed')),
            sha256        TEXT,
            content_type  TEXT,
            size_bytes    INTEGER,
            local_path    TEXT,
            fetched_at    TEXT,
            expires_at    TEXT,
            error         TEXT
        );

        CREATE TABLE problem_assets (
            problem_id    TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
            asset_id      INTEGER NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
            PRIMARY KEY (problem_id, asset_id)
        );

        CREATE TABLE renders (
            id                INTEGER PRIMARY KEY,
            problem_id        TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
            content_hash      TEXT NOT NULL,
            renderer_version  TEXT NOT NULL,
            status            TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
            local_path        TEXT,
            object_key        TEXT,
            width             INTEGER,
            height            INTEGER,
            size_bytes        INTEGER,
            attempts          INTEGER NOT NULL DEFAULT 0,
            error             TEXT,
            rendered_at       TEXT NOT NULL,
            uploaded_at       TEXT,
            UNIQUE (problem_id, content_hash, renderer_version)
        );
        CREATE INDEX idx_renders_status ON renders (status, uploaded_at);

        CREATE TABLE ingestion_runs (
            id           INTEGER PRIMARY KEY,
            started_at   TEXT NOT NULL,
            finished_at  TEXT,
            mode         TEXT NOT NULL,
            selection    TEXT NOT NULL,
            status       TEXT NOT NULL CHECK (status IN ('running', 'completed', 'completed_with_errors', 'failed')),
            discovered   INTEGER NOT NULL DEFAULT 0,
            inserted     INTEGER NOT NULL DEFAULT 0,
            updated      INTEGER NOT NULL DEFAULT 0,
            skipped      INTEGER NOT NULL DEFAULT 0,
            failed       INTEGER NOT NULL DEFAULT 0,
            invalid      INTEGER NOT NULL DEFAULT 0,
            report       TEXT
        );

        CREATE TABLE ingestion_errors (
            id              INTEGER PRIMARY KEY,
            run_id          INTEGER NOT NULL REFERENCES ingestion_runs (id) ON DELETE CASCADE,
            provider        TEXT NOT NULL,
            series_key      TEXT NOT NULL,
            year            INTEGER,
            problem_number  INTEGER,
            url             TEXT,
            stage           TEXT NOT NULL,
            error           TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX idx_errors_run ON ingestion_errors (run_id);

        CREATE TABLE ingestion_checkpoints (
            edition_id    TEXT PRIMARY KEY REFERENCES contest_editions (id) ON DELETE CASCADE,
            run_id        INTEGER NOT NULL REFERENCES ingestion_runs (id) ON DELETE CASCADE,
            completed_at  TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE solutions (
            id             INTEGER PRIMARY KEY,
            problem_id     TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
            provider       TEXT NOT NULL,
            source_url     TEXT NOT NULL,
            solution_html  TEXT NOT NULL CHECK (length(trim(solution_html)) > 0),
            solution_text  TEXT NOT NULL,
            content_hash   TEXT NOT NULL,
            version        INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            UNIQUE (problem_id, provider)
        );

        CREATE TABLE problem_tags (
            problem_id  TEXT NOT NULL REFERENCES problems (id) ON DELETE CASCADE,
            tag         TEXT NOT NULL,
            source      TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (problem_id, tag)
        );
        CREATE INDEX idx_tags_tag ON problem_tags (tag);

        ALTER TABLE problems ADD COLUMN difficulty REAL
        """,
    ),
    (
        # Added by an experiment that was later reverted, after it had already migrated a
        # real database. Nothing reads or writes these columns. The migration is kept with
        # exactly the SQL that was applied so every database at version 3 has the same
        # schema, and so code at this version can open the databases that experiment touched.
        3,
        """
        ALTER TABLE source_records ADD COLUMN parser_source TEXT NOT NULL DEFAULT 'existing';
        ALTER TABLE source_records ADD COLUMN fallback_used INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE source_records ADD COLUMN fetch_status INTEGER;
        ALTER TABLE source_records ADD COLUMN fetch_timestamp TEXT;
        ALTER TABLE source_records ADD COLUMN fallback_error TEXT;
        ALTER TABLE source_records ADD COLUMN asset_status TEXT NOT NULL DEFAULT 'not_needed';
        ALTER TABLE source_records ADD COLUMN render_status TEXT NOT NULL DEFAULT 'pending';
        ALTER TABLE source_records ADD COLUMN ingestion_error TEXT
        """,
    ),
    (
        # Covering indexes for the bot's /random: picking a problem no longer reads every
        # (large) problem row or render row. 195 ms -> 15 ms on the 8,880-problem database.
        4,
        """
        CREATE INDEX idx_problems_cover ON problems (edition_id, number, id, content_hash);
        CREATE INDEX idx_renders_current ON renders (problem_id, content_hash, status)
        """,
    ),
]

LATEST_VERSION = MIGRATIONS[-1][0]
