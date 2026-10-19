(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const names = {
    business: "Business assistant",
    investigation: "Data investigator",
  };
  const fresh = () => ({
    conversation: null,
    turns: [],
    draft: "",
    busy: false,
    error: "",
  });
  const sessions = { business: fresh(), investigation: fresh() };
  let active = "business";
  let service = null;
  let statusBusy = false;
  let statusError = "";
  let credential = $("access-token").value;
  let authGeneration = 0;
  let authenticatedIdentity;
  const controllers = new Set();
  const uuid =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  function display(value) {
    if (value === undefined || value === null) return "—";
    return typeof value === "object"
      ? JSON.stringify(value, null, 2)
      : String(value);
  }
  function node(tag, value, className) {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = display(value);
    if (className) element.className = className;
    return element;
  }
  function badge(parent, value, state) {
    const element = node("span", value, "badge");
    if (state) element.dataset.state = state;
    parent.append(element);
  }
  function inspect(parent, title, value, open = false) {
    const details = node("details");
    details.open = open;
    details.append(node("summary", title), node("pre", display(value)));
    parent.append(details);
  }
  function table(parent, title, columns, rows) {
    if (!Array.isArray(rows) || !rows.length) {
      parent.append(node("p", `${title}: no rows returned.`, "note"));
      return;
    }
    const keys =
      Array.isArray(columns) && columns.length
        ? columns.map((column) =>
            typeof column === "object" && column !== null
              ? column.name
              : column,
          )
        : [
            ...new Set(
              rows.flatMap((row) =>
                row && typeof row === "object" ? Object.keys(row) : ["value"],
              ),
            ),
          ];
    const wrap = node("div", undefined, "table-wrap");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", title);
    const grid = node("table");
    grid.append(node("caption", `${title} · ${rows.length} rows`));
    const head = node("thead");
    const headings = node("tr");
    keys.forEach((key) => {
      const cell = node("th", key);
      cell.scope = "col";
      headings.append(cell);
    });
    head.append(headings);
    const body = node("tbody");
    rows.forEach((row) => {
      const tr = node("tr");
      keys.forEach((key, i) =>
        tr.append(
          node(
            "td",
            Array.isArray(row)
              ? row[i]
              : row && typeof row === "object"
                ? row[key]
                : row,
          ),
        ),
      );
      body.append(tr);
    });
    grid.append(head, body);
    wrap.append(grid);
    parent.append(wrap);
  }
  function sql(parent, statements) {
    if (!Array.isArray(statements) || !statements.length) return;
    const details = node("details");
    details.open = true;
    details.append(node("summary", "SQL and parameters"));
    statements.forEach((statement, index) => {
      details.append(
        node("h3", `Statement ${index + 1}`),
        node("pre", statement.sql),
      );
      details.append(
        node("h3", "Parameters"),
        node("pre", display(statement.parameters)),
      );
    });
    parent.append(details);
  }
  function evidence(parent, result) {
    const report = result.evidence;
    if (!report || typeof report !== "object") {
      parent.append(
        node("p", "No evidence returned for this response.", "note"),
      );
      return;
    }
    if (result.agent === "investigation") {
      const source = report.evidence || {};
      parent.append(node("h3", "Investigation"));
      parent.append(
        node(
          "p",
          `Firm: ${display(report.firm_id)} · Month: ${display(report.month)} · Status: ${display(report.status)}`,
          "note",
        ),
      );
      parent.append(
        node(
          "p",
          `Investigation: ${display(report.investigation_id)} · Publication: ${display(source.publication_id)} · Selected record: ${display(source.selected_record_id)}`,
          "note",
        ),
      );
      table(
        parent,
        "Revenue comparison (EUR)",
        ["source", "revenue_eur"],
        [
          { source: "Published", revenue_eur: source.published_revenue_eur },
          { source: "API", revenue_eur: source.api_revenue_eur },
          { source: "Original", revenue_eur: source.original_revenue_eur },
        ],
      );
      const timeline = node("ol", undefined, "timeline");
      (Array.isArray(report.findings) ? report.findings : []).forEach(
        (finding) => {
          const item = node("li");
          badge(item, finding.status, finding.status);
          item.append(node("h3", finding.code), node("p", finding.detail));
          inspect(item, "Finding details", finding);
          timeline.append(item);
        },
      );
      parent.append(node("h3", "Finding timeline"));
      parent.append(
        timeline.childElementCount
          ? timeline
          : node("p", "No findings returned.", "note"),
      );
      const versions = source.versions || {};
      parent.append(
        node(
          "p",
          `Versions: ${Array.isArray(versions.items) ? versions.items.length : 0} shown / ${display(versions.total)} total${versions.truncated ? " · Truncated" : ""}`,
          "note",
        ),
      );
      table(parent, "Record versions", null, versions.items);
      if (source.limitations != null)
        inspect(parent, "Limitations", source.limitations, true);
      sql(parent, source.sql);
    } else {
      parent.append(
        node(
          "p",
          `Publication: ${display(report.publication_id)}${report.truncated ? " · Results truncated" : ""}`,
          "note",
        ),
      );
      table(parent, "Query results", report.columns, report.rows);
      if (report.metric_definition != null)
        inspect(parent, "Metric definition", report.metric_definition);
      if (report.coverage != null) inspect(parent, "Coverage", report.coverage);
      if (report.sql != null)
        sql(parent, [{ sql: report.sql, parameters: report.parameters }]);
    }
    inspect(parent, "Complete evidence JSON", report);
  }
  function renderTurn(turn) {
    const article = node("article", undefined, "turn");
    article.append(node("h3", turn.question, "question"));
    if (turn.error) {
      article.append(node("p", turn.error, "turn-error"));
      article.append(
        node(
          "p",
          "The outcome is uncertain. Recover the original request without starting a new execution. Stopping a browser request does not guarantee server cancellation.",
          "note",
        ),
      );
      const actions = node("div", undefined, "actions recovery-actions");
      const recover = node("button", "Recover request", "recover-request");
      recover.type = "button";
      recover.disabled = sessions[active].busy || !available();
      const generation = authGeneration;
      recover.addEventListener("click", () => {
        if (generation !== authGeneration || syncCredential()) return;
        const agent = turn.payload.agent;
        const session = sessions[agent];
        if (
          active !== agent ||
          !session.turns.includes(turn) ||
          session.busy ||
          !available()
        )
          return;
        executeTurn(agent, session, turn, true);
      });
      actions.append(
        recover,
        node("span", `Request: ${turn.payload.request_id}`, "note request-id"),
      );
      article.append(actions);
      inspect(article, "Original request payload", turn.payload);
      return article;
    }
    if (!turn.result) {
      article.append(
        node(
          "p",
          turn.recovering
            ? "Recovering original request…"
            : "Waiting for agent…",
          "note",
        ),
      );
      return article;
    }
    const result = turn.result;
    const trace = result.trace || {};
    const badges = node("div", undefined, "badges");
    badge(badges, result.status, result.status);
    badge(badges, trace.mode || "Mode unavailable", trace.mode);
    badge(
      badges,
      trace.mode === "direct"
        ? "No model call"
        : `Provider / model: ${trace.provider ? `${trace.provider} / ` : ""}${display(trace.model)}`,
    );
    badge(
      badges,
      typeof trace.elapsed_ms === "number"
        ? `${trace.elapsed_ms.toLocaleString()} ms`
        : "Latency unavailable",
    );
    badge(
      badges,
      typeof trace.cost_usd === "string" &&
        /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(trace.cost_usd)
        ? `$${trace.cost_usd} USD`
        : "Cost unavailable",
    );
    article.append(badges, node("p", result.answer, "answer"));
    if (result.status === "clarification")
      article.append(
        node(
          "p",
          "Reply with the requested details to continue this conversation.",
          "clarification-note",
        ),
      );
    if (result.plan != null) inspect(article, "Plan", result.plan);
    evidence(article, result);
    inspect(article, "Run trace and request IDs", {
      request_id: result.request_id,
      conversation_id: result.conversation_id,
      ...trace,
    });
    return article;
  }
  function available() {
    return service && service.profiles.includes(active);
  }
  function renderControls() {
    const session = sessions[active];
    const uncertain = session.turns.some((turn) => turn.error);
    $("send").disabled = session.busy || !available();
    $("send").textContent = uncertain ? "Start new execution" : "Ask agent";
    $("execution-note").hidden = !uncertain;
    document.querySelectorAll(".recover-request").forEach((button) => {
      button.disabled = session.busy || !available();
    });
    $("new-conversation").disabled = session.busy;
    $("question").disabled = session.busy;
    $("build-question").disabled = session.busy || !available();
    $("transcript").setAttribute("aria-busy", String(session.busy));
    $("request-status").textContent = session.busy
      ? "Running… switching agents keeps this request active."
      : !available()
        ? "Agent unavailable. Check service status or deployment access."
        : uncertain
          ? "Outcome uncertain. Recover a prior request or start a new execution."
          : session.turns.at(-1)?.result?.status === "clarification"
            ? "Awaiting your clarification."
            : active === "investigation"
              ? "Ready. Run an exact investigation or ask a question."
              : "Ready. Questions run only when you press Ask agent.";
    const error = session.error || statusError;
    $("error").textContent = error;
    $("error").hidden = !error;
    $("conversation-id").textContent = session.conversation
      ? `Conversation: ${session.conversation}`
      : "New conversation";
    $("examples").replaceChildren();
    const examples =
      service?.examples.filter((example) => example.agent === active) || [];
    examples.forEach((example) => {
      const button = node("button", example.question);
      button.type = "button";
      button.disabled = session.busy;
      button.addEventListener("click", () => {
        session.draft = example.question;
        $("question").value = session.draft;
        $("question").focus();
      });
      $("examples").append(button);
    });
    if (!examples.length)
      $("examples").append(node("p", "No examples available.", "note"));
  }
  function render() {
    $("conversation-title").textContent = names[active];
    $("agent-description").textContent =
      active === "business"
        ? "Ask business questions and inspect the supporting data."
        : "Investigate a firm and month through findings, record versions, and revenue comparisons.";
    $("investigation-form").hidden = active !== "investigation";
    $("question").value = sessions[active].draft;
    const turns = sessions[active].turns;
    $("transcript").replaceChildren(
      ...(turns.length
        ? turns.map(renderTurn)
        : [
            node(
              "p",
              "Ask a question to see the answer and its evidence here.",
              "empty",
            ),
          ]),
    );
    renderControls();
  }
  function headers() {
    const result = { "X-Pulse-Agent": "assistant-v1" };
    const token = credential.trim();
    if (token) result.Authorization = `Bearer ${token}`;
    return result;
  }
  // Invalidate before aborting: fetch or JSON parsing may still settle afterwards.
  function resetAuthentication() {
    authGeneration += 1;
    controllers.forEach((controller) => controller.abort());
    controllers.clear();
    sessions.business = fresh();
    sessions.investigation = fresh();
    service = null;
    authenticatedIdentity = undefined;
    statusBusy = false;
    statusError = "";
    $("firm-id").value = "";
    $("month").value = "";
    $("refresh-status").disabled = false;
    $("service-status").textContent = "Access changed · refresh status";
    $("service-status").dataset.state = "unavailable";
    $("service-identity").textContent = "Identity not verified";
    render();
  }
  function syncCredential() {
    if (credential === $("access-token").value) return false;
    credential = $("access-token").value;
    resetAuthentication();
    return true;
  }
  async function request(path, options, generation) {
    const controller = new AbortController();
    controllers.add(controller);
    const timer = setTimeout(() => {
      if (generation === authGeneration) controller.abort();
    }, 90000);
    const checkGeneration = () => {
      if (generation !== authGeneration) throw new Error("Access changed.");
    };
    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
        cache: "no-store",
      });
      checkGeneration();
      if (!response.ok) {
        if (response.status === 401 || response.status === 403)
          throw new Error(
            `Access denied (HTTP ${response.status}). Check the deployment bearer token.`,
          );
        throw new Error(`Service request failed (HTTP ${response.status}).`);
      }
      const data = await response.json();
      checkGeneration();
      return data;
    } catch (error) {
      if (error.name === "AbortError")
        throw new Error(
          "Request timed out. The server may still have processed it; no automatic retry was made.",
        );
      throw error;
    } finally {
      clearTimeout(timer);
      controllers.delete(controller);
    }
  }
  async function loadStatus() {
    syncCredential();
    if (statusBusy) return;
    let generation = authGeneration;
    statusBusy = true;
    $("refresh-status").disabled = true;
    $("service-status").textContent = "Checking service…";
    $("service-status").dataset.state = "";
    try {
      const data = await request(
        "/api/v1/agents/status",
        {
          headers: headers(),
        },
        generation,
      );
      if (generation !== authGeneration) return;
      if (
        !data ||
        !["live", "demo"].includes(data.mode) ||
        !Array.isArray(data.profiles) ||
        !Array.isArray(data.examples)
      )
        throw new Error("Invalid service status response.");
      const identity = JSON.stringify([
        data.authentication ?? null,
        data.identity ?? null,
      ]);
      if (
        authenticatedIdentity !== undefined &&
        identity !== authenticatedIdentity
      ) {
        resetAuthentication();
        generation = authGeneration;
      }
      authenticatedIdentity = identity;
      service = {
        ...data,
        examples: data.examples.filter(
          (item) => item && typeof item.question === "string",
        ),
      };
      statusError = "";
      $("service-status").textContent = `${data.mode} · ${display(data.model)}`;
      $("service-status").dataset.state = data.mode;
      $("service-identity").textContent =
        `Identity: ${display(data.identity)} · Authentication: ${display(data.authentication)}`;
    } catch (error) {
      if (generation !== authGeneration) return;
      service = null;
      statusError = error.message || "Unable to reach the agent service.";
      $("service-status").textContent = "Service unavailable";
      $("service-status").dataset.state = "error";
    } finally {
      if (generation === authGeneration) {
        statusBusy = false;
        $("refresh-status").disabled = false;
        renderControls();
      }
    }
  }
  async function executeTurn(agent, session, turn, recovery = false) {
    const generation = authGeneration;
    const current = () =>
      generation === authGeneration && sessions[agent] === session;
    session.busy = true;
    session.error = "";
    turn.error = "";
    turn.recovering = recovery;
    render();
    try {
      const result = await request(
        turn.endpoint || "/api/v1/agents/ask",
        {
          method: "POST",
          headers: { ...headers(), "Content-Type": "application/json" },
          // Keep the exact bytes, including the original null conversation ID.
          body: turn.serializedPayload,
        },
        generation,
      );
      if (!current()) return;
      if (
        !result ||
        result.request_id !== turn.payload.request_id ||
        result.agent !== agent ||
        !uuid.test(result.conversation_id) ||
        !["answered", "clarification", "unsupported", "error"].includes(
          result.status,
        ) ||
        typeof result.answer !== "string"
      )
        throw new Error("Invalid agent response. No automatic retry was made.");
      // Recovering an earlier turn must not replace a newer conversation.
      if (session.turns.at(-1) === turn)
        session.conversation = result.conversation_id;
      turn.result = result;
      if (
        result.status !== "error" &&
        !turn.endpoint &&
        (!recovery || session.draft === turn.question)
      )
        session.draft = "";
    } catch (error) {
      if (!current()) return;
      turn.error = error.message || "Unable to reach the agent service.";
      session.error = turn.error;
    } finally {
      if (current()) {
        session.busy = false;
        if (active === agent) render();
      }
    }
  }
  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (syncCredential()) return;
    const agent = active;
    const session = sessions[agent];
    if (session.busy || !available()) return;
    const question = $("question").value.trim();
    if (!question) {
      session.error = "Enter a question first.";
      renderControls();
      return;
    }
    const payload = Object.freeze({
      request_id: crypto.randomUUID(),
      conversation_id: session.conversation,
      agent,
      question,
    });
    session.draft = question;
    const turn = {
      question,
      payload,
      serializedPayload: JSON.stringify(payload),
    };
    session.turns.push(turn);
    executeTurn(agent, session, turn);
  });
  $("question").addEventListener("input", () => {
    sessions[active].draft = $("question").value;
  });
  $("agent-picker").addEventListener("change", (event) => {
    if (!Object.hasOwn(sessions, event.target.value)) return;
    sessions[active].draft = $("question").value;
    active = event.target.value;
    render();
  });
  $("new-conversation").addEventListener("click", () => {
    if (sessions[active].busy) return;
    sessions[active] = fresh();
    render();
    $("question").focus();
  });
  $("investigation-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (syncCredential()) return;
    if (active !== "investigation" || sessions[active].busy || !available())
      return;
    const firm = $("firm-id").value.trim();
    const month = $("month").value;
    if (
      !/^[0-9]{1,10}$/.test(firm) ||
      Number(firm) < 1 ||
      Number(firm) > 2147483647 ||
      !/^\d{4}-(0[1-9]|1[0-2])$/.test(month) ||
      month.startsWith("0000")
    ) {
      sessions[active].error =
        "Enter a numeric firm ID from 1 to 2147483647 and a valid YYYY-MM month.";
      renderControls();
      return;
    }
    const session = sessions[active];
    const payload = Object.freeze({
      request_id: crypto.randomUUID(),
      conversation_id: session.conversation,
      agent: "investigation",
      firm_id: Number(firm),
      month,
    });
    const turn = {
      question: `Investigate total billed revenue for firm ID ${payload.firm_id} in ${month}.`,
      endpoint: "/api/v1/agents/investigate",
      payload,
      serializedPayload: JSON.stringify(payload),
    };
    session.turns.push(turn);
    executeTurn("investigation", session, turn);
  });
  $("refresh-status").addEventListener("click", loadStatus);
  $("access-token").addEventListener("input", syncCredential);
  $("access-token").addEventListener("change", syncCredential);
  $("clear-token").addEventListener("click", () => {
    $("access-token").value = "";
    syncCredential();
  });
  render();
  loadStatus();
})();
