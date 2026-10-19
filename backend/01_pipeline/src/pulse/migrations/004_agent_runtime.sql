CREATE SCHEMA agents;
CREATE TABLE agents.conversations (
    id uuid PRIMARY KEY,
    owner text NOT NULL,
    profile text NOT NULL CHECK (profile IN ('business','investigation')),
    state jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE agents.runs (
    id uuid PRIMARY KEY,
    conversation_id uuid NOT NULL REFERENCES agents.conversations,
    owner text NOT NULL,
    profile text NOT NULL CHECK (profile IN ('business','investigation')),
    question text NOT NULL CHECK (length(question) BETWEEN 1 AND 2000),
    request_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('running','complete','error')),
    reserved_cost_usd numeric(12,6) NOT NULL CHECK (reserved_cost_usd>=0),
    actual_cost_usd numeric(12,8) CHECK (actual_cost_usd>=0),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX agent_runs_admission ON agents.runs(status,created_at);
CREATE INDEX agent_runs_owner ON agents.runs(owner,created_at);
CREATE INDEX agent_runs_conversation ON agents.runs(conversation_id,created_at);

-- Local Compose development credential, not an OpenAI secret. Production must
-- provision its own password and scoped audit URL before external exposure.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='pulse_agent_writer') THEN
        CREATE ROLE pulse_agent_writer LOGIN PASSWORD 'pulse_agent_writer_local';
    END IF;
END $$;
GRANT USAGE ON SCHEMA agents TO pulse_agent_writer;
GRANT SELECT,INSERT,UPDATE ON agents.conversations,agents.runs TO pulse_agent_writer;
