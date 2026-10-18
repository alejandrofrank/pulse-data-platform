CREATE SCHEMA ops;
CREATE SCHEMA raw;
CREATE SCHEMA core;
CREATE SCHEMA mart;

CREATE TABLE ops.runs (
    id uuid PRIMARY KEY,
    dagster_run_id text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running','success','partial','failed','abandoned')),
    policy_version text NOT NULL,
    files_loaded integer NOT NULL DEFAULT 0,
    files_skipped integer NOT NULL DEFAULT 0,
    files_failed integer NOT NULL DEFAULT 0,
    message text
);
CREATE TABLE raw.master (
    sha256 text PRIMARY KEY,
    content bytea NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE core.firms (
    id integer PRIMARY KEY,
    nombre text NOT NULL,
    cif text NOT NULL,
    provincia text NOT NULL,
    ciudad text NOT NULL,
    fecha_alta date NOT NULL,
    num_empleados integer NOT NULL CHECK (num_empleados >= 0),
    especialidad text NOT NULL
);
CREATE TABLE raw.deliveries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename text NOT NULL UNIQUE,
    delivery_month date NOT NULL,
    revision integer NOT NULL,
    sha256 text NOT NULL,
    content bytea NOT NULL,
    schema_version integer NOT NULL,
    policy_version text NOT NULL,
    run_id uuid NOT NULL REFERENCES ops.runs,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(delivery_month, revision)
);
CREATE TABLE raw.records (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    delivery_id bigint NOT NULL REFERENCES raw.deliveries,
    line_number integer NOT NULL,
    payload jsonb NOT NULL,
    normalized jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('accepted','rejected','duplicate')),
    UNIQUE(delivery_id, line_number)
);
CREATE INDEX records_key ON raw.records ((normalized->>'asesoria_id'),(normalized->>'mes'));
CREATE TABLE ops.issues (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id bigint NOT NULL REFERENCES raw.records,
    severity text NOT NULL CHECK (severity IN ('error','warning','info')),
    code text NOT NULL,
    field text,
    message text NOT NULL
);
CREATE INDEX issues_record ON ops.issues(record_id);
CREATE INDEX issues_severity ON ops.issues(severity);
CREATE TABLE ops.file_errors (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename text NOT NULL,
    sha256 text NOT NULL,
    content bytea NOT NULL,
    reason text NOT NULL,
    run_id uuid NOT NULL REFERENCES ops.runs,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE(filename, sha256, reason)
);
CREATE TABLE mart.firm_month (
    asesoria_id integer NOT NULL REFERENCES core.firms,
    mes date NOT NULL CHECK (extract(day FROM mes)=1),
    clientes_activos integer NOT NULL CHECK (clientes_activos>=0),
    clientes_nuevos integer NOT NULL CHECK (clientes_nuevos>=0),
    clientes_baja integer NOT NULL CHECK (clientes_baja>=0),
    declaraciones_renta integer NOT NULL CHECK (declaraciones_renta>=0),
    declaraciones_iva integer NOT NULL CHECK (declaraciones_iva>=0),
    declaraciones_sociedades integer NOT NULL CHECK (declaraciones_sociedades>=0),
    declaraciones_otros integer NOT NULL CHECK (declaraciones_otros>=0),
    consultas_recibidas integer NOT NULL CHECK (consultas_recibidas>=0),
    consultas_resueltas integer NOT NULL CHECK (consultas_resueltas>=0),
    incidencias_aeat integer NOT NULL CHECK (incidencias_aeat>=0),
    documentos_procesados integer CHECK (documentos_procesados>=0),
    facturacion_asesoria_eur numeric(18,2) NOT NULL CHECK (facturacion_asesoria_eur>=0),
    facturacion_gestion_eur numeric(18,2) NOT NULL CHECK (facturacion_gestion_eur>=0),
    facturacion_consultoria_eur numeric(18,2) NOT NULL CHECK (facturacion_consultoria_eur>=0),
    facturacion_otros_eur numeric(18,2) NOT NULL CHECK (facturacion_otros_eur>=0),
    horas_trabajadas numeric(18,2) CHECK (horas_trabajadas>=0),
    satisfaccion_cliente numeric(3,2) CHECK (satisfaccion_cliente BETWEEN 1 AND 5),
    source_record_id bigint NOT NULL UNIQUE REFERENCES raw.records,
    has_rejected_newer_delivery boolean NOT NULL DEFAULT false,
    published_run_id uuid NOT NULL REFERENCES ops.runs,
    PRIMARY KEY(asesoria_id,mes)
);
CREATE INDEX firm_month_month ON mart.firm_month(mes);

