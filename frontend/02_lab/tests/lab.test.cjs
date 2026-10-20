const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { randomUUID } = require("node:crypto");
const { JSDOM } = require("jsdom");
const html = fs.readFileSync("02_lab/public/lab.html", "utf8");
const script = fs.readFileSync("02_lab/public/lab.js", "utf8");

async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw new Error("Lab UI did not reach the expected state");
}
function initial() {
  return {
    worker_alive: true,
    worker_error: null,
    business_hash: "abcd".repeat(16),
    counts: {
      files: 0,
      raw_rows: 0,
      accepted_rows: 0,
      rejected_rows: 0,
      duplicate_rows: 0,
      published_rows: 0,
      warning_rows: 0,
      gaps: 0,
      file_errors: 0,
    },
    catalog: ["2023-04", "2023-05", "2023-06"].map((month) => ({
      filename: `metricas_${month}.csv`,
      month: `${month}-01`,
      rows: 500,
      format: "v1",
      status: "pending",
    })),
    job: null,
    jobs: [],
    months: [],
    issues: [],
    checks: [{ label: "Una fila por asesoría y mes", passed: true }],
  };
}

function month(mes, revenue, clients, missing = 0) {
  return {
    mes: `${mes}-01`,
    revenue_eur: revenue,
    clientes_activos: clients,
    reporting_firms: 2 - missing,
    expected_firms: 2,
    missing_firms: missing,
    coverage_pct: String((2 - missing) * 50),
  };
}

async function toplineHarness(t, months = []) {
  const state = initial();
  state.months = months;
  const dom = new JSDOM(html, {
    url: "http://localhost:8001",
    runScripts: "outside-only",
  });
  t.after(() => dom.window.close());
  let poll;
  let offline = false;
  dom.window.setInterval = (callback) => {
    poll = callback;
    return 1;
  };
  dom.window.fetch = async (path, options = {}) => {
    assert.equal(
      path,
      "/api/lab/state",
      "Topline uses the lab snapshot, never the main database",
    );
    assert.equal(
      options.method,
      undefined,
      "Selecting a month never starts ingestion",
    );
    if (offline) throw new Error("Network interrupted");
    return { ok: true, json: async () => structuredClone(state) };
  };
  dom.window.eval(script);
  await until(() =>
    dom.window.document
      .getElementById("connection-status")
      .textContent.includes("Conectado"),
  );
  return {
    state,
    d: dom.window.document,
    poll: () => poll(),
    offline: (value) => {
      offline = value;
    },
    select: (value) => {
      const select = dom.window.document.getElementById("topline-month");
      select.value = value;
      select.dispatchEvent(new dom.window.Event("change"));
    },
    money: (value) =>
      new dom.window.Intl.NumberFormat("es-ES", {
        style: "currency",
        currency: "EUR",
      }).format(Number(value)),
  };
}

test("Topline follows the latest metric month and preserves a historical selection across playback", async (t) => {
  const h = await toplineHarness(t, [
    month("2024-01", "100.00", 10),
    month("2024-02", "200.00", 20, 1),
  ]);
  const value = (id) => h.d.getElementById(id).textContent;
  assert.equal(value("topline-revenue"), h.money("200.00"));
  assert.equal(value("topline-clients"), "20");
  assert.equal(value("topline-coverage"), "50 %");
  assert.equal(h.d.querySelectorAll("#lab-revenue-chart .chart-bar").length, 2);
  assert.equal(
    h.d.querySelectorAll("#lab-revenue-chart .is-partial").length,
    1,
  );
  h.select("2024-01-01");
  assert.equal(value("topline-revenue"), h.money("100.00"));
  h.state.months.push(month("2024-03", "300.00", 30));
  await h.poll();
  assert.equal(value("topline-revenue"), h.money("100.00"));
  assert.equal(h.d.getElementById("topline-month").value, "2024-01-01");
  assert.equal(h.d.querySelectorAll("#lab-revenue-chart .chart-bar").length, 3);
  h.select("");
  assert.equal(value("topline-revenue"), h.money("300.00"));
  // An older corrected month does not become the latest, but its chart value updates.
  h.state.months[0].revenue_eur = "120.00";
  await h.poll();
  assert.equal(value("topline-revenue"), h.money("300.00"));
  assert.match(
    h.d.querySelector("#lab-revenue-chart title").textContent,
    /120/,
  );
  h.select("2024-01-01");
  assert.equal(value("topline-revenue"), h.money("120.00"));
  const beforeReplay = h.d.getElementById("lab-revenue-chart").innerHTML;
  await h.poll();
  assert.equal(value("topline-revenue"), h.money("120.00"));
  assert.equal(h.d.getElementById("lab-revenue-chart").innerHTML, beforeReplay);
});

test("Topline keeps missing months distinct from valid zero revenue and clears on reset", async (t) => {
  const h = await toplineHarness(t, [
    month("2024-01", "0.00", 0),
    month("2024-02", null, null, 2),
  ]);
  assert.equal(h.d.getElementById("topline-revenue").textContent, "—");
  assert.equal(h.d.getElementById("topline-clients").textContent, "—");
  assert.equal(h.d.getElementById("topline-coverage").textContent, "0 %");
  assert.equal(h.d.querySelectorAll("#lab-revenue-chart .chart-bar").length, 1);
  h.select("2024-01-01");
  assert.equal(
    h.d.getElementById("topline-revenue").textContent,
    h.money("0.00"),
  );
  assert.equal(h.d.getElementById("topline-clients").textContent, "0");
  h.state.months = [];
  await h.poll();
  assert.equal(h.d.getElementById("topline-month").disabled, true);
  assert.equal(h.d.getElementById("topline-month").value, "");
  assert.equal(h.d.getElementById("topline-revenue").textContent, "—");
  assert.equal(h.d.querySelectorAll("#lab-revenue-chart .chart-bar").length, 0);
  assert.match(
    h.d.getElementById("topline-range").textContent,
    /Sin meses publicados/,
  );
  assert.equal(h.d.getElementById("reject-count").textContent, "0");
});

test("A disconnected topline hides stale totals and restores the selected month on reconnect", async (t) => {
  const h = await toplineHarness(t, [month("2024-01", "100.00", 10)]);
  h.select("2024-01-01");
  h.offline(true);
  await h.poll();
  for (const id of [
    "topline-revenue",
    "topline-clients",
    "topline-coverage",
    "reject-count",
  ])
    assert.equal(h.d.getElementById(id).textContent, "—");
  assert.equal(h.d.querySelectorAll("#lab-revenue-chart .chart-bar").length, 0);
  assert.equal(h.d.getElementById("topline-month").disabled, true);
  h.offline(false);
  await h.poll();
  assert.equal(
    h.d.getElementById("topline-revenue").textContent,
    h.money("100.00"),
  );
  assert.equal(h.d.getElementById("topline-month").value, "2024-01-01");
  assert.equal(h.d.getElementById("topline-month").disabled, false);
  assert.equal(h.d.querySelectorAll("#lab-revenue-chart .chart-bar").length, 1);
});

test("Lab scrubbing is read-only, custom ordering is sent exactly, and pause/resume stay explicit", async () => {
  const state = initial();
  const calls = [];
  const dom = new JSDOM(html, {
    url: "http://localhost:8001",
    runScripts: "outside-only",
  });
  const w = dom.window,
    d = w.document;
  w.crypto.randomUUID = randomUUID;
  w.HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  w.HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  w.fetch = async (path, options = {}) => {
    if (options.method === "POST") {
      const body = JSON.parse(options.body);
      calls.push({ path, body, headers: options.headers });
      if (path === "/api/lab/play") {
        state.job = {
          id: body.request_id,
          filenames: body.filenames,
          status: "running",
          cursor: 0,
          created_at: new Date().toISOString(),
          steps: [],
        };
        state.jobs = [state.job];
      } else if (path.endsWith("/pause")) state.job.status = "paused";
      else if (path.endsWith("/resume")) state.job.status = "running";
      else if (path.endsWith("/cancel")) state.job.status = "cancelled";
    }
    return {
      ok: true,
      json: async () =>
        JSON.parse(
          JSON.stringify(path === "/api/lab/state" ? state : state.job),
        ),
    };
  };
  try {
    w.eval(script);
    await until(() => d.querySelectorAll(".catalog-row").length === 3);
    d.getElementById("playhead").value = "2";
    d.getElementById("playhead").dispatchEvent(new w.Event("input"));
    assert.match(d.getElementById("cursor-label").textContent, /2023-05/);
    assert.equal(
      calls.length,
      0,
      "Moving the playhead must never ingest or delete data",
    );
    d.getElementById("select-until").click();
    d.getElementById("reverse").click();
    d.getElementById("play").click();
    await until(
      () =>
        calls.length === 1 &&
        d.getElementById("job-status").textContent === "En curso",
    );
    assert.deepEqual(calls[0].body.filenames, [
      "metricas_2023-05.csv",
      "metricas_2023-04.csv",
    ]);
    assert.equal(calls[0].headers["X-Pulse-Lab"], "playback-v1");
    assert.equal(d.getElementById("play").disabled, true);
    assert.equal(d.getElementById("reset-open").disabled, true);
    d.getElementById("pause").click();
    await until(() => d.getElementById("pause").textContent === "Continuar");
    assert.equal(d.getElementById("play").disabled, true);
    d.getElementById("pause").click();
    await until(() => calls.some((c) => c.path.endsWith("/resume")));
    assert.equal(calls.filter((c) => c.path === "/api/lab/play").length, 1);
  } finally {
    w.close();
  }
});

test("A disconnected lab shows an error and does not enable playback", async () => {
  const dom = new JSDOM(html, {
    url: "http://localhost:8001",
    runScripts: "outside-only",
  });
  try {
    dom.window.fetch = async () => {
      throw new Error("Lab offline");
    };
    dom.window.eval(script);
    await until(() => !dom.window.document.getElementById("error").hidden);
    assert.match(
      dom.window.document.getElementById("error").textContent,
      /Lab offline/,
    );
    assert.equal(dom.window.document.getElementById("play").disabled, true);
    assert.equal(dom.window.document.getElementById("step").disabled, true);
  } finally {
    dom.window.close();
  }
});

test("Lab reconnect clears only the connectivity failure and preserves action and worker errors", async (t) => {
  const dom = new JSDOM(html, {
    url: "http://localhost:8001",
    runScripts: "outside-only",
  });
  t.after(() => dom.window.close());
  const w = dom.window,
    d = w.document;
  w.crypto.randomUUID = randomUUID;
  let poll,
    offline = true,
    actionCalls = 0;
  const state = initial();
  w.setInterval = (callback) => {
    poll = callback;
    return 1;
  };
  w.fetch = async (path, options = {}) => {
    if (options.method === "POST") {
      actionCalls++;
      assert.equal(path, "/api/lab/play");
      return {
        ok: false,
        status: 409,
        json: async () => ({ detail: "Playback request rejected" }),
      };
    }
    assert.equal(path, "/api/lab/state");
    if (offline) throw new Error("Lab connection interrupted");
    return { ok: true, json: async () => structuredClone(state) };
  };
  w.eval(script);
  await until(
    () => d.getElementById("connection-status").textContent === "Sin conexión",
  );
  assert.match(
    d.getElementById("error").textContent,
    /Lab connection interrupted/,
  );
  assert.equal(d.getElementById("play").disabled, true);
  offline = false;
  await poll();
  assert.equal(d.getElementById("error").hidden, true);
  assert.equal(d.getElementById("error").textContent, "");
  assert.equal(d.getElementById("play").disabled, false);
  d.getElementById("play").click();
  await until(() =>
    d.getElementById("error").textContent.includes("Playback request rejected"),
  );
  await poll();
  assert.equal(d.getElementById("error").hidden, false);
  assert.match(
    d.getElementById("error").textContent,
    /Playback request rejected/,
  );
  offline = true;
  await poll();
  assert.match(
    d.getElementById("error").textContent,
    /Lab connection interrupted/,
  );
  assert.match(
    d.getElementById("error").textContent,
    /Playback request rejected/,
  );
  offline = false;
  state.worker_alive = false;
  state.worker_error = "Worker stopped unexpectedly";
  await poll();
  assert.doesNotMatch(
    d.getElementById("error").textContent,
    /Lab connection interrupted/,
  );
  assert.match(
    d.getElementById("error").textContent,
    /Playback request rejected/,
  );
  assert.match(
    d.getElementById("error").textContent,
    /Worker stopped unexpectedly/,
  );
  assert.equal(
    d.getElementById("connection-status").textContent,
    "Worker detenido",
  );
  assert.equal(d.getElementById("play").disabled, true);
  await poll();
  assert.match(
    d.getElementById("error").textContent,
    /Worker stopped unexpectedly/,
  );
  assert.equal(
    actionCalls,
    1,
    "Reconnection must not retry the rejected action",
  );
});
