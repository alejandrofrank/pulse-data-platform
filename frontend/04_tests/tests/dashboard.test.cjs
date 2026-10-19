const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM, VirtualConsole } = require("jsdom");

const publicDir = path.resolve(__dirname, "../public");
const html = fs.readFileSync(path.join(publicDir, "index.html"), "utf8");
const script = fs.readFileSync(path.join(publicDir, "app.js"), "utf8");
const pipeline = "Pipeline & quality (Phase 1)";
const agents = "Shared agent runtime (Phase 2)";
const started = "2026-10-20T10:00:00Z";
const ended = "2026-10-20T10:01:00Z";
const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const flush = async () => {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
};
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function example(name, status = "passed", area = pipeline, message = "") {
  return {
    name,
    module: "backend/tests/test_pipeline.py",
    area,
    status,
    duration_seconds: 0.125,
    message,
  };
}
function suite(id, cases, extra = {}) {
  return {
    id,
    label: id === "backend" ? "Backend" : "Frontend",
    status: cases.some((item) => ["failed", "error"].includes(item.status))
      ? "failed"
      : "passed",
    started_at: started,
    finished_at: ended,
    duration_seconds: 60,
    exit_code: 0,
    error: null,
    counts: {
      passed: cases.filter((item) => item.status === "passed").length,
      failed: cases.filter((item) => item.status === "failed").length,
      skipped: cases.filter((item) => item.status === "skipped").length,
      errors: cases.filter((item) => item.status === "error").length,
      total: cases.length,
    },
    cases,
    ...extra,
  };
}
function report(extra = {}) {
  return {
    schema_version: 1,
    run_id: "local-run-001",
    status: "failed",
    started_at: started,
    finished_at: ended,
    commit: "abcdef1234567890",
    dirty: true,
    source: "local",
    ci_url: null,
    suites: [
      suite(
        "backend",
        [
          example("pipeline / accepts rows"),
          example("pipeline / rejects duplicates"),
          example(
            "pipeline / reports quality",
            "failed",
            pipeline,
            "Expected 3 rows\nReceived 2 rows",
          ),
          example(
            "agent / paid evaluation",
            "skipped",
            agents,
            "Requires external credentials",
          ),
          example("agent / fixture", "error", agents, "Fixture unavailable"),
        ],
        { exit_code: 1 },
      ),
      suite("frontend", [
        {
          ...example(
            "request generations > fetch > resolves",
            "passed",
            agents,
          ),
          module: "03_agents/tests/workbench.test.cjs",
        },
        {
          ...example(
            "request generations > json > resolves",
            "passed",
            "Test tooling",
          ),
          module: "04_tests/tests/dashboard.test.cjs",
        },
      ]),
    ],
    ...extra,
  };
}
function setup(t, initial = () => ok(report())) {
  const errors = [];
  const console = new VirtualConsole();
  console.on("jsdomError", (error) => errors.push(error.message));
  const dom = new JSDOM(html, {
    url: "http://localhost/tests/",
    runScripts: "outside-only",
    virtualConsole: console,
  });
  const w = dom.window,
    d = w.document;
  const intervals = new Map(),
    timeouts = new Map(),
    calls = [];
  let handler = initial,
    timerID = 0;
  w.setInterval = (fn, delay) => {
    const id = ++timerID;
    intervals.set(id, { fn, delay });
    return id;
  };
  w.clearInterval = (id) => intervals.delete(id);
  w.setTimeout = (fn, delay) => {
    const id = ++timerID;
    timeouts.set(id, { fn, delay });
    return id;
  };
  w.clearTimeout = (id) => timeouts.delete(id);
  w.fetch = (url, options) => {
    calls.push({ url, options });
    return Promise.resolve().then(() => handler(url, options));
  };
  t.after(() => {
    w.close();
    assert.deepEqual(errors, []);
  });
  w.eval(script);
  return {
    w,
    d,
    calls,
    intervals,
    timeouts,
    get: (id) => d.getElementById(id),
    respond: (next) => {
      handler = next;
    },
    refresh: async () => {
      d.getElementById("refresh").click();
      await flush();
    },
    change: (id, value) => {
      const control = d.getElementById(id);
      control.value = value;
      control.dispatchEvent(new w.Event(id === "search" ? "input" : "change"));
    },
    rows: () => [...d.querySelectorAll("#cases tr")],
  };
}
function readCounts(container) {
  return Object.fromEntries(
    [...container.querySelectorAll(".counts dl, :scope > dl")].map((item) => [
      item.querySelector("dt").textContent,
      item.querySelector("dd").textContent,
    ]),
  );
}

test("relative assets and read-only polling work at /tests/ without external requests", async (t) => {
  const ui = setup(t);
  await flush();
  assert.equal(ui.d.querySelector("script[src]").getAttribute("src"), "app.js");
  assert.equal(
    ui.d.querySelector("link[rel=stylesheet]").getAttribute("href"),
    "styles.css",
  );
  assert.deepEqual(
    [...ui.d.querySelectorAll("nav a")].map((a) => a.getAttribute("href")),
    ["/", "/agents", "https://github.com/alejandrofrank/pulse-data-platform/actions"],
  );
  assert.equal(
    ui.d.querySelector(".runner code").textContent,
    "python backend/03_scripts/test_dashboard.py",
  );
  assert.match(
    ui.get("case-panel").textContent.replace(/\s+/g, " "),
    /Counts are individual test cases; nested group headings are not counted twice\./,
  );
  assert.match(
    ui.d.querySelector(".runner").textContent.replace(/\s+/g, " "),
    /not a production coverage certificate/,
  );
  assert.match(
    ui.d.querySelector(".runner").textContent.replace(/\s+/g, " "),
    /External paid live evaluations are not rerun/,
  );
  assert.ok(
    ![...ui.d.querySelectorAll("button")].some((button) =>
      /run/i.test(button.textContent),
    ),
  );
  assert.equal(ui.intervals.size, 1);
  const timer = [...ui.intervals.values()][0];
  assert.equal(timer.delay, 5000);
  timer.fn();
  await flush();
  await ui.refresh();
  assert.equal(ui.calls.length, 3);
  for (const call of ui.calls) {
    assert.equal(call.url, "report.json");
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.method, undefined);
  }
});

test("counts, source revision, suite timings and full leaf case names are inspectable", async (t) => {
  const ui = setup(t);
  await flush();
  assert.deepEqual(readCounts(ui.get("totals")), {
    Passed: "4",
    Failed: "1",
    Skipped: "1",
    Errors: "1",
    Total: "7",
  });
  assert.deepEqual(readCounts(ui.get("suite-backend")), {
    Passed: "2",
    Failed: "1",
    Skipped: "1",
    Errors: "1",
    Total: "5",
  });
  assert.deepEqual(readCounts(ui.get("suite-frontend")), {
    Passed: "2",
    Failed: "0",
    Skipped: "0",
    Errors: "0",
    Total: "2",
  });
  assert.match(ui.get("run-metadata").textContent, /abcdef1234567890/);
  assert.match(
    ui.get("run-metadata").textContent,
    /Dirty · uncommitted changes/,
  );
  assert.match(ui.get("run-metadata").textContent, /Local/);
  assert.match(ui.get("suite-backend").textContent, /2026-10-20 10:00:00 UTC/);
  assert.match(ui.get("suite-backend").textContent, /2026-10-20 10:01:00 UTC/);
  assert.match(ui.get("suite-backend").textContent, /60.000 s/);
  assert.match(
    ui.get("cases").textContent,
    /request generations > fetch > resolves/,
  );
  assert.match(
    ui.get("cases").textContent,
    /request generations > json > resolves/,
  );
  assert.match(
    ui.get("cases").textContent,
    /03_agents\/tests\/workbench.test.cjs/,
  );
  assert.equal(ui.rows().length, 7);
  assert.equal(ui.rows()[0].lastElementChild.textContent, "0.125 s");
  assert.equal(
    ui.get("completion").textContent,
    "2/2 suites completed · totals from completed suites only",
  );
  assert.match(ui.get("area-counts").textContent, /2\/3 passed · 1 failed/);
  assert.equal(
    ui.d.querySelector("#cases details pre").textContent,
    "Expected 3 rows\nReceived 2 rows",
  );
});

test("pending and running suites never expose partial counts or per-case progress", async (t) => {
  const value = report({
    status: "running",
    finished_at: null,
    suites: [
      suite("backend", [example("not finalized")], {
        status: "running",
        finished_at: null,
        duration_seconds: null,
        exit_code: null,
      }),
      suite("frontend", [], {
        status: "pending",
        started_at: null,
        finished_at: null,
        duration_seconds: null,
        exit_code: null,
      }),
    ],
  });
  const ui = setup(t, () => ok(value));
  await flush();
  assert.equal(ui.get("run-status").textContent, "Running");
  assert.match(ui.get("suite-backend").textContent, /Running/);
  assert.match(ui.get("suite-frontend").textContent, /Pending/);
  assert.deepEqual(Object.values(readCounts(ui.get("suite-backend"))), [
    "—",
    "—",
    "—",
    "—",
    "—",
  ]);
  assert.equal(readCounts(ui.get("totals")).Total, "0");
  assert.equal(ui.rows().length, 0);
  assert.match(
    ui.get("table-message").textContent,
    /Waiting for a suite to finish/,
  );
  value.suites[0] = suite("backend", [example("completed backend case")]);
  await ui.refresh();
  assert.equal(readCounts(ui.get("totals")).Total, "1");
  assert.equal(ui.rows().length, 1);
  assert.match(ui.get("completion").textContent, /1\/2 suites completed/);
});

test("suite launch errors and interrupted runs remain visible with no fabricated cases", async (t) => {
  const ui = setup(t, () =>
    ok(
      report({
        status: "interrupted",
        suites: [
          suite("backend", [], {
            status: "error",
            error: "pytest could not start\nExecutable missing",
            exit_code: 127,
          }),
          suite("frontend", [], { status: "pending", finished_at: null }),
        ],
      }),
    ),
  );
  await flush();
  assert.equal(ui.get("run-status").dataset.state, "interrupted");
  assert.equal(
    ui.d.querySelector(".suite-error").textContent,
    "pytest could not start\nExecutable missing",
  );
  assert.match(ui.get("suite-backend").textContent, /127/);
  assert.equal(ui.rows().length, 0);
  assert.match(ui.get("table-message").textContent, /No individual test cases/);
});

test("search, suite, status and exact area filters combine and clear", async (t) => {
  const ui = setup(t);
  await flush();
  ui.change("area-filter", agents);
  assert.equal(ui.rows().length, 3);
  ui.change("suite-filter", "frontend");
  assert.equal(ui.rows().length, 1);
  ui.change("status-filter", "passed");
  ui.change("search", "WORKBENCH");
  assert.equal(ui.rows().length, 1);
  ui.change("search", "does not exist");
  assert.equal(ui.rows().length, 0);
  assert.equal(ui.get("table-message").hidden, false);
  assert.match(ui.get("table-message").textContent, /No results match/);
  ui.get("clear-filters").click();
  assert.equal(ui.rows().length, 7);
  ui.change("search", "received 2");
  assert.equal(ui.rows().length, 1);
  assert.match(ui.get("cases").textContent, /reports quality/);
});

test("pagination bounds 605 cases and resets on filters and new runs", async (t) => {
  let value = report({
    status: "passed",
    suites: [
      suite(
        "backend",
        Array.from({ length: 605 }, (_, i) => example(`case ${i + 1}`)),
      ),
      suite("frontend", []),
    ],
  });
  const ui = setup(t, () => ok(value));
  await flush();
  assert.equal(ui.rows().length, 100);
  assert.equal(ui.get("page-info").textContent, "1–100 of 605 cases");
  for (let i = 0; i < 6; i++) ui.get("next").click();
  assert.equal(ui.rows().length, 5);
  assert.equal(ui.get("page-info").textContent, "601–605 of 605 cases");
  assert.equal(ui.get("next").disabled, true);
  await ui.refresh();
  assert.equal(ui.get("page-info").textContent, "601–605 of 605 cases");
  ui.change("search", "case 1");
  assert.equal(ui.get("page-info").textContent, "1–100 of 111 cases");
  ui.get("clear-filters").click();
  ui.get("next").click();
  value = { ...value, run_id: "new-run" };
  await ui.refresh();
  assert.equal(ui.get("page-info").textContent, "1–100 of 605 cases");
  assert.equal(ui.get("previous").disabled, true);
});

test("loading, no report (404), empty report and eventual recovery are distinct", async (t) => {
  const pending = deferred();
  const ui = setup(t, () => pending.promise);
  assert.equal(ui.get("run-status").textContent, "Loading");
  assert.equal(ui.get("report-message").hidden, false);
  assert.match(ui.get("table-message").textContent, /Loading/);
  pending.resolve({ ok: false, status: 404 });
  await flush();
  assert.equal(ui.get("run-status").textContent, "No runs yet");
  assert.equal(ui.get("error").hidden, true);
  assert.match(ui.get("report-message").textContent, /No runs yet/);
  ui.respond(() => ok(report({ status: "passed", suites: [] })));
  await ui.refresh();
  assert.equal(ui.get("run-status").textContent, "No results");
  assert.notEqual(ui.get("run-status").dataset.state, "passed");
  assert.equal(ui.rows().length, 0);
  ui.respond(() => ok(report()));
  await ui.refresh();
  assert.equal(ui.rows().length, 7);
  assert.equal(ui.get("report-message").hidden, true);
});

test("refresh failures clear stale passing results and recover automatically", async (t) => {
  for (const failure of [
    () => Promise.reject(new Error("Offline")),
    () => ({ ok: false, status: 503 }),
    () => ({ ok: true, json: () => Promise.reject(new Error("Invalid JSON")) }),
    () => ok(report({ schema_version: 2 })),
    () => ok({ schema_version: 1, suites: [] }),
  ])
    await t.test(String(failure), async (t) => {
      const value = report({
        status: "passed",
        suites: [
          suite("backend", [example("passing case")]),
          suite("frontend", []),
        ],
      });
      const ui = setup(t, () => ok(value));
      await flush();
      ui.respond(failure);
      await ui.refresh();
      assert.equal(ui.get("run-status").dataset.state, "error");
      assert.equal(ui.get("error").hidden, false);
      assert.equal(ui.d.querySelectorAll('[data-state="passed"]').length, 0);
      assert.equal(ui.get("totals").textContent, "");
      assert.equal(ui.get("suites").textContent, "");
      assert.equal(ui.rows().length, 0);
      assert.match(
        ui.get("table-message").textContent,
        /previous results have been cleared/,
      );
      ui.respond(() => ok(value));
      [...ui.intervals.values()][0].fn();
      await flush();
      assert.equal(ui.get("error").hidden, true);
      assert.equal(ui.get("run-status").dataset.state, "passed");
      assert.equal(ui.rows().length, 1);
    });
});

test("obsolete fetch and JSON successes or failures cannot replace a newer report", async (t) => {
  for (const stage of ["fetch", "json"])
    for (const outcome of ["success", "failure"]) {
      await t.test(`${stage} ${outcome}`, async (t) => {
        const old = deferred();
        const ui = setup(t, () =>
          stage === "fetch"
            ? old.promise
            : { ok: true, json: () => old.promise },
        );
        await flush();
        ui.respond(() => ok(report({ run_id: "latest-report" })));
        await ui.refresh();
        assert.equal(ui.calls[0].options.signal.aborted, true);
        if (outcome === "failure") old.reject(new Error("Old request failed"));
        else
          old.resolve(
            stage === "fetch"
              ? ok(report({ run_id: "obsolete" }))
              : report({ run_id: "obsolete" }),
          );
        await flush();
        assert.match(ui.get("run-metadata").textContent, /latest-report/);
        assert.doesNotMatch(ui.get("run-metadata").textContent, /obsolete/);
        assert.equal(ui.get("error").hidden, true);
        assert.equal(ui.rows().length, 7);
        assert.equal(ui.get("case-panel").getAttribute("aria-busy"), "false");
      });
    }
});

test("a hung request times out, avoids overlapping polls, and ignores late success", async (t) => {
  const pending = deferred();
  const ui = setup(t, () => pending.promise);
  await flush();
  [...ui.intervals.values()][0].fn();
  assert.equal(ui.calls.length, 1);
  const timeout = [...ui.timeouts.values()][0];
  assert.equal(timeout.delay, 15000);
  timeout.fn();
  assert.match(ui.get("error").textContent, /timed out/);
  assert.equal(ui.calls[0].options.signal.aborted, true);
  ui.respond(() => ok(report({ run_id: "after-timeout" })));
  [...ui.intervals.values()][0].fn();
  await flush();
  pending.resolve(ok(report({ run_id: "too-late" })));
  await flush();
  assert.match(ui.get("run-metadata").textContent, /after-timeout/);
  assert.equal(ui.get("error").hidden, true);
});

test("stale heartbeat warns after 90 seconds, preserves completed cases and recovers", async (t) => {
  const value = report({
    status: "running",
    finished_at: null,
    updated_at: new Date(Date.now() - 91000).toISOString(),
  });
  value.suites[1] = suite("frontend", [], {
    status: "running",
    finished_at: null,
  });
  const ui = setup(t, () => ok(value));
  await flush();
  assert.equal(ui.get("run-status").textContent, "Runner not responding");
  assert.equal(ui.get("run-status").dataset.state, "error");
  assert.equal(ui.get("heartbeat-warning").hidden, false);
  assert.match(
    ui.get("heartbeat-warning").textContent,
    /Last update:.*over 90 seconds ago/,
  );
  assert.equal(ui.rows().length, 5);
  assert.equal(readCounts(ui.get("suite-backend")).Passed, "2");
  assert.equal(readCounts(ui.get("suite-frontend")).Total, "—");
  value.updated_at = new Date().toISOString();
  await ui.refresh();
  assert.equal(ui.get("run-status").textContent, "Running");
  assert.equal(ui.get("heartbeat-warning").hidden, true);
  value.status = "failed";
  value.source = "ci";
  value.updated_at = started;
  value.dirty = false;
  await ui.refresh();
  assert.equal(ui.get("heartbeat-warning").hidden, true);
  assert.match(ui.get("run-metadata").textContent, /CI/);
  assert.match(ui.get("run-metadata").textContent, /Clean/);
});

test("a heartbeat that ages while an identical report is polled becomes unresponsive", async (t) => {
  const now = Date.now();
  const value = report({
    status: "running",
    updated_at: new Date(now).toISOString(),
  });
  const ui = setup(t, () => ok(value));
  ui.w.Date.now = () => now;
  await flush();
  assert.equal(ui.get("heartbeat-warning").hidden, true);
  ui.w.Date.now = () => now + 90001;
  [...ui.intervals.values()][0].fn();
  await flush();
  assert.equal(ui.get("heartbeat-warning").hidden, false);
  assert.equal(ui.rows().length, 7);
});

test("report strings and failure details remain literal text, including malicious URLs", async (t) => {
  const payload =
    '<img src=x onerror="window.injected=true"><script>window.injected=true</script>';
  const item = {
    ...example(payload, "failed", payload, payload),
    module: payload,
  };
  const value = report({
    run_id: payload,
    commit: payload,
    ci_url: "javascript:window.injected=true",
    suites: [suite("backend", [item], { error: payload })],
  });
  const ui = setup(t, () => ok(value));
  await flush();
  assert.equal(ui.d.querySelector("#cases .test-name").textContent, payload);
  assert.equal(ui.d.querySelector("#cases pre").textContent, payload);
  assert.equal(ui.d.querySelector(".suite-error").textContent, payload);
  assert.equal(ui.get("run-metadata").querySelector("dd").textContent, payload);
  assert.equal(
    ui.d.querySelectorAll("img, main script, [onerror], a[href^='javascript:']")
      .length,
    0,
  );
  assert.equal(ui.w.injected, undefined);
  const details = ui.d.querySelector("#cases details");
  details.open = true;
  await ui.refresh();
  assert.equal(ui.d.querySelector("#cases details").open, true);
  value.updated_at = new Date().toISOString();
  await ui.refresh();
  assert.equal(ui.d.querySelector("#cases details").open, true);
});
