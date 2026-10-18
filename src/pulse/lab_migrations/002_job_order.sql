-- Wall clocks can move backwards; use a sequence to order accepted playback requests.
ALTER TABLE lab.jobs ADD COLUMN sequence bigint GENERATED ALWAYS AS IDENTITY;
CREATE UNIQUE INDEX playback_sequence ON lab.jobs (sequence);
