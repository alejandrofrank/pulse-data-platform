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
