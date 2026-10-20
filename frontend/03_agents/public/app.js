(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const names = {
    business: "Business assistant",
    investigation: "Data investigator",
  };
  const localSessions = new Set();
  const localRequests = new Map();
  const fresh = (agent) => {
    const session = {
      localID: crypto.randomUUID(),
      agent,
      conversation: null,
      turns: [],
      draft: "",
      busy: false,
      error: "",
      loading: false,
      loadError: "",
      historyTotal: 0,
      historyOffset: 0,
      title: "",
    };
    localSessions.add(session);
    return session;
  };
  const sessions = {
    business: fresh("business"),
    investigation: fresh("investigation"),
  };
  const savedSessions = new Map();
  let historyItems = [];
  let historyTotal = 0;
  let historyOffset = 0;
  let historyBusy = false;
  let historyError = "";
  let historyVersion = 0;
  let selectionVersion = 0;
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
    details.open = false;
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
    const user = node("div", undefined, "user-message");
    user.append(
      node("div", "You", "message-label"),
      node("p", turn.question, "question"),
    );
    const reply = node("div", undefined, "assistant-message");
    const label = node("div", names[active], "message-label");
    if (turn.createdAt) {
      const time = node("time", dateLabel(turn.createdAt));
      time.dateTime = turn.createdAt;
      label.append(time);
    }
    reply.append(label);
    article.append(user, reply);
    if (turn.error) {
      reply.append(node("p", turn.error, "turn-error"));
      reply.append(
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
      reply.append(actions);
      inspect(reply, "Original request payload", turn.payload);
      return article;
    }
    if (!turn.result) {
      reply.append(
        node(
          "p",
          turn.remote
            ? turn.status === "running"
              ? "This request was still running when the conversation was loaded."
              : "No saved answer is available for this request."
            : turn.recovering
              ? "Recovering original request…"
              : "Waiting for agent…",
          "note",
        ),
      );
      if (turn.remote) {
        const refresh = node("button", "Refresh saved messages");
        refresh.type = "button";
        refresh.addEventListener("click", () =>
          openConversation({
            id: sessions[active].conversation,
            agent: active,
          }),
        );
        reply.append(refresh);
      }
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
    reply.append(node("p", result.answer, "answer"));
    if (result.status === "clarification")
      reply.append(
        node(
          "p",
          "Reply with the requested details to continue this conversation.",
          "clarification-note",
        ),
      );
    if (result.evidence) {
      const proof = node("details", undefined, "evidence-panel");
      proof.append(
        node(
          "summary",
          result.agent === "investigation"
            ? "View investigation & source evidence"
            : "View data & sources",
        ),
      );
      evidence(proof, result);
      reply.append(proof);
    } else {
      reply.append(
        node("p", "No evidence returned for this response.", "answer-status"),
      );
    }
    const execution = node("details", undefined, "execution-details");
    execution.append(node("summary", "Execution details"), badges);
    if (result.plan != null) inspect(execution, "Plan", result.plan);
    inspect(execution, "Run trace and request IDs", {
      request_id: result.request_id,
      conversation_id: result.conversation_id,
      ...trace,
    });
    reply.append(execution);
    return article;
  }
  function available() {
    return service && service.profiles.includes(active);
  }
  function renderControls() {
    const session = sessions[active];
    const uncertain = session.turns.some((turn) => turn.error);
    const pending = session.turns.some(
      (turn) => !turn.result && !turn.error && turn.status === "running",
    );
    const blocked =
      session.busy ||
      session.loading ||
      !!session.loadError ||
      pending ||
      !available();
    $("send").disabled = blocked;
    $("send").textContent = uncertain ? "Start new execution" : "Send";
    $("execution-note").hidden = !uncertain;
    document.querySelectorAll(".recover-request").forEach((button) => {
      button.disabled = blocked;
    });
    $("new-conversation").disabled = session.busy;
    $("question").disabled =
      session.busy || session.loading || !!session.loadError;
    $("build-question").disabled = blocked;
    $("transcript").setAttribute(
      "aria-busy",
      String(session.busy || session.loading),
    );
    $("request-status").textContent = session.loading
      ? "Loading saved messages…"
      : session.loadError
        ? "Reopen the conversation to try loading it again."
        : session.busy
          ? "Working… you can browse other conversations."
          : pending
            ? "A saved request is still running. Refresh its messages before continuing."
            : !available()
              ? "Agent unavailable. Check service status or deployment access."
              : uncertain
                ? "Outcome uncertain. Recover a prior request or start a new execution."
                : session.turns.at(-1)?.result?.status === "clarification"
                  ? "Awaiting your clarification."
                  : active === "investigation"
                    ? "Ready. Run an exact investigation or ask a question."
                    : "Answers use your published data. Open the evidence to verify a result.";
    const error = session.error || statusError;
    $("error").textContent = error;
    $("error").hidden = !error;
    $("conversation-id").textContent = session.conversation
      ? `Conversation: ${session.conversation}`
      : "New conversation";
    $("starter-section").hidden =
      !!session.turns.length || session.loading || !!session.loadError;
    document
      .querySelector(".chat")
      .classList.toggle("is-empty", !$("starter-section").hidden);
    $("earlier-messages").hidden =
      session.historyOffset >= session.historyTotal;
    $("earlier-messages").disabled = session.loading || session.busy;
    $("conversation-error").textContent = session.loadError;
    $("conversation-error").hidden = !session.loadError;
    $("examples").replaceChildren();
    const examples =
      service?.examples.filter((example) => example.agent === active) || [];
    if (service?.mode === "live" && active === "business") {
      [
        "Which 3 firms billed the most in March 2026?",
        "What was the network’s billed revenue in 2025?",
      ].forEach((question) => {
        if (!examples.some((example) => example.question === question))
          examples.push({ agent: active, question });
      });
    }
    examples.forEach((example) => {
      const button = node("button", example.question);
      button.type = "button";
      button.disabled = session.busy;
      button.addEventListener("click", () => {
        session.draft = example.question;
        $("question").value = session.draft;
        resizeComposer();
        $("question").focus();
      });
      $("examples").append(button);
    });
    if (!examples.length)
      $("examples").append(node("p", "No examples available.", "note"));
  }
  function render() {
    $("conversation-title").textContent =
      sessions[active].title || names[active];
    $("agent-description").textContent =
      active === "business"
        ? "Ask business questions and inspect the supporting data."
        : "Investigate a firm and month through findings, record versions, and revenue comparisons.";
    $("composer-description").textContent =
      active === "business"
        ? "Explore your published business metrics"
        : "Trace a number from source to reporting";
    $("investigation-form").hidden = active !== "investigation";
    $("exact-controls").hidden = active !== "investigation";
    document.querySelectorAll('input[name="agent"]').forEach((radio) => {
      radio.checked = radio.value === active;
    });
    $("question").value = sessions[active].draft;
    resizeComposer();
    const turns = sessions[active].turns;
    $("transcript").replaceChildren(
      ...(sessions[active].loadError ? [] : turns.map(renderTurn)),
    );
    renderControls();
    renderHistory();
  }
  function dateLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "Saved chat"
      : new Intl.DateTimeFormat("en-GB", {
          day: "numeric",
          month: "short",
          year: "numeric",
        }).format(date);
  }
  function resizeComposer() {
    $("question").style.height = "auto";
    $("question").style.height =
      `${Math.max(36, Math.min(160, $("question").scrollHeight))}px`;
  }
  function scrollToLatest() {
    $("chat-scroll").scrollTop = $("chat-scroll").scrollHeight;
  }
  function closeHistory() {
    document.body.classList.remove("history-open");
    $("toggle-history").setAttribute("aria-expanded", "false");
    document.querySelector(".chat").inert = false;
  }
  function selectLocal(session) {
    sessions[active].draft = $("question").value;
    active = session.agent;
    sessions[active] = session;
    selectionVersion += 1;
    closeHistory();
    render();
    scrollToLatest();
    $("question").focus();
  }
  function cacheSession(session) {
    const cached = savedSessions.get(session.conversation);
    if (!cached || cached === session) {
      savedSessions.set(session.conversation, session);
      return session;
    }
    // History can discover a run before its POST response arrives. Keep the
    // restored chat's draft and merge the original turn/recovery identity into it.
    const turns = new Map(
      cached.turns.map((turn) => [
        turn.payload?.request_id || turn.requestID || turn.result?.request_id,
        turn,
      ]),
    );
    session.turns.forEach((turn) => {
      const id =
        turn.payload?.request_id || turn.requestID || turn.result?.request_id;
      const known = turns.get(id);
      // The history read may have recovered the result before the POST failed.
      // A transport failure cannot downgrade an already validated saved answer.
      if (known?.result && !turn.result) {
        turn.result = known.result;
        turn.error = "";
      }
      turns.set(id, turn);
    });
    cached.turns = [...turns.values()].sort(
      (a, b) => (Date.parse(a.createdAt) || 0) - (Date.parse(b.createdAt) || 0),
    );
    cached.title ||= session.title;
    if (sessions[session.agent] === session) sessions[session.agent] = cached;
    localSessions.delete(session);
    return cached;
  }
  function renderHistory() {
    const search = $("history-search").value.trim().toLowerCase();
    const items = historyItems.filter((item) =>
      `${item.title} ${names[item.agent]}`.toLowerCase().includes(search),
    );
    $("history-list").replaceChildren(
      ...[...localSessions]
        .filter(
          (session) =>
            !session.conversation &&
            (session.draft.trim() || session.turns.length),
        )
        .filter((session) =>
          `${session.draft} ${session.turns[0]?.question || ""} ${names[session.agent]}`
            .toLowerCase()
            .includes(search),
        )
        .map((session) => {
          const button = node(
            "button",
            undefined,
            "history-item local-session",
          );
          button.type = "button";
          button.dataset.localId = session.localID;
          button.setAttribute(
            "aria-current",
            String(sessions[active] === session),
          );
          button.append(
            node(
              "strong",
              session.turns[0]?.question || session.draft.slice(0, 100),
            ),
            node(
              "span",
              `${session.busy ? "Sending" : session.turns.some((turn) => turn.error) ? "Needs recovery" : "Unsent draft"} · ${names[session.agent]}`,
              "history-meta",
            ),
          );
          button.addEventListener("click", () => selectLocal(session));
          return button;
        }),
      ...items.map((item) => {
        const button = node("button", undefined, "history-item");
        button.type = "button";
        button.dataset.id = item.id;
        button.title = item.title;
        button.setAttribute(
          "aria-current",
          String(sessions[active].conversation === item.id),
        );
        button.append(
          node("strong", item.title || "Untitled conversation"),
          node(
            "span",
            `${names[item.agent]} · ${dateLabel(item.updated_at)}`,
            "history-meta",
          ),
        );
        button.addEventListener("click", () => openConversation(item));
        return button;
      }),
    );
    $("history-status").textContent =
      historyError ||
      (historyBusy
        ? "Loading conversations…"
        : !service
          ? "Verify access to load saved chats."
          : search
            ? `${items.length} matching loaded chats`
            : historyTotal
              ? `${historyItems.length} of ${historyTotal} saved conversations`
              : "Your conversations will appear here.");
    $("history-status").classList.toggle("history-error", !!historyError);
    $("history-list").setAttribute("aria-busy", String(historyBusy));
    $("refresh-history").disabled = historyBusy || !service;
    $("more-conversations").hidden = historyOffset >= historyTotal;
    $("more-conversations").disabled = historyBusy || !service;
  }
  async function loadHistory(more = false) {
    if (syncCredential() || !service || (more && historyBusy)) return;
    const generation = authGeneration;
    const version = ++historyVersion;
    const offset = more ? historyOffset : 0;
    historyBusy = true;
    historyError = "";
    renderHistory();
    try {
      const data = await request(
        `/api/v1/agents/conversations?limit=30&offset=${offset}`,
        { headers: headers() },
        generation,
      );
      if (generation !== authGeneration || version !== historyVersion) return;
      if (
        !Array.isArray(data.items) ||
        !Number.isInteger(data.total) ||
        data.total < 0 ||
        !data.items.every(
          (item) =>
            uuid.test(item.id) &&
            Object.hasOwn(names, item.agent) &&
            typeof item.title === "string",
        )
      )
        throw new Error("Invalid conversation history response.");
      const merged = new Map(
        (more ? historyItems : []).map((item) => [item.id, item]),
      );
      data.items.forEach((item) => merged.set(item.id, item));
      historyItems = [...merged.values()];
      historyTotal = data.total;
      historyOffset = offset + data.items.length;
    } catch (error) {
      if (generation !== authGeneration || version !== historyVersion) return;
      historyError = error.message || "Unable to load conversations.";
      historyItems = [];
      historyTotal = 0;
      historyOffset = 0;
    } finally {
      if (generation === authGeneration && version === historyVersion) {
        historyBusy = false;
        renderHistory();
      }
    }
  }
  function validResult(result, requestID, agent, conversationID) {
    return (
      result &&
      result.request_id === requestID &&
      result.agent === agent &&
      uuid.test(result.conversation_id) &&
      (!conversationID || result.conversation_id === conversationID) &&
      ["answered", "clarification", "unsupported", "error"].includes(
        result.status,
      ) &&
      typeof result.answer === "string"
    );
  }
  async function openConversation(item, earlier = false) {
    if (
      syncCredential() ||
      !service ||
      !uuid.test(item.id) ||
      !Object.hasOwn(names, item.agent)
    )
      return;
    sessions[active].draft = $("question").value;
    const generation = authGeneration;
    const version = ++selectionVersion;
    const session =
      savedSessions.get(item.id) ||
      Object.assign(fresh(item.agent), {
        conversation: item.id,
        title: item.title || "Saved conversation",
      });
    savedSessions.set(item.id, session);
    sessions[item.agent] = session;
    active = item.agent;
    closeHistory();
    if (session.busy) {
      render();
      scrollToLatest();
      return;
    }
    const offset = earlier ? session.historyOffset : 0;
    session.loadVersion = version;
    session.loading = true;
    session.loadError = "";
    render();
    try {
      const data = await request(
        `/api/v1/agents/conversations/${item.id}?limit=10&offset=${offset}`,
        { headers: headers() },
        generation,
      );
      if (generation !== authGeneration || version !== session.loadVersion)
        return;
      if (
        data.id !== item.id ||
        data.agent !== item.agent ||
        !Array.isArray(data.items) ||
        !Number.isInteger(data.total) ||
        data.total < 0 ||
        typeof data.title !== "string" ||
        !data.items.every(
          (run) =>
            uuid.test(run.request_id) &&
            typeof run.question === "string" &&
            ["running", "complete", "error"].includes(run.status) &&
            (run.result == null ||
              validResult(run.result, run.request_id, item.agent, item.id)),
        )
      )
        throw new Error("Invalid saved conversation response.");
      const ids = new Set(data.items.map((run) => run.request_id));
      for (const pending of localSessions) {
        if (
          !pending.conversation &&
          pending.agent === item.agent &&
          pending.turns.some((turn) => ids.has(turn.payload?.request_id))
        ) {
          const answeredDraft = data.items.some(
            (run) =>
              run.question === pending.draft &&
              run.result &&
              run.result.status !== "error",
          );
          if (!session.restored && !session.draft && !answeredDraft)
            session.draft = pending.draft;
          pending.conversation = item.id;
        }
      }
      session.restored = true;
      // Keep local request bytes for recovery; historical GETs never manufacture retries.
      const local = new Map(
        session.turns.map((turn) => [
          turn.payload?.request_id || turn.result?.request_id || turn.requestID,
          turn,
        ]),
      );
      const loaded = data.items.map((run) => ({
        ...(localRequests.get(run.request_id) || local.get(run.request_id)),
        question: run.question,
        requestID: run.request_id,
        createdAt: run.created_at,
        status: run.status,
        result: run.result,
        remote: !localRequests.has(run.request_id),
        error: run.result ? "" : localRequests.get(run.request_id)?.error || "",
      }));
      loaded.forEach((turn) => local.delete(turn.requestID));
      const preserved = [...local.values()].filter(
        (turn) => earlier || turn.payload,
      );
      session.turns = [...loaded, ...preserved].sort(
        (a, b) =>
          (Date.parse(a.createdAt) || 0) - (Date.parse(b.createdAt) || 0) ||
          String(a.requestID || a.payload?.request_id).localeCompare(
            String(b.requestID || b.payload?.request_id),
          ),
      );
      session.title = data.title;
      session.historyTotal = data.total;
      session.historyOffset = offset + data.items.length;
      session.error = "";
    } catch (error) {
      if (generation !== authGeneration || version !== session.loadVersion)
        return;
      session.loadError =
        error.message || "Unable to reopen this conversation.";
      // Hide stale evidence, but retain local request bytes across transient
      // failures. Authentication failures clear all caches in request().
    } finally {
      if (generation === authGeneration && version === session.loadVersion) {
        session.loading = false;
        if (sessions[active] === session) {
          render();
          if (!earlier) scrollToLatest();
          $("question").focus();
        }
      }
    }
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
    localSessions.clear();
    localRequests.clear();
    sessions.business = fresh("business");
    sessions.investigation = fresh("investigation");
    savedSessions.clear();
    historyItems = [];
    historyTotal = 0;
    historyOffset = 0;
    historyBusy = false;
    historyError = "";
    historyVersion += 1;
    selectionVersion += 1;
    $("history-search").value = "";
    closeHistory();
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
        if (response.status === 401 || response.status === 403) {
          const denied = new Error(
            `Access denied (HTTP ${response.status}). Check the deployment bearer token.`,
          );
          denied.accessDenied = true;
          throw denied;
        }
        throw new Error(`Service request failed (HTTP ${response.status}).`);
      }
      const data = await response.json();
      checkGeneration();
      return data;
    } catch (error) {
      if (error.accessDenied && generation === authGeneration) {
        resetAuthentication();
        statusError = error.message;
        renderControls();
        renderHistory();
      }
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
        data.access_scope ?? null,
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
      loadHistory();
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
        renderHistory();
      }
    }
  }
  async function executeTurn(agent, session, turn, recovery = false) {
    let visibleSession = session;
    localRequests.set(turn.payload.request_id, turn);
    const generation = authGeneration;
    const current = () => generation === authGeneration;
    session.busy = true;
    session.error = "";
    turn.error = "";
    turn.recovering = recovery;
    render();
    scrollToLatest();
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
      if (!validResult(result, turn.payload.request_id, agent))
        throw new Error("Invalid agent response. No automatic retry was made.");
      // Recovering an earlier turn must not replace a newer conversation.
      if (session.turns.at(-1) === turn)
        session.conversation = result.conversation_id;
      session.title ||= session.turns[0].question.slice(0, 100);
      turn.result = result;
      if (
        result.status !== "error" &&
        !turn.endpoint &&
        (!recovery || session.draft === turn.question)
      )
        session.draft = "";
      visibleSession = cacheSession(session);
    } catch (error) {
      if (!current()) return;
      turn.error = error.message || "Unable to reach the agent service.";
      session.error = turn.error;
    } finally {
      if (current()) {
        session.busy = false;
        if (session.conversation) visibleSession = cacheSession(session);
        if (active === agent && sessions[agent] === visibleSession) {
          render();
          scrollToLatest();
        }
        loadHistory();
      }
    }
  }
  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (syncCredential()) return;
    const agent = active;
    const session = sessions[agent];
    if ($("send").disabled) return;
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
      createdAt: new Date().toISOString(),
      payload,
      serializedPayload: JSON.stringify(payload),
    };
    session.turns.push(turn);
    localRequests.set(payload.request_id, turn);
    executeTurn(agent, session, turn);
  });
  $("question").addEventListener("input", () => {
    sessions[active].draft = $("question").value;
    resizeComposer();
    renderHistory();
  });
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!$("send").disabled) $("ask-form").requestSubmit();
    }
  });
  $("agent-picker").addEventListener("change", (event) => {
    if (!Object.hasOwn(sessions, event.target.value)) return;
    sessions[active].draft = $("question").value;
    active = event.target.value;
    selectionVersion += 1;
    render();
  });
  $("new-conversation").addEventListener("click", () => {
    if (sessions[active].busy) return;
    sessions[active] = fresh(active);
    selectionVersion += 1;
    closeHistory();
    render();
    $("question").focus();
  });
  $("investigation-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (syncCredential()) return;
    if (active !== "investigation" || $("build-question").disabled) return;
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
      createdAt: new Date().toISOString(),
      endpoint: "/api/v1/agents/investigate",
      payload,
      serializedPayload: JSON.stringify(payload),
    };
    session.turns.push(turn);
    localRequests.set(payload.request_id, turn);
    executeTurn("investigation", session, turn);
  });
  $("refresh-status").addEventListener("click", loadStatus);
  $("refresh-history").addEventListener("click", () => loadHistory());
  $("more-conversations").addEventListener("click", () => loadHistory(true));
  $("history-search").addEventListener("input", renderHistory);
  $("earlier-messages").addEventListener("click", () =>
    openConversation(
      { id: sessions[active].conversation, agent: active },
      true,
    ),
  );
  $("toggle-history").addEventListener("click", () => {
    const open = document.body.classList.toggle("history-open");
    $("toggle-history").setAttribute("aria-expanded", String(open));
    document.querySelector(".chat").inert = open && window.innerWidth <= 760;
    if (open) $("history-search").focus();
  });
  window.addEventListener("resize", () => {
    if (window.innerWidth > 760) closeHistory();
  });
  document.addEventListener("keydown", (event) => {
    if (
      event.key === "Escape" &&
      document.body.classList.contains("history-open")
    ) {
      closeHistory();
      $("toggle-history").focus();
    }
  });
  $("access-token").addEventListener("input", syncCredential);
  $("access-token").addEventListener("change", syncCredential);
  $("clear-token").addEventListener("click", () => {
    $("access-token").value = "";
    syncCredential();
  });
  render();
  loadStatus();
})();
