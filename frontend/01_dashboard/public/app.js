/* All displayed values come from the read-only API. Never inject source data as HTML. */
const el = (id) => document.getElementById(id);
const format = (value, style = "number") =>
  value === null || value === undefined
    ? "—"
    : new Intl.NumberFormat(
        "es-ES",
        style === "money"
          ? { style: "currency", currency: "EUR", maximumFractionDigits: 2 }
          : { maximumFractionDigits: 2 },
      ).format(Number(value));
const dateText = (value) =>
  value ? new Date(value).toLocaleString("es-ES") : "Sin cargas";
let offset = 0;
let requestVersion = 0;
let qualityRequestVersion = 0;
let firmRequestVersion = 0;
const errors = new Map();

async function get(url) {
  const response = await fetch(url);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`API ${response.status}: ${body}`);
  }
  return response.json();
}
function showError(error, source = "details") {
  if (error) errors.set(source, error.message);
  else errors.delete(source);
  el("error").textContent = [...errors.values()].join(" · ");
  el("error").hidden = errors.size === 0;
}
function showDetails(value) {
  el("detail-content").textContent = JSON.stringify(value, null, 2);
  el("details").showModal();
}
function table(
  id,
  columns,
  rows,
  emptyMessage = "No hay registros para esta selección.",
) {
  const head = el(id).querySelector("thead");
  const body = el(id).querySelector("tbody");
  head.replaceChildren();
  body.replaceChildren();
  const heading = document.createElement("tr");
  for (const col of columns) {
    const th = document.createElement("th");
    th.textContent = col.label;
    if (col.numeric) th.className = "numeric";
    heading.append(th);
  }
  head.append(heading);
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = columns.length;
    td.className = "empty";
    td.textContent = emptyMessage;
    tr.append(td);
    body.append(tr);
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const col of columns) {
      const td = document.createElement("td");
      if (col.numeric) td.className = "numeric";
      if (col.action) {
        const button = document.createElement("button");
        button.className = "detail-button";
        button.textContent = col.label;
        button.onclick = () => col.action(row);
        td.append(button);
      } else
        td.textContent = col.render ? col.render(row) : (row[col.key] ?? "—");
      tr.append(td);
    }
    body.append(tr);
  }
}
function drawChart(rows) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 1100 240");
  const add = (tag, attrs, text) => {
    const n = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text) n.textContent = text;
    svg.append(n);
    return n;
  };
  if (!rows.length) {
    add("text", { x: 30, y: 100 }, "No hay datos en este periodo.");
    el("chart").replaceChildren(svg);
    return;
  }
  const max = Math.max(1, ...rows.map((r) => Number(r.revenue_eur) || 0));
  const width = 980 / rows.length;
  for (let i = 0; i < 4; i++) {
    const y = 20 + i * 60;
    add("line", { x1: 85, y1: y, x2: 1080, y2: y, class: "chart-grid" });
    add(
      "text",
      { x: 75, y: y + 4, "text-anchor": "end" },
      format((max * (3 - i)) / 3),
    );
  }
  rows.forEach((r, i) => {
    if (r.revenue_eur !== null && r.revenue_eur !== undefined) {
      const h = (180 * Number(r.revenue_eur)) / max;
      const bar = add("rect", {
        x: 90 + i * width,
        y: 200 - h,
        width: Math.max(2, width - 4),
        height: h,
        class: r.missing_firms > 0 ? "chart-bar is-partial" : "chart-bar",
        rx: 2,
      });
      const title = document.createElementNS(ns, "title");
      title.textContent = `${r.mes.slice(0, 7)}: ${format(r.revenue_eur, "money")}${r.missing_firms > 0 ? " · Cobertura parcial" : ""}`;
      bar.append(title);
    }
    if (
      i % Math.max(1, Math.ceil(rows.length / 8)) === 0 ||
      i === rows.length - 1
    )
      add("text", { x: 90 + i * width, y: 225 }, r.mes.slice(0, 7));
  });
  el("chart").replaceChildren(svg);
}
function clearMetrics(message) {
  document
    .querySelectorAll(".latest-month")
    .forEach((n) => (n.textContent = "—"));
  for (const id of ["revenue", "clients", "coverage"]) el(id).textContent = "—";
  el("coverage-detail").textContent = message;
  el("coverage-card").dataset.state = "unknown";
  el("chart").textContent = message;
  table("metrics", [{ label: "Métricas" }], [], message);
}
async function loadMain() {
  const version = ++requestVersion;
  el("loading").textContent = "Cargando…";
  showError(null, "metrics");
  showError(null, "details");
  clearMetrics("Cargando métricas de esta selección…");
  try {
    const params = new URLSearchParams({ limit: 500 });
    if (el("start").value) params.set("start", el("start").value);
    if (el("end").value) params.set("end", el("end").value);
    const firm = el("firm").value;
    const [metrics, quality] = await Promise.all([
      get(`/api/v1/${firm ? `firms/${firm}` : "network"}/metrics?${params}`),
      get("/api/v1/quality/summary"),
    ]);
    if (version !== requestVersion) return;
    const rows = metrics.items;
    const latest = rows.at(-1);
    document
      .querySelectorAll(".latest-month")
      .forEach((n) => (n.textContent = latest?.mes.slice(0, 7) || "sin datos"));
    el("revenue").textContent = format(latest?.revenue_eur, "money");
    el("clients").textContent = format(latest?.clientes_activos);
    el("coverage").textContent = latest
      ? firm
        ? latest.coverage_status === "available"
          ? "100 %"
          : "0 %"
        : `${format(latest.coverage_pct)} %`
      : "—";
    el("coverage-detail").textContent = firm
      ? "Estado del mes de la asesoría"
      : `${latest?.reporting_firms ?? 0} / ${latest?.expected_firms ?? 0} asesorías con datos`;
    el("coverage-card").dataset.state = !latest
      ? "unknown"
      : (
            firm
              ? latest.coverage_status === "available"
              : latest.missing_firms === 0
          )
        ? "success"
        : "warning";
    el("rejected").textContent = format(quality.rejected_rows);
    el("rejected-card").dataset.state =
      quality.rejected_rows > 0 ? "error" : "success";
    el("quality-detail").textContent =
      `${format(quality.warning_rows)} filas con advertencias · ${quality.file_errors} errores de archivo`;
    el("quality-summary").textContent =
      `${format(quality.raw_rows)} filas originales · ${format(quality.accepted_rows)} aceptadas · ${format(quality.curated_rows)} registros publicados · ${quality.duplicate_rows} duplicados · ${quality.missing_firm_months} meses ausentes · ${quality.fallback_rows} valores anteriores retenidos`;
    el("last-load").textContent =
      `Última incorporación: ${dateText(quality.last_successful_load)}`;
    el("last-run").textContent = quality.latest_run
      ? `Última ejecución: ${quality.latest_run.status} · ${dateText(quality.latest_run.started_at)} · ${quality.latest_run.files_loaded} nuevos archivos · ${quality.latest_run.files_skipped} ya procesados`
      : "Sin ejecuciones";
    drawChart(rows);
    const cols = [
      { label: "Mes", render: (r) => r.mes.slice(0, 7) },
      {
        label: "Facturación",
        numeric: true,
        render: (r) => format(r.revenue_eur, "money"),
      },
      {
        label: "Clientes activos",
        numeric: true,
        render: (r) => format(r.clientes_activos),
      },
      {
        label: "Altas",
        numeric: true,
        render: (r) => format(r.clientes_nuevos),
      },
      { label: "Bajas", numeric: true, render: (r) => format(r.clientes_baja) },
      {
        label: "Horas",
        numeric: true,
        render: (r) => format(r.horas_trabajadas),
      },
      ...(!firm
        ? [
            {
              label: "Cobertura de horas",
              render: (r) =>
                r.hours_reporting_firms == null
                  ? "Sin información"
                  : `${r.hours_reporting_firms}/${r.expected_firms} asesorías${r.hours_reporting_firms < r.expected_firms ? " · Parcial" : ""}`,
            },
          ]
        : []),
      {
        label: "Satisfacción",
        numeric: true,
        render: (r) =>
          format(firm ? r.satisfaccion_cliente : r.satisfaction_firm_average),
      },
      {
        label: "Cobertura de asesorías",
        render: (r) =>
          firm ? r.coverage_status : `${r.reporting_firms}/${r.expected_firms}`,
      },
    ];
    if (firm)
      cols.push({
        label: "Origen",
        action: async (r) => {
          showError(null, "details");
          try {
            showDetails(
              await get(`/api/v1/lineage/${firm}/${r.mes.slice(0, 7)}`),
            );
          } catch (e) {
            showError(e);
          }
        },
      });
    table("metrics", cols, rows);
    el("loading").textContent = `${rows.length} meses`;
  } catch (error) {
    if (version === requestVersion) {
      clearMetrics("No se pudieron cargar las métricas de esta selección.");
      showError(error, "metrics");
      el("loading").textContent = "Error de carga";
    }
  }
}
async function loadQuality() {
  const version = ++qualityRequestVersion;
  const selection = el("quality-view").value;
  const pageOffset = offset;
  showError(null, "quality");
  table("quality", [{ label: "Calidad" }], [], "Cargando…");
  el("page-info").textContent = "Cargando…";
  try {
    const data = await get(
      `/api/v1/quality/${selection}${selection.includes("?") ? "&" : "?"}limit=20&offset=${pageOffset}`,
    );
    if (version !== qualityRequestVersion) return;
    let columns;
    if (selection.startsWith("issues"))
      columns = [
        { label: "Archivo", key: "filename" },
        { label: "Fila", key: "line_number" },
        { label: "Regla", key: "code" },
        { label: "Campo", key: "field" },
        { label: "Motivo", key: "message" },
      ];
    else if (selection === "gaps")
      columns = [
        { label: "Mes", render: (r) => r.mes.slice(0, 7) },
        { label: "Asesoría", key: "asesoria_id" },
        { label: "Nombre", key: "nombre" },
        { label: "Estado", key: "status" },
      ];
    else if (selection === "deliveries")
      columns = [
        { label: "Archivo", key: "filename" },
        { label: "Formato", key: "schema_version" },
        { label: "Originales", key: "raw_rows" },
        { label: "Aceptadas", key: "accepted_rows" },
        { label: "Rechazadas", key: "rejected_rows" },
        { label: "Duplicadas", key: "duplicate_rows" },
      ];
    else if (selection === "runs")
      columns = [
        { label: "Inicio", render: (r) => dateText(r.started_at) },
        { label: "Estado", key: "status" },
        { label: "Cargados", key: "files_loaded" },
        { label: "Omitidos", key: "files_skipped" },
        { label: "Fallidos", key: "files_failed" },
      ];
    else
      columns = [
        { label: "Archivo", key: "filename" },
        { label: "Motivo", key: "reason" },
        { label: "Última detección", render: (r) => dateText(r.last_seen_at) },
        { label: "Resuelto", render: (r) => (r.resolved_at ? "Sí" : "No") },
      ];
    columns.push({ label: "Detalle", action: showDetails });
    table("quality", columns, data.items);
    el("page-info").textContent =
      `${data.items.length ? pageOffset + 1 : 0}–${data.items.length ? pageOffset + data.items.length : 0} de ${data.total}`;
    el("previous").disabled = pageOffset === 0;
    el("next").disabled = pageOffset + 20 >= data.total;
  } catch (error) {
    if (version !== qualityRequestVersion) return;
    table(
      "quality",
      [{ label: "Calidad" }],
      [],
      "No se pudo cargar esta selección.",
    );
    el("page-info").textContent = "Error de carga";
    showError(error, "quality");
  }
}
async function loadFirms() {
  const version = ++firmRequestVersion;
  showError(null, "firms");
  try {
    const data = await get("/api/v1/firms?limit=500");
    if (version !== firmRequestVersion) return;
    // Preserve the current choice, including changes made while this request was pending.
    const selected = el("firm").value;
    const options = data.items.map((f) => {
      const option = document.createElement("option");
      option.value = f.id;
      option.textContent = `${f.id} · ${f.nombre}`;
      return option;
    });
    el("firm").replaceChildren(new Option("Toda la red", ""), ...options);
    el("firm").value = options.some((option) => option.value === selected)
      ? selected
      : "";
    if (el("firm").value !== selected) loadMain();
  } catch (error) {
    if (version === firmRequestVersion) showError(error, "firms");
  }
}
el("filters").onsubmit = (e) => {
  e.preventDefault();
  loadMain();
};
el("refresh").onclick = () => {
  loadMain();
  loadQuality();
  loadFirms();
};
el("quality-view").onchange = () => {
  offset = 0;
  loadQuality();
};
el("previous").onclick = () => {
  offset = Math.max(0, offset - 20);
  loadQuality();
};
el("next").onclick = () => {
  offset += 20;
  loadQuality();
};
el("close-dialog").onclick = () => el("details").close();
loadFirms();
loadMain();
loadQuality();
