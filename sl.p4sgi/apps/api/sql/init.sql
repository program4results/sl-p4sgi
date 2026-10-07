-- SL-P4SGI Phase 3 schema (runs on first Postgres boot via docker-entrypoint-initdb.d)
-- Idempotent-friendly; API also ensures schema on startup.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS schools (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    emis            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    email           TEXT,
    district        TEXT,
    province        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ous (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID REFERENCES schools(id) ON DELETE SET NULL,
    emis            TEXT,
    ou_path         TEXT NOT NULL,
    ou_name         TEXT,
    primary_email   TEXT,
    raw             JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ous_emis ON ous(emis);
CREATE INDEX IF NOT EXISTS idx_ous_ou_path ON ous(ou_path);

CREATE TABLE IF NOT EXISTS school_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID REFERENCES schools(id) ON DELETE SET NULL,
    emis            TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    config          JSONB NOT NULL,
    source_job_id   UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (emis, version)
);

CREATE INDEX IF NOT EXISTS idx_school_configs_emis ON school_configs(emis);

CREATE TABLE IF NOT EXISTS provisioning_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected', 'failed')),
    source_filename TEXT,
    source_format   TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    emis            TEXT,
    school_name     TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at     TIMESTAMPTZ,
    approved_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_provisioning_jobs_status ON provisioning_jobs(status);
CREATE INDEX IF NOT EXISTS idx_provisioning_jobs_emis ON provisioning_jobs(emis);

-- Publish audit: school-config publishes (Phase 2) + operational webhook events (Phase 3)
CREATE TABLE IF NOT EXISTS publish_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES provisioning_jobs(id) ON DELETE SET NULL,
    school_config_id UUID REFERENCES school_configs(id) ON DELETE SET NULL,
    emis            TEXT NOT NULL,
    config_path     TEXT,
    config_version  INTEGER,
    actor           TEXT DEFAULT 'system',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Phase 3: tablet / Apps Script webhook fields
    kind            TEXT,
    school_email    TEXT,
    doc_id          TEXT,
    status          TEXT,
    payload_preview TEXT
);

CREATE INDEX IF NOT EXISTS idx_publish_events_emis ON publish_events(emis);
CREATE INDEX IF NOT EXISTS idx_publish_events_created ON publish_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_publish_events_kind ON publish_events(kind);

-- Phase 3 migrations for DBs created in Phase 1/2
ALTER TABLE publish_events ALTER COLUMN config_path DROP NOT NULL;
ALTER TABLE publish_events ADD COLUMN IF NOT EXISTS kind TEXT;
ALTER TABLE publish_events ADD COLUMN IF NOT EXISTS school_email TEXT;
ALTER TABLE publish_events ADD COLUMN IF NOT EXISTS doc_id TEXT;
ALTER TABLE publish_events ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE publish_events ADD COLUMN IF NOT EXISTS payload_preview TEXT;

-- Seed Test Primary school row (config file written on approve / seed script)
INSERT INTO schools (emis, name, email, district, province)
VALUES ('110101', 'Test Primary School', 'sl-test@sl.p4sgi.com', 'Pilot', 'Sierra Leone')
ON CONFLICT (emis) DO NOTHING;

-- Phase 4a: fleet device registry + telemetry stubs
CREATE TABLE IF NOT EXISTS devices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    emis                TEXT NOT NULL,
    school_email        TEXT,
    tablet_android_id   TEXT,
    serial              TEXT,
    device_type         TEXT,
    sim                 TEXT,
    whatsapp            TEXT,
    app_version         TEXT,
    last_seen           TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_devices_emis ON devices(emis);
CREATE INDEX IF NOT EXISTS idx_devices_school_email ON devices(school_email);
CREATE INDEX IF NOT EXISTS idx_devices_tablet_android_id ON devices(tablet_android_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_tablet_android_id
  ON devices (tablet_android_id)
  WHERE tablet_android_id IS NOT NULL AND tablet_android_id <> '';

CREATE TABLE IF NOT EXISTS telemetry_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id   UUID REFERENCES devices(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL
                CHECK (kind IN (
                    'skill_run', 'prompt', 'publish',
                    'llm_local', 'llm_online', 'data_mb', 'radar_point'
                )),
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_device ON telemetry_events(device_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_kind ON telemetry_events(kind);
CREATE INDEX IF NOT EXISTS idx_telemetry_created ON telemetry_events(created_at DESC);



-- Phase 0.4.3: device identity fields (serial / model) for fleet table
ALTER TABLE devices ADD COLUMN IF NOT EXISTS serial TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS device_type TEXT;

-- Phase 0.4.17: Edge Attendance outbox ingest (edge-attendance/1)
CREATE TABLE IF NOT EXISTS attendance_sync_events (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id                  TEXT NOT NULL UNIQUE,
    schema_version              TEXT,
    emis_code                   TEXT NOT NULL,
    school_email                TEXT,
    class_label                 TEXT,
    attendance_date             DATE,
    detected_count              INTEGER,
    confirmed_count             INTEGER,
    absent_named                JSONB NOT NULL DEFAULT '[]'::jsonb,
    mean_confidence             DOUBLE PRECISION,
    manual_review_required      BOOLEAN,
    photo_present_at_capture    BOOLEAN,
    photo_retained              BOOLEAN,
    model_version               TEXT,
    device_serial               TEXT,
    synced_at                   TIMESTAMPTZ,
    province                    TEXT,
    district                    TEXT,
    payload                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_attendance_sync_emis_date
  ON attendance_sync_events (emis_code, attendance_date);
CREATE INDEX IF NOT EXISTS idx_attendance_sync_province
  ON attendance_sync_events (province);
