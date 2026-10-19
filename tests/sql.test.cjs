const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM } = require("jsdom");
const html = fs.readFileSync("src/pulse/static/lab.html", "utf8");
const script = fs.readFileSync("src/pulse/static/sql.js", "utf8");
const schema = {
  database: "pulse_lab",
  role: "pulse_reader",
  tables: [
    {
      schema: "raw",
      name: "records",
      columns: [
        {
          column: "payload",
          type: "jsonb",
          nullable: false,
          primary_key: false,
        },
      ],
    },
  ],
};
const result = {
  columns: [
    { name: "value", type: "numeric" },
    { name: "value", type: "text" },
    { name: "empty", type: "text" },
  ],
  rows: [["9007199254740993.78", "<img src=x onerror=alert(1)>", null]],
  row_count: 1,
  elapsed_ms: 3,
  truncated: false,
  snapshot_at: "2026-10-18T08:00:00Z",
  limit: 500,
};
async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("SQL workspace did not reach the expected state");
}

test("SQL editor submits arbitrary text, preserves exact values and safely renders cells", async () => {
  const dom = new JSDOM(html, {
    url: "http://localhost:8001",
    runScripts: "outside-only",
  });
  const w = dom.window,
    d = w.document,
    calls = [];
  w.fetch = async (path, options = {}) => {
    if (options.body) calls.push(JSON.parse(options.body));
    return {
      ok: true,
      json: async () => (path.endsWith("schema") ? schema : result),
    };
  };
  try {
    w.eval(script);
    await until(() => d.querySelectorAll(".sql-table").length === 1);
    assert.equal(calls.length, 0, "No query executes automatically");
    assert.match(d.getElementById("sql-connection").textContent, /pulse_lab/);
    const editor = d.getElementById("sql-editor");
    editor.value = "WITH chosen AS (SELECT 42 AS n) SELECT n FROM chosen;";
    editor.setSelectionRange(0, 0);
    d.getElementById("sql-run").click();
    await until(() => d.querySelectorAll("#sql-results tbody tr").length === 1);
    assert.equal(calls[0].sql, editor.value);
    assert.equal(calls[0].explain, false);
    const cells = d.querySelectorAll("#sql-results td");
    assert.equal(cells[0].textContent, "9007199254740993.78");
    assert.equal(cells[1].textContent, "<img src=x onerror=alert(1)>");
    assert.equal(d.querySelector("#sql-results img"), null);
    assert.equal(cells[2].textContent, "NULL");
    assert.equal(cells[2].dataset.null, "true");
    assert.equal(d.querySelectorAll("#sql-results th").length, 3);
    assert.match(
      w.localStorage.getItem("pulse.lab.sql.draft.v1"),
      /WITH chosen/,
    );
    editor.value = "SELECT ";
    editor.setSelectionRange(7, 7);
    d.querySelector(".sql-table > button").click();
    assert.equal(editor.value, 'SELECT "raw"."records"');
    assert.equal(calls.length, 1);
  } finally {
    w.close();
  }
});

test("SQL selection, plan, truncation and error recovery leave no stale results", async () => {
  const dom = new JSDOM(html, {
    url: "http://localhost:8001",
    runScripts: "outside-only",
  });
  const w = dom.window,
    d = w.document,
    calls = [];
  w.fetch = async (path, options = {}) => {
    if (!options.body) return { ok: true, json: async () => schema };
    const body = JSON.parse(options.body);
    calls.push(body);
    if (body.sql === "broken")
      return {
        ok: false,
        json: async () => ({
          detail: { message: "syntax error", sqlstate: "42601" },
        }),
      };
    return {
      ok: true,
      json: async () => ({
        ...result,
        explain: body.explain,
        truncated: !body.explain,
        truncation_reason: "row_limit",
      }),
    };
  };
  try {
    w.eval(script);
    const editor = d.getElementById("sql-editor");
    editor.value = "SELECT 1;\nSELECT 2;";
    editor.setSelectionRange(10, 19);
    editor.dispatchEvent(
      new w.KeyboardEvent("keydown", {
        key: "Enter",
        ctrlKey: true,
        bubbles: true,
      }),
    );
    await until(() => !d.getElementById("sql-results-wrap").hidden);
    assert.equal(calls[0].sql, "SELECT 2;");
    assert.match(d.getElementById("sql-status").textContent, /Hay más filas/);
    d.getElementById("sql-plan").click();
    await until(() =>
      d.getElementById("sql-status").textContent.startsWith("Plan:"),
    );
    assert.equal(calls[1].explain, true);
    editor.value = "broken";
    editor.setSelectionRange(0, 0);
    d.getElementById("sql-run").click();
    await until(() => !d.getElementById("sql-error").hidden);
    assert.match(
      d.getElementById("sql-error").textContent,
      /42601.*syntax error/,
    );
    assert.equal(d.getElementById("sql-results-wrap").hidden, true);
    assert.equal(d.getElementById("sql-run").disabled, false);
    d.querySelector("#sql-examples button").click();
    assert.equal(calls.length, 3, "Loading an example must not execute it");
    d.getElementById("sql-run").click();
    await until(() => !d.getElementById("sql-results-wrap").hidden);
    assert.equal(d.getElementById("sql-error").hidden, true);
  } finally {
    w.close();
  }
});
