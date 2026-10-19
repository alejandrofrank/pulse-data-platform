CREATE ROLE pulse_reader LOGIN PASSWORD 'pulse_reader_local';
ALTER ROLE pulse_reader SET default_transaction_read_only = on;
