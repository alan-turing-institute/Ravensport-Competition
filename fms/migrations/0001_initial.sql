-- FMS schema: all tables and append-only triggers.
-- Append-only constraint applies to: transactions, jobs, job_cancellations, sessions.
-- card_table and atg_readings have no mutation constraints.
--
-- gen_random_uuid() is a PostgreSQL 13+ built-in; no extension required.

-- ---------------------------------------------------------------------------
-- sessions: one immutable row per login event.
-- Active token state is tracked in application memory only.
-- ---------------------------------------------------------------------------
CREATE TABLE sessions (
    session_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    username     TEXT        NOT NULL,
    role         TEXT        NOT NULL,
    logged_in_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- card_table: append-only event log; two rows per card lifecycle (enrol + revoke).
-- card_id is stable across events for the same physical card.
-- ---------------------------------------------------------------------------
CREATE TABLE card_table (
    id          BIGSERIAL   PRIMARY KEY,
    card_id     UUID        NOT NULL,
    card_number INTEGER     NOT NULL,
    label       TEXT        NOT NULL,
    active      BOOLEAN     NOT NULL,
    action      TEXT        NOT NULL CHECK (action IN ('enrol', 'revoke')),
    enrolled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX card_table_card_number_idx ON card_table (card_number);
CREATE INDEX card_table_card_id_idx     ON card_table (card_id);

-- ---------------------------------------------------------------------------
-- jobs: one immutable row per job creation.
-- Mutable state (status, rfid_validated, transaction_id) lives in memory.
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    job_id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    type                TEXT        NOT NULL CHECK (type IN ('dispense', 'fill')),
    counterparty        TEXT        NOT NULL,
    grade               TEXT        NOT NULL,
    operator            TEXT        NOT NULL,
    -- The authenticated session the job was created under (fms.md §8.3): the
    -- join from every bunkering event to its LOGIN (hist.md §4.2). On jobs, not
    -- transactions, because a dispense-close row is written by the Modbus task
    -- with no request context.
    session_id          UUID        NOT NULL REFERENCES sessions (session_id),
    atg_volume_at_open_l DOUBLE PRECISION,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- job_cancellations: one immutable row per cancelled job (fms.md §8.5).
-- A job's status lives in memory only, so this row is the durable record that
-- a job was cancelled, and by whom: jobs.operator and jobs.session_id are those
-- of the job's creation, and a job may be cancelled under another session.
-- The primary key admits one cancellation per job.
-- ---------------------------------------------------------------------------
CREATE TABLE job_cancellations (
    job_id       UUID        PRIMARY KEY REFERENCES jobs (job_id),
    operator     TEXT        NOT NULL,
    session_id   UUID        NOT NULL REFERENCES sessions (session_id),
    cancelled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- transactions: append-only; one row per lifecycle event.
-- BDN linkage is a second row with references_transaction_id set.
-- ---------------------------------------------------------------------------
CREATE TABLE transactions (
    transaction_id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id                      UUID            NOT NULL REFERENCES jobs(job_id),
    references_transaction_id   UUID,

    -- dispense-specific fields
    authorisation_id            UUID,
    operator                    TEXT,
    counterparty                TEXT,
    grade                       TEXT,
    preset_volume_l             DOUBLE PRECISION,
    -- 32-bit unsigned pulse totalisers (dispenser-modbus-tcp-spec.md §7); BIGINT
    -- holds the full u32 range, which a signed INTEGER cannot.
    start_totaliser_pulses      BIGINT,
    end_totaliser_pulses        BIGINT,
    volume_dispensed_l          DOUBLE PRECISION,

    -- fill-specific fields
    ticket_number               TEXT,
    delivered_volume_l          DOUBLE PRECISION,
    supplier                    TEXT,
    tanker_registration         TEXT,

    -- measurement fields, dispense and fill (fms.md §8.4); null on BDN-linkage
    -- rows. On a dispense the FMS populates them (fms.md §5.2); on a fill they
    -- are the road tanker's own figures, transcribed from the delivery ticket.
    temperature_c               DOUBLE PRECISION,
    density_kg_per_l            DOUBLE PRECISION,
    temp_corrected_volume_l     DOUBLE PRECISION,

    -- shared fields
    bdn_number                  TEXT,
    atg_volume_at_open_l        DOUBLE PRECISION,
    atg_volume_at_close_l       DOUBLE PRECISION,
    opened_at                   TIMESTAMPTZ,
    closed_at                   TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT now()
);

CREATE INDEX transactions_job_id_idx ON transactions (job_id);

-- ---------------------------------------------------------------------------
-- atg_readings: time-series store; no append-only constraint.
-- ---------------------------------------------------------------------------
CREATE TABLE atg_readings (
    id          BIGSERIAL       PRIMARY KEY,
    volume_l    DOUBLE PRECISION NOT NULL,
    temperature_c DOUBLE PRECISION NOT NULL,
    alarm_flags TEXT,
    recorded_at TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Append-only trigger function: blocks UPDATE and DELETE on guarded tables.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION deny_mutation()
    RETURNS trigger
    LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only table: % on % is not permitted', TG_OP, TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER transactions_no_update
    BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER transactions_no_delete
    BEFORE DELETE ON transactions
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER jobs_no_update
    BEFORE UPDATE ON jobs
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER jobs_no_delete
    BEFORE DELETE ON jobs
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER job_cancellations_no_update
    BEFORE UPDATE ON job_cancellations
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER job_cancellations_no_delete
    BEFORE DELETE ON job_cancellations
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER sessions_no_update
    BEFORE UPDATE ON sessions
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER sessions_no_delete
    BEFORE DELETE ON sessions
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();
