const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { randomUUID } = require("node:crypto");
const { JSDOM } = require("jsdom");

const publicDir = path.resolve(__dirname, "../public");
const html = fs.readFileSync(path.join(publicDir, "index.html"), "utf8");
const script = fs.readFileSync(path.join(publicDir, "app.js"), "utf8");
const status = {
  mode: "demo",
  model: "demo/deterministic",
  identity: "operator-A",
  authentication: "token",
  profiles: ["business", "investigation"],
  examples: [
    { agent: "business", question: "What was total revenue?" },
    { agent: "investigation", question: "Investigate firm F-01 for 2025-03." },
  ],
};
const businessEvidence = {
  publication_id: "pub-123",
  sql: "SELECT firm_id, revenue_eur FROM published WHERE month = :month",
  parameters: { month: "2025-03" },
  columns: ["firm_id", "revenue_eur"],
  rows: [
    ["F-01", 0],
    ["F-02", 140],
  ],
  coverage: { firms: 2, missing: 1 },
  metric_definition: "Published net revenue, EUR",
  truncated: true,
};
const investigationEvidence = {
  investigation_id: "inv-123",
  firm_id: "F-01",
  month: "2025-03",
  status: "mismatch",
  findings: [
    {
      code: "published_api",
      status: "match",
      detail: "Published and API agree.",
      delta: 0,
    },
    {
      code: "original",
      status: "mismatch",
      detail: "Original differs.",
      delta: 25,
    },
    { code: "lineage", status: "unavailable", detail: "Source missing." },
  ],
  evidence: {
    publication_id: "pub-123",
    versions: {
      items: [
        { record_id: "r1", revenue_eur: 75 },
        { record_id: "r2", revenue_eur: 100, selected: true },
      ],
      total: 3,
      truncated: true,
    },
    selected_record_id: "r2",
    published_revenue_eur: 100,
    api_revenue_eur: 100,
    original_revenue_eur: 75,
    sql: [
      {
        sql: "SELECT * FROM versions WHERE firm_id = :firm",
        parameters: { firm: "F-01" },
      },
    ],
    limitations: ["Only retained versions are available."],
    additional_provenance: { source: "input.csv" },
  },
};
const response = (body, overrides = {}) => ({
  request_id: body.request_id,
  conversation_id: body.conversation_id || randomUUID(),
  agent: body.agent,
  status: "answered",
  answer: "Revenue is supported by the following evidence.",
  plan: ["Read published data", "Compare revenue"],
  evidence:
    body.agent === "business" ? businessEvidence : investigationEvidence,
  trace: {
    run_id: "run-123",
    model: "demo/deterministic",
    mode: "demo",
    input_tokens: 10,
    output_tokens: 20,
    elapsed_ms: 25,
    cost_usd: "0.000000",
  },
  ...overrides,
});
const ok = (value) => ({ ok: true, json: async () => value });
async function until(predicate) {
  for (let i = 0; i < 100; i++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("Workbench did not reach expected state");
}
async function setup(
  t,
  handler = (url, options, body) => ok(body ? response(body) : status),
) {
  const dom = new JSDOM(html, {
    url: "http://localhost/agents",
    runScripts: "outside-only",
  });
  t.after(() => dom.window.close());
  const w = dom.window;
  const d = w.document;
  const calls = [];
  w.crypto.randomUUID = randomUUID;
  w.fetch = async (url, options = {}) => {
    assert.ok(
      ["/api/v1/agents/status", "/api/v1/agents/ask"].includes(url),
      "Only the specified API endpoints are used",
    );
    const body = options.body ? JSON.parse(options.body) : null;
    calls.push({ url, options, body });
    return handler(url, options, body);
  };
  w.eval(script);
  await until(() => !d.getElementById("refresh-status").disabled);
  const get = (id) => d.getElementById(id);
  function submit(question) {
    if (question !== undefined) {
      get("question").value = question;
      get("question").dispatchEvent(new w.Event("input"));
    }
    get("ask-form").dispatchEvent(
      new w.Event("submit", { bubbles: true, cancelable: true }),
    );
  }
  function select(agent) {
    const radio = d.querySelector(`input[value="${agent}"]`);
    radio.checked = true;
    radio.dispatchEvent(new w.Event("change", { bubbles: true }));
  }
  const idle = () =>
    until(() => get("transcript").getAttribute("aria-busy") === "false");
  const posts = () => calls.filter((call) => call.body);
  return { w, d, get, calls, submit, select, idle, posts };
}

test("static route assets are local and status/examples are read-only", async (t) => {
  const ui = await setup(t);
  assert.equal(
    ui.d.querySelector("script[src]").getAttribute("src"),
    "/agent-static/app.js",
  );
  assert.equal(
    ui.d.querySelector("link[rel=stylesheet]").getAttribute("href"),
    "/agent-static/style.css",
  );
  assert.match(
    ui.get("service-status").textContent,
    /demo.*demo\/deterministic/,
  );
  assert.equal(ui.get("send").disabled, false);
  ui.get("examples").querySelector("button").click();
  assert.equal(ui.get("question").value, status.examples[0].question);
  assert.equal(ui.posts().length, 0);
  ui.select("investigation");
  assert.match(ui.get("examples").textContent, /Investigate firm/);
  assert.doesNotMatch(ui.get("examples").textContent, /total revenue/);
  assert.equal(ui.get("investigation-form").hidden, false);
});

test("business answer exposes SQL, parameters, results, coverage, plan, and trace", async (t) => {
  const ui = await setup(t);
  ui.submit("  Total revenue?  ");
  await ui.idle();
  const call = ui.posts()[0];
  assert.equal(call.options.method, "POST");
  assert.equal(call.options.headers["X-Pulse-Agent"], "assistant-v1");
  assert.equal(call.options.headers["Content-Type"], "application/json");
  assert.equal(call.options.headers.Authorization, undefined);
  assert.match(call.body.request_id, /^[0-9a-f-]{36}$/);
  assert.deepEqual(Object.keys(call.body).sort(), [
    "agent",
    "conversation_id",
    "question",
    "request_id",
  ]);
  assert.equal(call.body.conversation_id, null);
  assert.equal(call.body.agent, "business");
  assert.equal(call.body.question, "Total revenue?");
  const text = ui.get("transcript").textContent;
  for (const value of [
    "SELECT",
    "2025-03",
    "pub-123",
    "Results truncated",
    "Metric definition",
    "Coverage",
    "Plan",
    "$0.000000 USD",
    "25 ms",
    "Provider / model",
    "input_tokens",
    "run-123",
  ])
    assert.ok(text.includes(value), value);
  assert.equal(ui.d.querySelectorAll("tbody tr").length, 2);
  assert.equal(ui.d.querySelector("tbody tr td:last-child").textContent, "0");
  const full = [...ui.d.querySelectorAll("details")].find(
    (el) =>
      el.querySelector("summary").textContent === "Complete evidence JSON",
  );
  assert.deepEqual(
    JSON.parse(full.querySelector("pre").textContent),
    businessEvidence,
  );
  assert.equal(ui.get("question").value, "");
});

test("clarification, mode switches, drafts, and resets keep conversations isolated", async (t) => {
  const businessID = randomUUID();
  const investigationID = randomUUID();
  const ui = await setup(t, (url, options, body) =>
    ok(
      body
        ? response(body, {
            conversation_id:
              body.agent === "business" ? businessID : investigationID,
            status: body.question === "Revenue?" ? "clarification" : "answered",
          })
        : status,
    ),
  );
  ui.submit("Revenue?");
  await ui.idle();
  assert.match(ui.get("request-status").textContent, /clarification/);
  ui.get("question").value = "March 2025";
  ui.select("investigation");
  assert.equal(ui.get("question").value, "");
  ui.submit("Check F-01");
  await ui.idle();
  assert.equal(ui.posts()[1].body.conversation_id, null);
  ui.select("business");
  assert.equal(ui.get("question").value, "March 2025");
  ui.submit();
  await ui.idle();
  assert.equal(ui.posts()[2].body.conversation_id, businessID);
  assert.equal(ui.d.querySelectorAll(".turn").length, 2);
  ui.get("new-conversation").click();
  assert.equal(ui.d.querySelectorAll(".turn").length, 0);
  ui.submit("Start again");
  await ui.idle();
  assert.equal(ui.posts()[3].body.conversation_id, null);
  ui.select("investigation");
  assert.match(
    ui.get("conversation-id").textContent,
    new RegExp(investigationID),
  );
  assert.equal(ui.d.querySelectorAll(".turn").length, 1);
  assert.equal(new Set(ui.posts().map((call) => call.body.request_id)).size, 4);
});

test("exact firm/month prepares a question and investigator shows the complete report", async (t) => {
  const ui = await setup(t);
  ui.select("investigation");
  ui.get("firm-id").value = "F-01";
  ui.get("month").value = "2025-03";
  ui.get("investigation-form").dispatchEvent(
    new ui.w.Event("submit", { cancelable: true }),
  );
  assert.equal(ui.posts().length, 0);
  assert.match(ui.get("question").value, /firm "F-01" for month 2025-03/);
  ui.submit();
  await ui.idle();
  assert.equal(ui.posts()[0].body.agent, "investigation");
  assert.equal(ui.d.querySelectorAll(".timeline li").length, 3);
  for (const state of ["match", "mismatch", "unavailable"])
    assert.ok(ui.d.querySelector(`.timeline [data-state="${state}"]`));
  const text = ui.get("transcript").textContent;
  for (const value of [
    "inv-123",
    "Selected record: r2",
    "2 shown / 3 total · Truncated",
    "Record versions",
    "Revenue comparison (EUR)",
    "Only retained versions",
    "SELECT * FROM versions",
  ])
    assert.ok(text.includes(value), value);
  const full = [...ui.d.querySelectorAll("details")].find(
    (el) =>
      el.querySelector("summary").textContent === "Complete evidence JSON",
  );
  assert.deepEqual(
    JSON.parse(full.querySelector("pre").textContent),
    investigationEvidence,
  );
  assert.equal(
    ui.d.querySelectorAll("table")[1].querySelectorAll("tbody tr").length,
    2,
  );
});

test("double submissions are blocked and late responses stay with their original agent", async (t) => {
  let finish;
  const ui = await setup(t, (url, options, body) =>
    body
      ? new Promise((resolve) => {
          finish = () => resolve(ok(response(body)));
        })
      : ok(status),
  );
  ui.submit("Pending business question");
  ui.submit("Duplicate question");
  ui.get("send").click();
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.get("new-conversation").disabled, true);
  ui.get("new-conversation").click();
  ui.select("investigation");
  assert.equal(ui.get("send").disabled, false);
  finish();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(ui.d.querySelectorAll(".turn").length, 0);
  assert.equal(ui.get("conversation-id").textContent, "New conversation");
  ui.select("business");
  await ui.idle();
  assert.equal(ui.d.querySelectorAll(".turn").length, 1);
  assert.match(ui.get("transcript").textContent, /Pending business question/);
});

test("bearer token is optional, sent to status and ask, never persisted, and can be cleared", async (t) => {
  const ui = await setup(t);
  const token = "test-deployment-token";
  ui.get("access-token").value = token;
  ui.get("refresh-status").click();
  await until(() => !ui.get("refresh-status").disabled);
  assert.equal(
    ui.calls.at(-1).options.headers.Authorization,
    `Bearer ${token}`,
  );
  ui.submit("Revenue?");
  await ui.idle();
  assert.equal(ui.posts()[0].options.headers.Authorization, `Bearer ${token}`);
  assert.equal(ui.w.localStorage.length, 0);
  assert.equal(ui.w.sessionStorage.length, 0);
  assert.ok(!ui.d.body.textContent.includes(token));
  ui.get("clear-token").click();
  assert.equal(ui.get("send").disabled, true);
  ui.get("refresh-status").click();
  await until(() => !ui.get("refresh-status").disabled);
  ui.submit("Another question");
  await ui.idle();
  assert.equal(ui.posts()[1].options.headers.Authorization, undefined);
});

test("untrusted answers, examples, SQL, parameters, findings, and rows remain literal text", async (t) => {
  const hostile =
    '<img src=x onerror="window.pwned=true"><script>window.pwned=true</script>';
  const ui = await setup(t, (url, options, body) =>
    ok(
      body
        ? response(body, {
            answer: hostile,
            plan: hostile,
            evidence:
              body.agent === "business"
                ? {
                    ...businessEvidence,
                    sql: hostile,
                    parameters: { x: hostile },
                    columns: [hostile],
                    rows: [[hostile]],
                  }
                : {
                    ...investigationEvidence,
                    findings: [
                      { code: hostile, status: "mismatch", detail: hostile },
                    ],
                  },
            trace: { ...response(body).trace, model: hostile },
          })
        : {
            ...status,
            model: hostile,
            examples: [{ agent: "business", question: hostile }],
          },
    ),
  );
  ui.get("examples").querySelector("button").click();
  ui.submit();
  await ui.idle();
  assert.ok(ui.get("transcript").textContent.includes(hostile));
  ui.select("investigation");
  ui.submit(hostile);
  await ui.idle();
  assert.ok(ui.d.querySelector(".timeline").textContent.includes(hostile));
  assert.equal(ui.w.pwned, undefined);
  assert.equal(
    ui.d.querySelectorAll("img, script:not([src]), [onerror]").length,
    0,
  );
});

test("status failures disable requests and refresh recovers with profile availability", async (t) => {
  let healthy = false;
  const ui = await setup(t, () =>
    healthy
      ? ok({ ...status, mode: "live", profiles: ["investigation"] })
      : { ok: false, status: 401 },
  );
  assert.equal(ui.get("send").disabled, true);
  assert.match(ui.get("error").textContent, /Access denied/);
  ui.submit("Must not send");
  assert.equal(ui.posts().length, 0);
  healthy = true;
  ui.get("refresh-status").click();
  await until(() => !ui.get("refresh-status").disabled);
  assert.equal(ui.get("send").disabled, true);
  assert.equal(ui.get("error").hidden, true);
  ui.select("investigation");
  assert.equal(ui.get("send").disabled, false);
  assert.equal(ui.get("service-status").dataset.state, "live");
});

test("transport, HTTP, invalid JSON, and invalid envelopes release busy state without retry", async (t) => {
  const cases = [
    () => {
      throw new Error("Network offline");
    },
    () => ({ ok: false, status: 503 }),
    () => ({ ok: false, status: 403 }),
    () => ({
      ok: true,
      json: async () => {
        throw new SyntaxError("Invalid JSON");
      },
    }),
    (body) => ok(response(body, { request_id: "wrong-request" })),
    (body) => ok(response(body, { agent: "investigation" })),
    (body) => ok(response(body, { conversation_id: "not-a-uuid" })),
    (body) => ok(response(body, { status: "unknown" })),
  ];
  for (const fail of cases) {
    await t.test(fail.toString().slice(0, 85), async (t) => {
      let broken = true;
      const ui = await setup(t, (url, options, body) =>
        body ? (broken ? fail(body) : ok(response(body))) : ok(status),
      );
      ui.submit("Keep my question");
      await ui.idle();
      assert.equal(ui.posts().length, 1);
      assert.equal(ui.get("send").disabled, false);
      assert.equal(ui.get("question").value, "Keep my question");
      assert.equal(ui.get("error").hidden, false);
      assert.equal(ui.get("conversation-id").textContent, "New conversation");
      broken = false;
      assert.equal(ui.get("send").textContent, "Start new execution");
      ui.d.querySelector(".recover-request").click();
      await ui.idle();
      assert.equal(ui.posts().length, 2);
      assert.equal(ui.posts()[1].options.body, ui.posts()[0].options.body);
      assert.equal(ui.posts()[1].body.conversation_id, null);
      assert.equal(ui.get("error").hidden, true);
    });
  }
});

test("unsupported and agent errors render explicitly and preserve the conversation", async (t) => {
  for (const resultStatus of ["unsupported", "error"]) {
    await t.test(resultStatus, async (t) => {
      const ui = await setup(t, (url, options, body) =>
        ok(
          body
            ? response(body, {
                status: resultStatus,
                answer: "Cannot answer this request.",
                evidence: null,
                trace: {},
              })
            : status,
        ),
      );
      ui.submit("Unsupported request");
      await ui.idle();
      assert.equal(
        ui.d.querySelector(".turn .badge").textContent,
        resultStatus,
      );
      assert.match(
        ui.get("transcript").textContent,
        /Cannot answer.*No evidence/s,
      );
      assert.match(ui.get("transcript").textContent, /Cost unavailable/);
      assert.equal(ui.get("send").disabled, false);
      ui.submit("Follow-up");
      await ui.idle();
      assert.ok(ui.posts()[1].body.conversation_id);
    });
  }
});

test("empty questions and incomplete exact forms cannot post", async (t) => {
  const ui = await setup(t);
  ui.submit("   ");
  assert.match(ui.get("error").textContent, /Enter a question/);
  ui.select("investigation");
  ui.get("firm-id").value = "   ";
  ui.get("month").value = "2025-03";
  ui.get("investigation-form").dispatchEvent(
    new ui.w.Event("submit", { cancelable: true }),
  );
  assert.match(ui.get("error").textContent, /exact firm ID/);
  assert.equal(ui.posts().length, 0);
});

test("empty evidence tables and object rows are supported", async (t) => {
  let rows = [];
  const ui = await setup(t, (url, options, body) =>
    ok(
      body
        ? response(body, {
            evidence: {
              ...businessEvidence,
              columns: [{ name: "firm_id" }, { name: "revenue_eur" }],
              rows,
            },
          })
        : status,
    ),
  );
  ui.submit("Empty query");
  await ui.idle();
  assert.match(ui.get("transcript").textContent, /no rows returned/);
  rows = [{ firm_id: "F-01", revenue_eur: null }];
  ui.submit("Object rows");
  await ui.idle();
  assert.equal(ui.d.querySelector("tbody td").textContent, "F-01");
  assert.equal(ui.d.querySelector("tbody td:last-child").textContent, "—");
});

test("request timeout releases controls without automatically reposting", async (t) => {
  let attempts = 0;
  const ui = await setup(t, (url, options, body) => {
    if (!body) return ok(status);
    attempts += 1;
    if (attempts > 1) return ok(response(body));
    return new Promise((resolve, reject) => {
      options.signal.addEventListener("abort", () => {
        const error = new Error("Aborted");
        error.name = "AbortError";
        reject(error);
      });
    });
  });
  const originalTimeout = ui.w.setTimeout.bind(ui.w);
  ui.w.setTimeout = (callback, delay) =>
    originalTimeout(callback, delay === 90000 ? 1 : delay);
  ui.submit("Slow question");
  await ui.idle();
  assert.match(ui.get("error").textContent, /timed out.*no automatic retry/);
  assert.equal(ui.posts().length, 1);
  assert.equal(ui.get("send").disabled, false);
  assert.equal(ui.get("question").value, "Slow question");
  ui.d.querySelector(".recover-request").click();
  await ui.idle();
  assert.equal(ui.posts().length, 2);
  assert.equal(ui.posts()[1].options.body, ui.posts()[0].options.body);
  assert.equal(ui.d.querySelectorAll(".turn").length, 1);
  assert.equal(ui.get("question").value, "");
});

test("concurrent agents can finish out of order without mixing results", async (t) => {
  const pending = new Map();
  const ui = await setup(t, (url, options, body) => {
    if (!body) return ok(status);
    return new Promise((resolve) =>
      pending.set(body.agent, () => resolve(ok(response(body)))),
    );
  });
  ui.submit("Business pending");
  ui.select("investigation");
  ui.submit("Investigation pending");
  pending.get("business")();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(ui.get("send").disabled, true);
  assert.doesNotMatch(ui.get("transcript").textContent, /Business pending/);
  pending.get("investigation")();
  await ui.idle();
  assert.match(ui.get("transcript").textContent, /Finding timeline/);
  const investigationID = ui.get("conversation-id").textContent;
  ui.select("business");
  assert.match(ui.get("transcript").textContent, /Business pending/);
  assert.doesNotMatch(
    ui.get("transcript").textContent,
    /Investigation pending/,
  );
  assert.notEqual(ui.get("conversation-id").textContent, investigationID);
  assert.equal(ui.posts().length, 2);
});

function changeToken(ui, value, event = "input") {
  ui.get("access-token").value = value;
  ui.get("access-token").dispatchEvent(
    new ui.w.Event(event, { bubbles: true }),
  );
}
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

test("credential edits and clear erase both sessions, recovery payloads, drafts, and availability", async (t) => {
  const ui = await setup(t, (url, options, body) => {
    if (body?.question === "Uncertain A") throw new Error("Disconnected");
    return ok(body ? response(body) : status);
  });
  changeToken(ui, "token-A");
  ui.get("refresh-status").click();
  await until(() => !ui.get("refresh-status").disabled);
  ui.submit("Private business A");
  await ui.idle();
  ui.select("investigation");
  ui.submit("Private investigation A");
  await ui.idle();
  ui.submit("Uncertain A");
  await ui.idle();
  const detachedRecovery = ui.d.querySelector(".recover-request");
  ui.get("firm-id").value = "PRIVATE-FIRM";
  ui.get("month").value = "2025-03";
  ui.get("clear-token").click();
  assert.equal(ui.get("access-token").value, "");
  assert.equal(ui.get("service-identity").textContent, "Identity not verified");
  assert.equal(ui.get("firm-id").value, "");
  assert.equal(ui.get("month").value, "");
  for (const agent of ["business", "investigation"]) {
    ui.select(agent);
    assert.equal(ui.get("send").disabled, true);
    assert.equal(ui.get("conversation-id").textContent, "New conversation");
    assert.equal(ui.get("question").value, "");
    assert.equal(ui.d.querySelectorAll(".turn").length, 0);
    assert.equal(ui.get("examples").querySelectorAll("button").length, 0);
    assert.equal(ui.get("error").hidden, true);
    assert.doesNotMatch(
      ui.get("transcript").textContent,
      /pub-123|Private|Uncertain/,
    );
  }
  changeToken(ui, "token-B", "change");
  ui.get("refresh-status").click();
  await until(() => !ui.get("refresh-status").disabled);
  detachedRecovery.click();
  assert.equal(
    ui.posts().length,
    3,
    "A detached recovery control cannot resend under another credential",
  );
  ui.submit("New identity question");
  await ui.idle();
  assert.equal(ui.posts().at(-1).body.conversation_id, null);
  assert.equal(
    ui.posts().at(-1).options.headers.Authorization,
    "Bearer token-B",
  );
});

test("old ask and status continuations cannot cross a credential generation, even when abort is ignored", async (t) => {
  for (const stage of ["fetch", "json"]) {
    for (const outcome of ["resolve", "reject"]) {
      await t.test(`${stage} ${outcome}`, async (t) => {
        let hold = false;
        const pending = [];
        const ui = await setup(t, (url, options, body) => {
          const value = body
            ? response(body, {
                answer: options.headers.Authorization
                  ? "Private B"
                  : "Private A",
              })
            : {
                ...status,
                identity: options.headers.Authorization
                  ? "operator-B"
                  : "operator-A",
              };
          if (!hold) return ok(value);
          const deferred = new Promise((resolve, reject) => {
            pending.push({
              options,
              body,
              finish: () => resolve(stage === "json" ? value : ok(value)),
              fail: () => reject(new Error("Old identity failure")),
            });
          });
          return stage === "json"
            ? { ok: true, json: () => deferred }
            : deferred;
        });
        hold = true;
        ui.submit("A business pending");
        ui.select("investigation");
        ui.submit("A investigation pending");
        ui.get("refresh-status").click();
        await flush();
        const old = pending.slice();
        assert.equal(old.length, 3);
        changeToken(ui, "token-B");
        old.forEach((entry) =>
          assert.equal(entry.options.signal.aborted, true),
        );
        assert.equal(ui.get("send").disabled, true);
        ui.get("refresh-status").click();
        await flush();
        // A's status finally must not unlock B's still-pending status request.
        old[2][outcome === "resolve" ? "finish" : "fail"]();
        await flush();
        assert.equal(ui.get("refresh-status").disabled, true);
        assert.equal(ui.get("send").disabled, true);
        assert.equal(ui.get("error").hidden, true);
        pending[3].finish();
        await until(() => !ui.get("refresh-status").disabled);
        assert.match(ui.get("service-identity").textContent, /operator-B/);
        ui.submit("B pending");
        await flush();
        old
          .slice(0, 2)
          .forEach((entry) =>
            entry[outcome === "resolve" ? "finish" : "fail"](),
          );
        await flush();
        assert.equal(ui.get("transcript").getAttribute("aria-busy"), "true");
        assert.equal(ui.get("send").disabled, true);
        assert.doesNotMatch(
          ui.get("transcript").textContent,
          /Private A|A investigation|Old identity/,
        );
        pending[4].finish();
        await ui.idle();
        assert.match(ui.get("transcript").textContent, /Private B/);
        assert.equal(ui.posts().at(-1).body.conversation_id, null);
        ui.select("business");
        assert.equal(ui.d.querySelectorAll(".turn").length, 0);
        assert.equal(ui.get("conversation-id").textContent, "New conversation");
      });
    }
  }
});

test("a changed server identity invalidates both sessions and aborts outstanding asks", async (t) => {
  let identity = "operator-A";
  let finish;
  const ui = await setup(t, (url, options, body) => {
    if (body?.question === "A pending")
      return new Promise((resolve) => {
        finish = () => resolve(ok(response(body)));
      });
    return ok(body ? response(body) : { ...status, identity });
  });
  ui.submit("A completed");
  await ui.idle();
  ui.select("investigation");
  ui.submit("A pending");
  identity = "operator-B";
  ui.get("refresh-status").click();
  await until(() => !ui.get("refresh-status").disabled);
  assert.equal(ui.posts()[1].options.signal.aborted, true);
  finish();
  await flush();
  for (const agent of ["business", "investigation"]) {
    ui.select(agent);
    assert.equal(ui.d.querySelectorAll(".turn").length, 0);
    assert.equal(ui.get("conversation-id").textContent, "New conversation");
    assert.equal(ui.get("send").disabled, false);
  }
  assert.match(ui.get("service-identity").textContent, /operator-B/);
});

test("recovering an old null-conversation request preserves exact bytes and the newer conversation", async (t) => {
  let firstID;
  let finish;
  const ui = await setup(t, (url, options, body) => {
    if (!body) return ok(status);
    if (!firstID) {
      firstID = body.request_id;
      throw new Error("Response lost after execution");
    }
    if (body.request_id === firstID)
      return new Promise((resolve) => {
        finish = () => resolve(ok(response(body)));
      });
    return ok(response(body));
  });
  ui.submit("Original exact question");
  await ui.idle();
  const original = ui.posts()[0];
  assert.equal(ui.get("send").textContent, "Start new execution");
  assert.equal(ui.get("execution-note").hidden, false);
  assert.match(ui.get("execution-note").textContent, /another charge/);
  ui.submit("Original exact question");
  await ui.idle();
  assert.notEqual(ui.posts()[1].body.request_id, firstID);
  const newerConversation = ui.get("conversation-id").textContent;
  ui.get("question").value = "Unsent edited draft";
  ui.get("question").dispatchEvent(new ui.w.Event("input"));
  const recovery = ui.d.querySelector(".recover-request");
  recovery.click();
  recovery.click();
  assert.equal(ui.posts().length, 3);
  assert.equal(ui.posts()[2].options.body, original.options.body);
  assert.equal(ui.posts()[2].body.conversation_id, null);
  assert.equal(ui.posts()[2].body.question, "Original exact question");
  assert.equal(ui.get("send").disabled, true);
  finish();
  await ui.idle();
  assert.equal(ui.get("conversation-id").textContent, newerConversation);
  assert.equal(ui.get("question").value, "Unsent edited draft");
  assert.equal(ui.d.querySelectorAll(".turn").length, 2);
  assert.equal(ui.d.querySelectorAll(".recover-request").length, 0);
  assert.equal(ui.get("send").textContent, "Ask agent");
});

test("repeated recovery retains the original UUID and non-null conversation through HTTP 409", async (t) => {
  let attempt = 0;
  const ui = await setup(t, (url, options, body) => {
    if (!body) return ok(status);
    if (body.question === "Clarifying detail") {
      attempt += 1;
      if (attempt === 1) throw new Error("Connection lost");
      if (attempt === 2) return { ok: false, status: 409 };
    }
    return ok(
      response(body, {
        status: body.question === "Revenue?" ? "clarification" : "answered",
      }),
    );
  });
  ui.submit("Revenue?");
  await ui.idle();
  ui.submit("Clarifying detail");
  await ui.idle();
  const original = ui.posts()[1];
  assert.ok(original.body.conversation_id);
  for (let i = 0; i < 2; i++) {
    ui.d.querySelector(".recover-request").click();
    await ui.idle();
    assert.equal(ui.posts().at(-1).options.body, original.options.body);
  }
  assert.equal(ui.posts().length, 4);
  assert.equal(ui.d.querySelectorAll(".turn").length, 2);
  assert.equal(ui.d.querySelectorAll(".recover-request").length, 0);
});

test("cost badges preserve decimal strings exactly and reject invalid or unknown costs", async (t) => {
  let cost;
  const ui = await setup(t, (url, options, body) =>
    ok(
      body
        ? response(body, { trace: { ...response(body).trace, cost_usd: cost } })
        : status,
    ),
  );
  for (const value of [
    "0",
    "0.000000",
    "0.001234567890123456789",
    "12345678901234567890.1200",
  ]) {
    cost = value;
    ui.submit("Cost check");
    await ui.idle();
    const badges = [...ui.d.querySelectorAll(".turn:last-child .badge")].map(
      (badge) => badge.textContent,
    );
    assert.ok(badges.includes(`$${value} USD`));
  }
  for (const value of [
    null,
    undefined,
    "",
    " 0.1",
    "-1",
    "NaN",
    "Infinity",
    "1e-6",
    "0.2\n",
    "<img>",
    0,
    0.5,
  ]) {
    cost = value;
    ui.submit("Unknown cost check");
    await ui.idle();
    const badges = [...ui.d.querySelectorAll(".turn:last-child .badge")].map(
      (badge) => badge.textContent,
    );
    assert.ok(
      badges.includes("Cost unavailable"),
      `Invalid cost: ${String(value)}`,
    );
    assert.ok(!badges.some((badge) => badge.startsWith("$")));
  }
});
