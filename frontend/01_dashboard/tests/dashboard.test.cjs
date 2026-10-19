const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM, VirtualConsole } = require("jsdom");
const base = process.env.PULSE_TEST_URL || "http://127.0.0.1:8000";
const html = fs.readFileSync("01_dashboard/public/index.html", "utf8");
const script = fs.readFileSync("01_dashboard/public/app.js", "utf8");

async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 50));
  }
  throw new Error("DOM condition timed out");
}

test("Dashboard renders live metrics, changes firm/month, exposes lineage and quality", async () => {
  const errors = [];
  const console = new VirtualConsole();
  console.on("jsdomError", (e) => errors.push(e.message));
  const dom = new JSDOM(html, {
    url: base,
    runScripts: "outside-only",
    virtualConsole: console,
  });
  const w = dom.window;
  const d = w.document;
  w.fetch = (url) => fetch(new URL(url, base));
  w.HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  w.HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  try {
    w.eval(script);
    await until(
      () =>
        d.querySelectorAll("#metrics tbody tr").length === 36 &&
        d.querySelectorAll("#firm option").length === 501,
    );
    assert.equal(d.getElementById("error").hidden, true);
    assert.equal(d.getElementById("rejected").textContent, "28");
    assert.equal(d.querySelectorAll("#chart rect").length, 36);
    assert.match(d.getElementById("quality-summary").textContent, /17\.988/);
    d.getElementById("firm").value = "218";
    d.getElementById("start").value = "2023-04";
    d.getElementById("end").value = "2023-04";
    d.getElementById("filters").dispatchEvent(
      new w.Event("submit", { cancelable: true }),
    );
    await until(
      () =>
        d.querySelectorAll("#metrics tbody tr").length === 1 &&
        d.querySelector("#metrics button"),
    );
    d.querySelector("#metrics button").click();
    await until(() => d.getElementById("details").hasAttribute("open"));
    assert.match(
      d.getElementById("detail-content").textContent,
      /metricas_2023-05.csv/,
    );
    d.getElementById("close-dialog").click();
    assert.equal(d.getElementById("details").hasAttribute("open"), false);
    d.getElementById("quality-view").value = "gaps";
    d.getElementById("quality-view").dispatchEvent(new w.Event("change"));
    await until(
      () => d.getElementById("page-info").textContent === "1–12 de 12",
    );
    assert.equal(d.querySelectorAll("#quality tbody tr").length, 12);
    assert.deepEqual(errors, []);
  } finally {
    w.close();
  }
});

test("API failure is visibly reported instead of showing invented metrics", async () => {
  const dom = new JSDOM(html, { url: base, runScripts: "outside-only" });
  try {
    dom.window.fetch = async () => {
      throw new Error("Connection unavailable");
    };
    dom.window.eval(script);
    await until(() => !dom.window.document.getElementById("error").hidden);
    assert.match(
      dom.window.document.getElementById("error").textContent,
      /Connection unavailable/,
    );
    assert.equal(
      dom.window.document.getElementById("revenue").textContent,
      "—",
    );
  } finally {
    dom.window.close();
  }
});

const ok = (body) => ({ ok: true, json: async () => body });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
const firms = [
  { id: 1, nombre: "Primera" },
  { id: 2, nombre: "Segunda" },
];
const networkRow = {
  mes: "2023-09-01",
  revenue_eur: "100.25",
  clientes_activos: 10,
  clientes_nuevos: 1,
  clientes_baja: 0,
  horas_trabajadas: "545238.00",
  hours_reporting_firms: 498,
  satisfaction_firm_average: "4.06",
  reporting_firms: 500,
  expected_firms: 500,
  missing_firms: 0,
  coverage_pct: "100.00",
};
function issuePage(offset = 0) {
  return {
    items: Array.from({ length: 20 }, (_, i) => ({
      filename: `row-${offset + i + 1}.csv`,
      code: "missing_value",
      message: "Missing hours",
    })),
    total: 60,
    limit: 20,
    offset,
  };
}
async function dashboard(t, handler = () => undefined) {
  const dom = new JSDOM(html, { url: base, runScripts: "outside-only" });
  t.after(() => dom.window.close());
  const w = dom.window,
    d = w.document;
  let respond = handler;
  w.fetch = (path) => {
    const custom = respond(path);
    if (custom !== undefined) return Promise.resolve(custom);
    if (path.startsWith("/api/v1/firms?"))
      return Promise.resolve(ok({ items: firms }));
    if (path.includes("/metrics?"))
      return Promise.resolve(ok({ items: [networkRow], total: 1 }));
    if (path.endsWith("/summary"))
      return Promise.resolve(
        ok({
          rejected_rows: 0,
          warning_rows: 2,
          file_errors: 0,
          raw_rows: 500,
          accepted_rows: 500,
          curated_rows: 500,
          duplicate_rows: 0,
          missing_firm_months: 0,
          fallback_rows: 0,
        }),
      );
    return Promise.resolve(ok(issuePage()));
  };
  w.eval(script);
  await until(() => d.getElementById("loading").textContent === "1 meses");
  return {
    w,
    d,
    get: (id) => d.getElementById(id),
    respond: (next) => {
      respond = next;
    },
    submit: () =>
      d
        .getElementById("filters")
        .dispatchEvent(new w.Event("submit", { cancelable: true })),
  };
}

test("Quality selection ignores both stale successes and stale failures", async (t) => {
  for (const outcome of ["success", "failure"]) {
    await t.test(outcome, async (t) => {
      const ui = await dashboard(t);
      const old = deferred(),
        latest = deferred();
      ui.respond((path) =>
        path.includes("severity=warning")
          ? old.promise
          : path.includes("/gaps?")
            ? latest.promise
            : undefined,
      );
      ui.get("quality-view").value = "issues?severity=warning";
      ui.get("quality-view").dispatchEvent(new ui.w.Event("change"));
      ui.get("quality-view").value = "gaps";
      ui.get("quality-view").dispatchEvent(new ui.w.Event("change"));
      assert.doesNotMatch(ui.get("quality").textContent, /row-1.csv/);
      latest.resolve(
        ok({
          items: [
            {
              mes: "2023-09-01",
              asesoria_id: 409,
              nombre: "Missing firm",
              status: "missing",
            },
          ],
          total: 1,
        }),
      );
      await until(() => ui.get("page-info").textContent === "1–1 de 1");
      if (outcome === "success") old.resolve(ok(issuePage()));
      else old.reject(new Error("Obsolete warning request failed"));
      await settle();
      assert.equal(ui.get("quality-view").value, "gaps");
      assert.match(ui.get("quality").textContent, /Missing firm/);
      assert.doesNotMatch(ui.get("quality").textContent, /row-1.csv/);
      assert.equal(ui.get("page-info").textContent, "1–1 de 1");
      assert.equal(ui.get("error").hidden, true);
      assert.equal(ui.get("next").disabled, true);
    });
  }
});

test("Quality pagination keeps the newest rows and their captured offset", async (t) => {
  const ui = await dashboard(t);
  const pages = new Map([
    [20, deferred()],
    [40, deferred()],
  ]);
  const offsets = [];
  ui.respond((path) => {
    if (!path.includes("/quality/issues?")) return;
    const offset = Number(new URL(path, base).searchParams.get("offset"));
    offsets.push(offset);
    return pages.get(offset).promise;
  });
  ui.get("next").click();
  ui.get("next").click();
  assert.deepEqual(offsets, [20, 40]);
  pages.get(40).resolve(ok(issuePage(40)));
  await until(() => ui.get("page-info").textContent === "41–60 de 60");
  pages.get(20).resolve(ok(issuePage(20)));
  await settle();
  assert.match(ui.get("quality").textContent, /row-41.csv/);
  assert.doesNotMatch(ui.get("quality").textContent, /row-21.csv/);
  assert.equal(ui.get("page-info").textContent, "41–60 de 60");
  assert.equal(ui.get("next").disabled, true);
  assert.equal(ui.get("previous").disabled, false);
});

test("Quality issues keep malformed-row identity visible without changing archived evidence", async (t) => {
  const malformed = {
    filename: "metricas_2025-10.csv",
    line_number: 7,
    code: "column_count",
    field: null,
    message: "Unexpected number of fields",
    payload: { asesoria_id: "1", mes: "2025-10", extra: ["unexpected"] },
    normalized: {},
    identified_firm_id: "1",
    identified_month: "2025-10-01",
  };
  const items = [
    malformed,
    {
      ...malformed,
      normalized: { asesoria_id: 2, mes: "2025-11-01" },
    },
    {
      ...malformed,
      normalized: { asesoria_id: 3 },
      identified_month: "2025-12-01",
    },
    {
      ...malformed,
      normalized: null,
      identified_firm_id: "4",
    },
    {
      ...malformed,
      identified_firm_id: null,
      identified_month: null,
    },
  ];
  const ui = await dashboard(t, (path) =>
    path.includes("/quality/issues?")
      ? ok({ items, total: items.length })
      : undefined,
  );
  ui.w.HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  const headings = [...ui.d.querySelectorAll("#quality th")].map(
    (n) => n.textContent,
  );
  const firmColumn = headings.indexOf("Asesoría");
  const monthColumn = headings.indexOf("Mes");
  assert.ok(
    firmColumn >= 0,
    "Firm identity must be visible in the issue table",
  );
  assert.ok(
    monthColumn >= 0,
    "Metric month must be visible in the issue table",
  );
  const rows = [...ui.d.querySelectorAll("#quality tbody tr")];
  assert.deepEqual(
    rows.map((tr) => [
      tr.cells[firmColumn].textContent,
      tr.cells[monthColumn].textContent,
    ]),
    [
      ["1", "2025-10"],
      ["2", "2025-11"],
      ["3", "2025-12"],
      ["4", "2025-10"],
      ["—", "—"],
    ],
  );
  for (const [heading, value] of [
    ["Archivo", malformed.filename],
    ["Fila", "7"],
    ["Regla", malformed.code],
    ["Campo", "—"],
    ["Motivo", malformed.message],
  ])
    assert.equal(rows[0].cells[headings.indexOf(heading)].textContent, value);
  rows[0].querySelector("button").click();
  assert.equal(ui.get("details").hasAttribute("open"), true);
  assert.deepEqual(JSON.parse(ui.get("detail-content").textContent), malformed);
  assert.deepEqual(
    malformed.normalized,
    {},
    "Identified fields must not rewrite normalized history",
  );
  assert.equal(ui.get("error").hidden, true);
});

test("Hours disclose partial, complete and absent contributors without turning NULL into zero", async (t) => {
  const rows = [
    networkRow,
    {
      ...networkRow,
      mes: "2023-10-01",
      horas_trabajadas: "0.00",
      hours_reporting_firms: 500,
    },
    {
      ...networkRow,
      mes: "2023-11-01",
      horas_trabajadas: null,
      hours_reporting_firms: 0,
    },
  ];
  const ui = await dashboard(t);
  ui.respond((path) =>
    path.includes("/metrics?") ? ok({ items: rows, total: 3 }) : undefined,
  );
  ui.submit();
  await until(() => ui.get("loading").textContent === "3 meses");
  const headings = [...ui.d.querySelectorAll("#metrics th")].map(
    (n) => n.textContent,
  );
  const hours = headings.indexOf("Horas"),
    coverage = headings.indexOf("Cobertura de horas");
  assert.ok(coverage >= 0);
  const cells = [...ui.d.querySelectorAll("#metrics tbody tr")].map((tr) =>
    [...tr.cells].map((td) => td.textContent),
  );
  assert.equal(cells[0][hours], "545.238");
  assert.equal(cells[0][coverage], "498/500 asesorías · Parcial");
  assert.equal(cells[1][hours], "0");
  assert.equal(cells[1][coverage], "500/500 asesorías");
  assert.equal(cells[2][hours], "—");
  assert.equal(cells[2][coverage], "0/500 asesorías · Parcial");
  assert.equal(ui.get("coverage").textContent, "100 %");
  // A single firm has no network contributor count; its missing value remains explicit.
  ui.respond((path) =>
    path.includes("/metrics?")
      ? ok({
          items: [
            {
              ...networkRow,
              coverage_status: "available",
              horas_trabajadas: null,
              satisfaccion_cliente: "4.06",
            },
          ],
          total: 1,
        })
      : undefined,
  );
  ui.get("firm").value = "1";
  ui.submit();
  await until(() => ui.get("loading").textContent === "1 meses");
  assert.doesNotMatch(
    ui.get("metrics").querySelector("thead").textContent,
    /Cobertura de horas/,
  );
  assert.equal(
    ui.d.querySelector("#metrics tbody tr").cells[hours].textContent,
    "—",
  );
});

test("Failed firm filters clear old cards, chart and rows, then recover", async (t) => {
  const ui = await dashboard(t);
  const pending = deferred();
  ui.respond((path) =>
    path.includes("/firms/1/metrics?") ? pending.promise : undefined,
  );
  assert.equal(ui.d.querySelectorAll("#chart rect").length, 1);
  ui.get("firm").value = "1";
  ui.submit();
  assert.equal(ui.get("revenue").textContent, "—");
  assert.equal(ui.d.querySelectorAll("#chart rect").length, 0);
  assert.doesNotMatch(ui.get("metrics").textContent, /545.238/);
  pending.resolve({
    ok: false,
    status: 503,
    text: async () => "Metrics unavailable",
  });
  await until(() => ui.get("loading").textContent === "Error de carga");
  for (const id of ["revenue", "clients", "coverage"])
    assert.equal(ui.get(id).textContent, "—");
  assert.equal(ui.get("coverage-card").dataset.state, "unknown");
  assert.match(ui.get("metrics").textContent, /No se pudieron cargar/);
  assert.equal(ui.d.querySelectorAll("#metrics button").length, 0);
  assert.match(ui.get("error").textContent, /503/);
  ui.respond((path) =>
    path.includes("/metrics?")
      ? ok({
          items: [
            {
              ...networkRow,
              revenue_eur: "12.34",
              coverage_status: "available",
            },
          ],
          total: 1,
        })
      : undefined,
  );
  ui.submit();
  await until(() => ui.get("revenue").textContent.includes("12,34"));
  assert.equal(ui.get("error").hidden, true);
  assert.equal(ui.d.querySelectorAll("#chart rect").length, 1);
});

test("An obsolete metric failure cannot clear a newer successful selection", async (t) => {
  const ui = await dashboard(t);
  const old = deferred();
  ui.respond((path) =>
    path.includes("/firms/1/metrics?") ? old.promise : undefined,
  );
  ui.get("firm").value = "1";
  ui.submit();
  ui.get("firm").value = "";
  ui.submit();
  await until(() => ui.get("loading").textContent === "1 meses");
  old.reject(new Error("Obsolete metrics failed"));
  await settle();
  assert.match(ui.get("revenue").textContent, /100,25/);
  assert.equal(ui.get("error").hidden, true);
  assert.equal(ui.d.querySelectorAll("#chart rect").length, 1);
});

test("Refresh recovers the firm list and preserves valid selections made during refresh", async (t) => {
  let firmCalls = 0;
  const ui = await dashboard(t, (path) => {
    if (path.startsWith("/api/v1/firms?")) {
      firmCalls++;
      return Promise.reject(new Error("Firm list unavailable"));
    }
  });
  assert.equal(ui.get("firm").options.length, 1);
  assert.match(ui.get("error").textContent, /Firm list unavailable/);
  ui.submit();
  await until(() => ui.get("loading").textContent === "1 meses");
  assert.equal(
    ui.get("error").hidden,
    false,
    "Metrics success must not hide the firm-list failure",
  );
  ui.respond((path) => {
    if (path.startsWith("/api/v1/firms?")) {
      firmCalls++;
      return ok({ items: firms });
    }
  });
  ui.get("refresh").click();
  await until(() => ui.get("firm").options.length === 3);
  assert.equal(firmCalls, 2);
  assert.equal(ui.get("error").hidden, true);
  ui.get("firm").value = "1";
  const pending = deferred();
  ui.respond((path) =>
    path.startsWith("/api/v1/firms?") ? pending.promise : undefined,
  );
  ui.get("refresh").click();
  ui.get("firm").value = "2";
  pending.resolve(ok({ items: [...firms, { id: 3, nombre: "Tercera" }] }));
  await until(() => ui.get("firm").options.length === 4);
  assert.equal(ui.get("firm").value, "2");
  ui.respond(() => undefined);
  ui.get("refresh").click();
  await until(() => ui.get("firm").options.length === 3);
  assert.equal(ui.get("firm").value, "2");
  ui.respond((path) =>
    path.startsWith("/api/v1/firms?")
      ? Promise.reject(new Error("List refresh failed"))
      : undefined,
  );
  ui.get("refresh").click();
  await until(() =>
    ui.get("error").textContent.includes("List refresh failed"),
  );
  assert.equal(ui.get("firm").value, "2");
  assert.equal(ui.get("firm").options.length, 3);
});
