CREATE SCHEMA lab;
CREATE TABLE lab.jobs (
    id uuid PRIMARY KEY,
    filenames jsonb NOT NULL,
    interval_ms integer NOT NULL CHECK (interval_ms BETWEEN 0 AND 10000),
    status text NOT NULL CHECK (status IN ('queued','running','paused','success','failed','cancelled')),
    cursor integer NOT NULL DEFAULT 0,
    pause_requested boolean NOT NULL DEFAULT false,
    cancel_requested boolean NOT NULL DEFAULT false,
    message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_active_playback ON lab.jobs ((true))
WHERE status IN ('queued','running','paused');
CREATE TABLE lab.steps (
    job_id uuid NOT NULL REFERENCES lab.jobs ON DELETE CASCADE,
    position integer NOT NULL,
    filename text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    started_at timestamptz,
    finished_at timestamptz,
    evidence jsonb,
    message text,
    PRIMARY KEY(job_id,position)
);
