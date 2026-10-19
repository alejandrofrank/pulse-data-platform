/* A direct PostgreSQL workspace. Query text is submitted unchanged, never built from filters. */
(() => {
  const el = (id) => document.getElementById(id);
  const editor = el("sql-editor");
  const draftKey = "pulse.lab.sql.draft.v1";
  let running = false;
  const examples = {
    "Entregas y calidad": `SELECT d.filename, r.status, COUNT(*) AS rows
FROM raw.records r
JOIN raw.deliveries d ON d.id = r.delivery_id
GROUP BY d.filename, r.status
ORDER BY d.filename, r.status;`,
    "Datos y CSV de origen": `SELECT f.asesoria_id, f.mes, f.clientes_activos,
       f.facturacion_asesoria_eur, d.filename AS source_csv,
       r.line_number, f.has_rejected_newer_delivery
FROM mart.firm_month f
JOIN raw.records r ON r.id = f.source_record_id
JOIN raw.deliveries d ON d.id = r.delivery_id
ORDER BY f.mes DESC, f.asesoria_id;`,
    "Versiones y correcciones": `SELECT r.normalized->>'asesoria_id' AS asesoria_id,
       r.normalized->>'mes' AS mes, d.filename, d.revision,
       r.status, r.normalized, r.payload
FROM raw.records r
JOIN raw.deliveries d ON d.id = r.delivery_id
WHERE r.normalized->>'asesoria_id' = '1'
ORDER BY mes, d.delivery_month, d.revision;`,
    Incidencias: `SELECT d.filename, r.line_number, i.severity, i.code,
       i.field, i.message, r.payload
FROM ops.issues i
JOIN raw.records r ON r.id = i.record_id
JOIN raw.deliveries d ON d.id = r.delivery_id
ORDER BY i.id DESC;`,
    "Historial de ingestas": `SELECT started_at, status, files_loaded, files_skipped,
       files_failed, message
FROM ops.runs
ORDER BY started_at DESC;`,
  };
  function node(tag, text) {
    const n = document.createElement(tag);
    if (text != null) n.textContent = text;
    return n;
  }
  function save() {
    try {
      localStorage.setItem(draftKey, editor.value);
    } catch {
      /* Storage is optional. */
    }
  }
  try {
    editor.value = localStorage.getItem(draftKey) ?? editor.value;
  } catch {
    /* Private browser. */
  }
  editor.addEventListener("input", save);
  function insert(text) {
    editor.setRangeText(
      text,
      editor.selectionStart,
      editor.selectionEnd,
      "end",
    );
    editor.focus();
    save();
  }
  const quote = (name) => `"${name.replaceAll('"', '""')}"`;
  async function request(path, body) {
    const response = await fetch(
      path,
      body === undefined
        ? {}
        : {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Pulse-Lab": "playback-v1",
            },
            body: JSON.stringify(body),
          },
    );
    const data = await response.json();
    if (!response.ok) {
      const detail = data.detail;
      throw new Error(
        typeof detail === "string"
          ? detail
          : detail?.message
            ? `${detail.sqlstate ? `[${detail.sqlstate}] ` : ""}${detail.message}${detail.hint ? `\n${detail.hint}` : ""}`
            : JSON.stringify(detail),
      );
    }
    return data;
  }
  async function loadSchema() {
    el("sql-schema-refresh").disabled = true;
    try {
      const data = await request("/api/lab/sql/schema");
      el("sql-connection").textContent =
        `${data.database} · ${data.role} · solo lectura`;
      const tree = document.createDocumentFragment();
      for (const table of data.tables) {
        const item = node("details");
        item.className = "sql-table";
        item.append(node("summary", `${table.schema}.${table.name}`));
        const button = node("button", "Insertar tabla");
        button.addEventListener("click", () =>
          insert(`${quote(table.schema)}.${quote(table.name)}`),
        );
        item.append(button);
        for (const column of table.columns) {
          const row = node("div");
          row.className = "sql-column";
          const field = node("button", column.column);
          field.title = `Insertar ${column.column}`;
          field.addEventListener("click", () => insert(quote(column.column)));
          row.append(
            field,
            node(
              "small",
              `${column.type}${column.primary_key ? " · PK" : ""}${column.nullable ? "" : " · NOT NULL"}`,
            ),
          );
          item.append(row);
        }
        tree.append(item);
      }
      el("sql-tables").replaceChildren(tree);
    } catch (error) {
      el("sql-connection").textContent = "Esquema no disponible";
      el("sql-tables").textContent = error.message;
    } finally {
      el("sql-schema-refresh").disabled = false;
    }
  }
  function render(data) {
    const head = node("tr");
    for (const c of data.columns) {
      const cell = node("th", c.name);
      cell.scope = "col";
      cell.append(node("small", c.type));
      head.append(cell);
    }
    const body = document.createDocumentFragment();
    for (const values of data.rows) {
      const row = node("tr");
      for (const value of values) {
        const cell = node("td", value === null ? "NULL" : value);
        if (value === null) cell.dataset.null = "true";
        row.append(cell);
      }
      body.append(row);
    }
    el("sql-results").querySelector("thead").replaceChildren(head);
    el("sql-results").querySelector("tbody").replaceChildren(body);
    el("sql-results-wrap").hidden = false;
    const suffix = data.truncated
      ? data.truncation_reason === "byte_limit"
        ? " · Resultado recortado: límite de 2 MB; selecciona menos columnas o usa substring()."
        : ` · Hay más filas; límite de vista ${data.limit}. Usa filtros, LIMIT/OFFSET o agregaciones.`
      : "";
    el("sql-status").textContent =
      `${data.explain ? "Plan" : "Resultado"}: ${data.row_count} filas · ${data.elapsed_ms} ms${suffix}`;
    el("sql-snapshot").textContent =
      `Lectura: ${new Date(data.snapshot_at).toLocaleString("es-ES")}`;
  }
  async function execute(explain = false) {
    if (running) return;
    const selected = editor.value.slice(
      editor.selectionStart,
      editor.selectionEnd,
    );
    const sql = selected.trim() ? selected : editor.value;
    if (!sql.trim()) {
      editor.focus();
      return;
    }
    running = true;
    el("sql-run").disabled = el("sql-plan").disabled = true;
    el("sql-error").hidden = true;
    el("sql-results-wrap").hidden = true;
    el("sql-snapshot").textContent = "";
    el("sql-status").textContent = explain ? "Calculando plan…" : "Ejecutando…";
    el("sql-executed").hidden = false;
    el("sql-executed-text").textContent = sql;
    save();
    try {
      render(
        await request("/api/lab/sql", {
          sql,
          limit: Number(el("sql-limit").value),
          explain,
        }),
      );
    } catch (error) {
      el("sql-status").textContent = "La consulta no se completó.";
      el("sql-error").textContent = error.message;
      el("sql-error").hidden = false;
    } finally {
      running = false;
      el("sql-run").disabled = el("sql-plan").disabled = false;
    }
  }
  editor.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      execute();
    }
  });
  for (const [name, text] of Object.entries(examples)) {
    const button = node("button", name);
    button.addEventListener("click", () => {
      editor.value = text;
      editor.focus();
      save();
    });
    el("sql-examples").append(button);
  }
  el("sql-run").addEventListener("click", () => execute());
  el("sql-plan").addEventListener("click", () => execute(true));
  el("sql-schema-refresh").addEventListener("click", loadSchema);
  loadSchema();
})();
