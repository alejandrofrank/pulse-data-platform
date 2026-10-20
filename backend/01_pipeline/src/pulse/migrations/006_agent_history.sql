-- NULL investigation permission marks legacy/unknown authorization. Do not
-- backfill it from results: even an unanswered question may contain evidence.
ALTER TABLE agents.runs
    ADD COLUMN authorization_firm_ids bigint[],
    ADD COLUMN authorization_can_investigate boolean;

CREATE INDEX agent_conversations_history
    ON agents.conversations(owner,updated_at DESC,id DESC);
CREATE INDEX agent_runs_history
    ON agents.runs(conversation_id,created_at DESC,id DESC);

-- Existing table-level SELECT/INSERT/UPDATE grants to pulse_agent_writer cover
-- the new columns. Reporting roles receive no access to agent history.
