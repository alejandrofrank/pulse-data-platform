/* Controls only the isolated lab; every displayed result comes from its API. */
const node = (id) => document.getElementById(id);
const number = (value) => new Intl.NumberFormat("es-ES").format(value);
const money = (value) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("es-ES", {
        style: "currency",
        currency: "EUR",
      }).format(Number(value));
const labels = {
  pending: "Pendiente",
  queued: "En cola",
  running: "En curso",
  paused: "En pausa",
  success: "Completado",
  failed: "Fallo",
  cancelled: "Detenido",
  loaded: "Cargado",
  skipped: "Omitido",
  changed: "Modificado",
};
let state = null;
let selection = [];
let catalogSignature = null;
let busy = false;
let polling = false;
let connected = false;
let pollTimer;
let pendingPlay = null;
let lastPaint = "";
let toplineMonths = "";
let toplineChart = "";
const catalogNodes = new Map();
const errors = new Map();

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text != null) item.textContent = text;
  if (className) item.className = className;
  return item;
}
async function api(path, body) {
  const options =
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Pulse-Lab": "playback-v1",
          },
          body: JSON.stringify(body),
        };
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(
      typeof result.detail === "string"
        ? result.detail
        : JSON.stringify(result.detail),
    );
    error.httpStatus = response.status;
    throw error;
  }
  return result;
}
function errorMessage(error, source = "action") {
  if (error) errors.set(source, error.message);
  else errors.delete(source);
  node("error").textContent = [...errors.values()].join(" · ");
  node("error").hidden = errors.size === 0;
}
function details(value, title = "Evidencia de la entrega") {
  node("evidence-title").textContent = title;
  node("evidence-content").textContent = JSON.stringify(value, null, 2);
  node("evidence-dialog").showModal();
}
function selectedFile() {
  return state?.catalog[Number(node("playhead").value) - 1];
}
function controls() {
  const status = state?.job?.status;
  const active = ["queued", "running", "paused"].includes(status);
  const unavailable = busy || !connected || !state || !state.worker_alive;
  node("play").disabled = unavailable || active || selection.length === 0;
  node("step").disabled = unavailable || active || !selectedFile();
  node("pause").disabled =
    unavailable || !["queued", "running", "paused", "failed"].includes(status);
  node("pause").textContent =
    status === "paused"
      ? "Continuar"
      : status === "failed"
        ? "Reintentar"
        : state?.job?.pause_requested
          ? "Pausando…"
          : "Pausar";
  node("cancel").disabled =
    unavailable || !["queued", "running", "paused"].includes(status);
  node("reset-open").disabled =
    unavailable || ["queued", "running"].includes(status);
}
function cursor() {
  const file = selectedFile();
  node("cursor-label").textContent = file ? file.filename : "00 / Inicio";
  node("cursor-detail").textContent = file
    ? `${file.format || "formato desconocido"} · ${file.rows ?? "?"} filas · ${labels[file.status]}`
    : "Ninguna entrega antes del cursor";
  state?.catalog.forEach((f) =>
    catalogNodes
      .get(f.filename)
      ?.clip.setAttribute(
        "aria-current",
        String(f.filename === file?.filename),
      ),
  );
  controls();
}
function setSelection(names) {
  selection = [...names];
  // A new explicit selection is a new intent; network retries keep the previous request ID.
  pendingPlay = null;
  state?.catalog.forEach((f) => {
    catalogNodes.get(f.filename).checkbox.checked = selection.includes(
      f.filename,
    );
  });
  node("selection-count").textContent = `${selection.length} seleccionados`;
  const queue = node("queue");
  queue.replaceChildren();
  if (!selection.length)
    queue.append(element("li", "Selecciona entregas del catálogo.", "note"));
  selection.forEach((name, index) => {
    const row = element("li");
    row.append(
      element(
        "span",
        `${String(index + 1).padStart(2, "0")} · ${name}`,
        "queue-file",
      ),
    );
    for (const [delta, text] of [
      [-1, "↑"],
      [1, "↓"],
    ]) {
      const button = element("button", text);
      button.setAttribute(
        "aria-label",
        `${delta < 0 ? "Subir" : "Bajar"} ${name}`,
      );
      button.disabled = index + delta < 0 || index + delta >= selection.length;
      button.onclick = () => {
        const next = [...selection];
        [next[index], next[index + delta]] = [next[index + delta], next[index]];
        setSelection(next);
      };
      row.append(button);
    }
    queue.append(row);
  });
  controls();
}
function catalog() {
  const signature = state.catalog.map((f) => f.filename).join("|");
  if (signature !== catalogSignature) {
    const first = catalogSignature === null;
    catalogSignature = signature;
    catalogNodes.clear();
    node("catalog").replaceChildren();
    node("timeline-track").replaceChildren();
    node("timeline-track").style.setProperty(
      "--clips",
      Math.max(1, state.catalog.length),
    );
    node("playhead").max = state.catalog.length;
    state.catalog.forEach((file, index) => {
      const row = element("label", null, "catalog-row");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.setAttribute("aria-label", `Seleccionar ${file.filename}`);
      checkbox.onchange = () =>
        setSelection(
          checkbox.checked
            ? [...selection, file.filename]
            : selection.filter((n) => n !== file.filename),
        );
      const copy = element("span");
      copy.append(element("span", file.filename, "catalog-name"));
      const meta = element("span", null, "catalog-meta");
      copy.append(meta);
      const status = element("span", null, "tag");
      row.append(checkbox, copy, status);
      node("catalog").append(row);
      const clip = element("button", null, "clip");
      clip.append(
        element(
          "span",
          index === 0 || file.month?.slice(5, 7) === "01"
            ? file.month?.slice(0, 4)
            : "",
          "clip-year",
        ),
      );
      clip.append(
        element("span", file.month?.slice(5, 7) || "?"),
        element("span", null, "clip-state"),
      );
      clip.onclick = () => {
        node("playhead").value = index + 1;
        cursor();
      };
      node("timeline-track").append(clip);
      catalogNodes.set(file.filename, { checkbox, status, meta, clip });
    });
    if (first && state.catalog.length) {
      node("playhead").value = 1;
      selection = [state.catalog[0].filename];
    }
    setSelection(
      selection.filter((n) => state.catalog.some((f) => f.filename === n)),
    );
  }
  state.catalog.forEach((file) => {
    const items = catalogNodes.get(file.filename);
    items.status.textContent = labels[file.status];
    items.status.dataset.state = file.status;
    items.meta.textContent = `${file.format || "?"} · ${file.rows ?? "?"} filas${file.rejected ? ` · ${file.rejected} rechazadas` : ""}`;
    items.clip.dataset.state = file.status;
    items.clip.title = `${file.filename} · ${labels[file.status]}`;
    items.clip.setAttribute(
      "aria-label",
      `Cursor en ${file.filename}, ${labels[file.status]}`,
    );
  });
  cursor();
}
function tableRows(id, rows, columns) {
  const body = node(id).querySelector("tbody");
  body.replaceChildren();
  if (!rows.length) {
    const cell = element("td", "Sin registros todavía.", "empty");
    cell.colSpan = columns.length;
    const tr = element("tr");
    tr.append(cell);
    body.append(tr);
    return;
  }
  rows.forEach((row) => {
    const tr = element("tr");
    columns.forEach((render) => {
      const td = element("td");
      const value = render(row);
      if (value instanceof Node) td.append(value);
      else td.textContent = value ?? "—";
      tr.append(td);
    });
    body.append(tr);
  });
}
function detailButton(value, title) {
  const button = element("button", "Ver", "detail-button");
  button.onclick = () => details(value, title);
  return button;
}
function tag(status) {
  const value = element("span", labels[status] || status, "tag");
  value.dataset.state = status;
  return value;
}
function clearTopline(message) {
  for (const id of [
    "topline-revenue",
    "topline-clients",
    "topline-coverage",
    "reject-count",
  ])
    node(id).textContent = "—";
  document
    .querySelectorAll(".topline-date")
    .forEach((item) => (item.textContent = "—"));
  node("topline-coverage-card").dataset.state = "unknown";
  node("lab-quality-card").dataset.state = "unknown";
  node("topline-coverage-detail").textContent = message;
  node("quality-detail").textContent = message;
  node("topline-range").textContent = message;
  node("topline-month").disabled = true;
  node("lab-revenue-chart").textContent = message;
  toplineChart = "";
}
function revenueChart(rows) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 1100 240");
  const add = (tag, attributes, text) => {
    const item = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attributes))
      item.setAttribute(key, value);
    if (text != null) item.textContent = text;
    svg.append(item);
    return item;
  };
  const max = Math.max(1, ...rows.map((row) => Number(row.revenue_eur) || 0));
  const width = 980 / rows.length;
  for (let i = 0; i < 4; i++) {
    const y = 20 + i * 60;
    add("line", { x1: 85, y1: y, x2: 1080, y2: y, class: "chart-grid" });
    add(
      "text",
      { x: 75, y: y + 4, "text-anchor": "end" },
      number(Math.round((max * (3 - i)) / 3)),
    );
  }
  rows.forEach((row, index) => {
    if (row.revenue_eur != null) {
      const height = (180 * Number(row.revenue_eur)) / max;
      const bar = add("rect", {
        x: 90 + index * width,
        y: 200 - height,
        width: Math.max(2, width - 4),
        height,
        rx: 2,
        class: row.missing_firms > 0 ? "chart-bar is-partial" : "chart-bar",
      });
      const title = document.createElementNS(ns, "title");
      title.textContent = `${row.mes.slice(0, 7)}: ${money(row.revenue_eur)} · ${row.reporting_firms}/${row.expected_firms} asesorías`;
      bar.append(title);
    } else {
      add("text", { x: 90 + index * width, y: 195 }, "—");
    }
    if (
      index % Math.max(1, Math.ceil(rows.length / 8)) === 0 ||
      index === rows.length - 1
    )
      add("text", { x: 90 + index * width, y: 225 }, row.mes.slice(0, 7));
  });
  node("lab-revenue-chart").replaceChildren(svg);
}
function renderTopline() {
  const rows = state.months;
  const select = node("topline-month");
  const signature = JSON.stringify(rows.map((row) => row.mes));
  if (signature !== toplineMonths) {
    toplineMonths = signature;
    const previous = select.value;
    const automatic = element("option", "Último mes disponible · en vivo");
    automatic.value = "";
    select.replaceChildren(
      automatic,
      ...rows.map((row) => {
        const option = element("option", row.mes.slice(0, 7));
        option.value = row.mes;
        return option;
      }),
    );
    select.value = rows.some((row) => row.mes === previous) ? previous : "";
  }
  if (!rows.length) {
    clearTopline("Sin meses publicados. Reproduce una selección de CSV.");
    return;
  }
  select.disabled = false;
  const selected = rows.find((row) => row.mes === select.value) || rows.at(-1);
  node("topline-range").textContent =
    `${rows.length} meses en el intervalo · ${rows[0].mes.slice(0, 7)} — ${rows.at(-1).mes.slice(0, 7)} · pulse_lab`;
  document
    .querySelectorAll(".topline-date")
    .forEach((item) => (item.textContent = selected.mes.slice(0, 7)));
  node("topline-revenue").textContent = money(selected.revenue_eur);
  node("topline-clients").textContent =
    selected.clientes_activos == null ? "—" : number(selected.clientes_activos);
  node("topline-coverage").textContent =
    selected.coverage_pct == null ? "—" : `${number(selected.coverage_pct)} %`;
  node("topline-coverage-detail").textContent =
    `${selected.reporting_firms} / ${selected.expected_firms} asesorías con datos`;
  node("topline-coverage-card").dataset.state =
    selected.missing_firms > 0 ? "warning" : "success";
  const chartSignature = JSON.stringify(rows);
  if (chartSignature !== toplineChart) {
    toplineChart = chartSignature;
    revenueChart(rows);
  }
}
function render() {
  catalog();
  renderTopline();
  const c = state.counts;
  node("loaded-count").textContent = number(c.files);
  node("catalog-count").textContent = `${state.catalog.length} CSV disponibles`;
  node("raw-count").textContent = number(c.raw_rows);
  node("raw-detail").textContent =
    `${number(c.accepted_rows)} aceptadas · ${number(c.duplicate_rows)} duplicadas`;
  node("published-count").textContent = number(c.published_rows);
  node("reject-count").textContent = number(c.rejected_rows);
  node("quality-detail").textContent =
    `${c.warning_rows} advertencias · ${c.gaps} huecos · ${c.file_errors} archivos con error`;
  node("lab-quality-card").dataset.state =
    c.rejected_rows || c.file_errors ? "error" : "success";
  const job = state.job;
  node("job-status").textContent = job
    ? labels[job.status]
    : "Listo para empezar";
  node("job-progress").textContent = job
    ? `${job.cursor} / ${job.filenames.length} entregas completadas`
    : "Sin reproducciones";
  node("progress").max = job?.filenames.length || 1;
  node("progress").value = job?.cursor || 0;
  const current = job?.steps.find((s) => s.status === "running");
  node("job-message").textContent = current
    ? `Procesando ${current.filename}. Pausar espera a que termine este CSV.`
    : job?.message ||
      "El cursor selecciona; no borra ni revierte datos. Pausar termina el CSV en curso.";
  node("run-label").textContent = job ? job.id.slice(0, 8) : "Sin ejecución";
  node("checks").replaceChildren();
  const pending = ["running", "queued"].includes(job?.status);
  state.checks.forEach((check) => {
    const value = element(
      "span",
      `${pending ? "…" : check.passed ? "✓" : "×"} ${check.label}`,
      "check",
    );
    value.dataset.state = pending ? "pending" : check.passed ? "pass" : "fail";
    node("checks").append(value);
  });
  node("hash-short").textContent = state.business_hash.slice(0, 16);
  node("business-hash").textContent = state.business_hash;
  const paint = JSON.stringify([
    job,
    state.months,
    state.issues,
    state.file_errors,
    state.jobs,
  ]);
  if (paint !== lastPaint) {
    lastPaint = paint;
    tableRows("steps", job?.steps || [], [
      (r) => r.filename,
      (r) => tag(r.status),
      (r) => r.evidence?.inserted,
      (r) => r.evidence?.updated,
      (r) =>
        r.evidence?.recovery
          ? "Recuperación"
          : r.evidence?.idempotent === true
            ? "Verificado"
            : r.evidence?.idempotent === false
              ? "Cambió"
              : "—",
      (r) => detailButton(r, `Entrega · ${r.filename}`),
    ]);
    tableRows("lab-months", state.months, [
      (r) => r.mes.slice(0, 7),
      (r) => money(r.revenue_eur),
      (r) => number(r.reporting_firms),
      (r) => number(r.missing_firms),
    ]);
    tableRows("history", state.jobs, [
      (r) => new Date(r.created_at).toLocaleString("es-ES"),
      (r) => tag(r.status),
      (r) => `${r.cursor}/${r.filenames.length}`,
      (r) => r.filenames.join(" → "),
      (r) => {
        const button = element("button", "Ver", "detail-button");
        button.onclick = async () => {
          try {
            details(
              await api(`/api/lab/jobs/${r.id}`),
              "Historial de reproducción",
            );
          } catch (e) {
            errorMessage(e);
          }
        };
        return button;
      },
    ]);
    node("issues").replaceChildren();
    const issues = [
      ...(state.file_errors || []).map((f) => ({
        ...f,
        code: "archivo_en_cuarentena",
        severity: "error",
        message: f.reason,
      })),
      ...state.issues,
    ];
    if (!issues.length)
      node("issues").append(
        element("p", "Sin incidencias registradas.", "empty"),
      );
    issues.forEach((issue) => {
      const row = element("article", null, "issue");
      row.dataset.severity = issue.severity;
      row.append(
        element("code", `${issue.severity} / ${issue.code}`),
        element("p", issue.message),
        element(
          "small",
          `${issue.filename}${issue.line_number ? ` · fila ${issue.line_number}` : " · archivo completo"}`,
        ),
      );
      node("issues").append(row);
    });
  }
  errorMessage(
    state.worker_error
      ? new Error(`El worker se ha detenido: ${state.worker_error}`)
      : null,
    "worker",
  );
  controls();
}
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    state = await api("/api/lab/state");
    connected = true;
    errorMessage(null, "connection");
    if (
      pendingPlay &&
      state.jobs.some((j) => j.id === pendingPlay.body.request_id)
    )
      pendingPlay = null;
    node("connection-status").textContent = state.worker_alive
      ? "Conectado · actualización en vivo"
      : "Worker detenido";
    render();
  } catch (e) {
    connected = false;
    clearTopline("Sin conexión. Métricas no disponibles.");
    controls();
    node("connection-status").textContent = "Sin conexión";
    errorMessage(e, "connection");
  } finally {
    polling = false;
  }
}
async function act(path, body = {}) {
  busy = true;
  controls();
  errorMessage(null, "action");
  try {
    const result = await api(path, body);
    await refresh();
    return result;
  } catch (e) {
    errorMessage(e);
    throw e;
  } finally {
    busy = false;
    controls();
  }
}
async function play(names) {
  const interval = Number(node("interval").value);
  const signature = JSON.stringify([names, interval]);
  if (!pendingPlay || pendingPlay.signature !== signature)
    pendingPlay = {
      signature,
      body: {
        request_id: crypto.randomUUID(),
        filenames: [...names],
        interval_ms: interval,
      },
    };
  try {
    await act("/api/lab/play", pendingPlay.body);
    pendingPlay = null;
  } catch (e) {
    if (e.httpStatus) pendingPlay = null;
  }
}
node("playhead").oninput = cursor;
node("topline-month").onchange = () => {
  if (connected && state) renderTopline();
};
node("select-until").onclick = () =>
  setSelection(
    state.catalog
      .slice(0, Number(node("playhead").value))
      .map((f) => f.filename),
  );
node("select-all").onclick = () =>
  setSelection(state.catalog.map((f) => f.filename));
node("select-none").onclick = () => setSelection([]);
node("reverse").onclick = () => setSelection([...selection].reverse());
node("play").onclick = () => play(selection);
node("step").onclick = () => {
  const file = selectedFile();
  if (file) play([file.filename]);
};
node("pause").onclick = () =>
  act(
    `/api/lab/jobs/${state.job.id}/${["paused", "failed"].includes(state.job.status) ? "resume" : "pause"}`,
  ).catch(() => {});
node("cancel").onclick = () =>
  act(`/api/lab/jobs/${state.job.id}/cancel`).catch(() => {});
node("close-evidence").onclick = () => node("evidence-dialog").close();
node("reset-open").onclick = () => node("reset-dialog").showModal();
node("reset-cancel").onclick = () => node("reset-dialog").close();
node("reset-confirm").onclick = async () => {
  node("reset-confirm").disabled = true;
  try {
    await act("/api/lab/reset", { confirmation: "RESET LAB" });
    pendingPlay = null;
    node("reset-dialog").close();
  } catch {
  } finally {
    node("reset-confirm").disabled = false;
  }
};
window.addEventListener("pagehide", () => clearInterval(pollTimer));
refresh();
pollTimer = setInterval(refresh, 1000);
