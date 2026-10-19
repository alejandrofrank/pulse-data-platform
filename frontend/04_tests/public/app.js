(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const labels = {
    passed: "Passed",
    failed: "Failed",
    skipped: "Skipped",
    errors: "Errors",
    total: "Total",
  };
  const finished = (suite) =>
    ["passed", "failed", "error"].includes(suite.status);
  const title = (text) => text.charAt(0).toUpperCase() + text.slice(1);
  const date = (value) =>
    value
      ? new Date(value)
          .toISOString()
          .replace("T", " ")
          .replace(/\.\d{3}Z$/, " UTC")
      : "—";
  const duration = (value) => (value === null ? "—" : `${value.toFixed(3)} s`);
  let report = null,
    cases = [],
    page = 0,
    generation = 0,
    active = null,
    fingerprint = "";

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function badge(status, text = title(status)) {
    const element = node("span", text, "badge");
    element.dataset.state = status;
    return element;
  }
  function field(label, value) {
    const element = node("dl");
    element.append(node("dt", label), node("dd", value));
    return element;
  }
  function counts(container, values) {
    container.replaceChildren(
      ...Object.entries(labels).map(([key, label]) =>
        field(label, values ? String(values[key]) : "—"),
      ),
    );
  }
  function validate(value) {
    const validDate = (v) =>
      typeof v === "string" && Number.isFinite(Date.parse(v));
    const nullableDate = (v) => v === null || validDate(v);
    const number = (v) => typeof v === "number" && Number.isFinite(v) && v >= 0;
    const nullableNumber = (v) => v === null || number(v);
    const text = (v) => typeof v === "string";
    const count = (v) => Number.isSafeInteger(v) && v >= 0;
    const valid =
      value &&
      value.schema_version === 1 &&
      text(value.run_id) &&
      ["running", "passed", "failed", "interrupted"].includes(value.status) &&
      validDate(value.started_at) &&
      nullableDate(value.finished_at) &&
      text(value.commit) &&
      typeof value.dirty === "boolean" &&
      ["local", "ci"].includes(value.source) &&
      (value.ci_url === null || text(value.ci_url)) &&
      (value.updated_at === undefined || validDate(value.updated_at)) &&
      Array.isArray(value.suites) &&
      value.suites.every(
        (suite) =>
          suite &&
          ["backend", "frontend"].includes(suite.id) &&
          text(suite.label) &&
          ["pending", "running", "passed", "failed", "error"].includes(
            suite.status,
          ) &&
          nullableDate(suite.started_at) &&
          nullableDate(suite.finished_at) &&
          nullableNumber(suite.duration_seconds) &&
          (suite.exit_code === null || Number.isInteger(suite.exit_code)) &&
          (suite.error === null || text(suite.error)) &&
          suite.counts &&
          Object.keys(labels).every((key) => count(suite.counts[key])) &&
          Array.isArray(suite.cases) &&
          suite.cases.every(
            (item) =>
              item &&
              ["name", "module", "area", "message"].every((key) =>
                text(item[key]),
              ) &&
              ["passed", "failed", "skipped", "error"].includes(item.status) &&
              number(item.duration_seconds),
          ),
      ) &&
      new Set(value.suites.map((suite) => suite.id)).size ===
        value.suites.length;
    if (!valid)
      throw new Error(
        "Invalid or unsupported test report (expected schema version 1).",
      );
    return value;
  }

  function renderHealth() {
    const stale =
      report.status === "running" &&
      report.updated_at &&
      Date.now() - Date.parse(report.updated_at) > 90000;
    const noResults = report.status === "passed" && !cases.length;
    $("run-status").textContent = stale
      ? "Runner not responding"
      : noResults
        ? "No results"
        : title(report.status);
    $("run-status").dataset.state = stale
      ? "error"
      : noResults
        ? "empty"
        : report.status;
    $("heartbeat-warning").hidden = !stale;
    $("heartbeat-warning").textContent = stale
      ? `Runner not responding. Last update: ${date(report.updated_at)} (over 90 seconds ago). Completed suite results remain below.`
      : "";
  }

  function renderReport(next) {
    const changed = JSON.stringify(next);
    const newRun = report?.run_id !== next.run_id;
    report = next;
    $("error").hidden = true;
    $("error").textContent = "";
    $("report-message").hidden = true;
    if (fingerprint !== changed) {
      fingerprint = changed;
      if (newRun) page = 0;
      cases = next.suites.filter(finished).flatMap((suite) =>
        suite.cases.map((item) => ({
          ...item,
          suite: suite.id,
          suiteLabel: suite.label,
        })),
      );
      const metadata = $("run-metadata");
      $("run-details").hidden = false;
      metadata.hidden = false;
      metadata.replaceChildren(
        field("Run", next.run_id),
        field("Source", next.source === "ci" ? "CI" : "Local"),
        field("Source commit", next.commit || "Unavailable"),
        field(
          "Working tree at start",
          next.dirty ? "Dirty · uncommitted changes" : "Clean",
        ),
        field("Started", date(next.started_at)),
        field("Finished", date(next.finished_at)),
        field(
          "Runner last update",
          next.updated_at ? date(next.updated_at) : "Not reported",
        ),
      );
      const total = Object.fromEntries(
        Object.keys(labels).map((key) => [
          key,
          next.suites
            .filter(finished)
            .reduce((sum, suite) => sum + suite.counts[key], 0),
        ]),
      );
      counts($("totals"), total);
      $("completion").textContent =
        `${next.suites.filter(finished).length}/2 suites completed · totals from completed suites only`;
      $("suites").replaceChildren(
        ...["backend", "frontend"].map((id) => {
          const suite = next.suites.find((entry) => entry.id === id);
          const card = node("section", undefined, "panel");
          card.id = `suite-${id}`;
          card.setAttribute("aria-label", `${title(id)} suite`);
          const heading = node("div", undefined, "heading");
          heading.append(
            node("h2", title(id)),
            badge(suite?.status || "pending"),
          );
          const numbers = node("div", undefined, "counts");
          counts(numbers, suite && finished(suite) ? suite.counts : null);
          card.append(heading, numbers);
          if (suite) {
            const meta = node("div", undefined, "metadata");
            meta.append(
              field("Started", date(suite.started_at)),
              field("Finished", date(suite.finished_at)),
              field("Duration", duration(suite.duration_seconds)),
              field(
                "Exit code",
                suite.exit_code === null ? "—" : String(suite.exit_code),
              ),
            );
            card.append(meta);
            if (suite.error)
              card.append(node("pre", suite.error, "suite-error"));
          }
          if (!suite || !finished(suite))
            card.append(
              node(
                "p",
                suite?.status === "running"
                  ? "Running · individual results available when this suite finishes."
                  : "Pending · no finalized results yet.",
                "note",
              ),
            );
          return card;
        }),
      );
      const areas = [...new Set(cases.map((item) => item.area))].sort();
      const selectedArea = $("area-filter").value;
      const options = [
        new Option("All areas", ""),
        ...areas.map((area) => new Option(area, area)),
      ];
      $("area-filter").replaceChildren(...options);
      $("area-filter").value = areas.includes(selectedArea) ? selectedArea : "";
      $("area-breakdown").hidden = !areas.length;
      $("area-counts").replaceChildren(
        ...areas.map((area) => {
          const items = cases.filter((item) => item.area === area);
          const amounts = (status) =>
            items.filter((item) => item.status === status).length;
          const line = node("p", area);
          line.append(
            node(
              "span",
              `${amounts("passed")}/${items.length} passed · ${amounts("failed")} failed · ${amounts("skipped")} skipped · ${amounts("error")} errors`,
            ),
          );
          return line;
        }),
      );
      renderCases();
    }
    renderHealth();
  }

  function renderCases() {
    const query = $("search").value.trim().toLowerCase();
    const matches = cases.filter(
      (item) =>
        (!$("suite-filter").value || item.suite === $("suite-filter").value) &&
        (!$("status-filter").value ||
          item.status === $("status-filter").value) &&
        (!$("area-filter").value || item.area === $("area-filter").value) &&
        (!query ||
          [item.name, item.module, item.area, item.message].some((text) =>
            text.toLowerCase().includes(query),
          )),
    );
    page = Math.min(page, Math.max(0, Math.ceil(matches.length / 100) - 1));
    const start = page * 100;
    const open = new Set(
      [...$("cases").querySelectorAll("details[open]")].map(
        (detail) => detail.dataset.key,
      ),
    );
    $("cases").replaceChildren(
      ...matches.slice(start, start + 100).map((item) => {
        const row = node("tr");
        const name = node("td");
        name.append(
          node("div", item.name, "test-name"),
          node("div", item.module, "module"),
        );
        if (item.message || ["failed", "error"].includes(item.status)) {
          const details = node("details");
          details.dataset.key = JSON.stringify([
            item.suite,
            item.module,
            item.name,
          ]);
          details.open = open.has(details.dataset.key);
          details.append(
            node(
              "summary",
              ["failed", "error"].includes(item.status)
                ? "Failure details"
                : "Case details",
            ),
            node(
              "pre",
              item.message || "No failure details were provided by the runner.",
            ),
          );
          name.append(details);
        }
        const status = node("td");
        status.append(badge(item.status));
        row.append(
          name,
          node("td", item.suiteLabel),
          node("td", item.area),
          status,
          node("td", duration(item.duration_seconds), "duration"),
        );
        return row;
      }),
    );
    $("case-count").textContent = `${cases.length} finalized cases`;
    $("table-message").hidden = matches.length > 0;
    $("table-message").textContent = cases.length
      ? "No results match these filters."
      : report?.status === "running"
        ? "No finalized cases yet. Waiting for a suite to finish."
        : "No individual test cases were reported.";
    $("page-info").textContent = matches.length
      ? `${start + 1}–${Math.min(start + 100, matches.length)} of ${matches.length} cases`
      : "0 cases";
    $("previous").disabled = page === 0;
    $("next").disabled = start + 100 >= matches.length;
  }

  function unavailable(message, isError) {
    report = null;
    fingerprint = "";
    cases = [];
    page = 0;
    for (const id of ["run-metadata", "totals", "suites", "area-counts"])
      $(id).replaceChildren();
    $("run-metadata").hidden = true;
    $("run-details").hidden = true;
    $("area-breakdown").hidden = true;
    $("heartbeat-warning").hidden = true;
    $("heartbeat-warning").textContent = "";
    $("completion").textContent = "";
    $("run-status").textContent = isError
      ? "Report unavailable"
      : "No runs yet";
    $("run-status").dataset.state = isError ? "error" : "empty";
    $("error").hidden = !isError;
    $("error").textContent = isError ? message : "";
    $("report-message").hidden = isError;
    $("report-message").textContent = isError ? "" : message;
    renderCases();
    $("table-message").textContent = isError
      ? "Results unavailable. Refresh to retry; previous results have been cleared."
      : "No runs yet. Run both suites with the terminal command below.";
  }

  async function refresh(automatic = false) {
    if (automatic && active) return;
    const current = ++generation;
    if (active) {
      active.controller.abort();
      clearTimeout(active.timeout);
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => {
      if (current !== generation) return;
      generation++;
      active = null;
      controller.abort();
      unavailable("Report request timed out. Refresh to retry.", true);
      $("refresh-status").textContent =
        "Refresh failed · retrying every 5 seconds";
      $("case-panel").setAttribute("aria-busy", "false");
    }, 15000);
    active = { controller, timeout };
    $("refresh-status").textContent = "Refreshing report…";
    $("case-panel").setAttribute("aria-busy", "true");
    try {
      const response = await fetch("report.json", {
        cache: "no-store",
        signal: controller.signal,
      });
      if (current !== generation) return;
      if (response.status === 404 || response.status === 204) {
        unavailable(
          "No runs yet. Generate a report with the terminal command below.",
          false,
        );
      } else {
        if (!response.ok)
          throw new Error(`Report request failed (HTTP ${response.status}).`);
        const value = await response.json();
        if (current !== generation) return;
        renderReport(validate(value));
      }
      $("refresh-status").textContent =
        `Checked ${new Date().toLocaleTimeString()} · every 5 seconds`;
    } catch (error) {
      if (current !== generation) return;
      unavailable(error.message || "Unable to read the test report.", true);
      $("refresh-status").textContent =
        "Refresh failed · retrying every 5 seconds";
    } finally {
      clearTimeout(timeout);
      if (current === generation) {
        active = null;
        $("case-panel").setAttribute("aria-busy", "false");
      }
    }
  }

  function filter() {
    page = 0;
    if (report) renderCases();
  }
  $("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    filter();
  });
  $("search").addEventListener("input", filter);
  for (const id of ["suite-filter", "status-filter", "area-filter"])
    $(id).addEventListener("change", filter);
  $("clear-filters").addEventListener("click", () => {
    $("filters").reset();
    filter();
  });
  $("previous").addEventListener("click", () => {
    page--;
    renderCases();
  });
  $("next").addEventListener("click", () => {
    page++;
    renderCases();
  });
  $("refresh").addEventListener("click", () => refresh());
  const interval = setInterval(() => {
    if (report) renderHealth();
    refresh(true);
  }, 5000);
  window.addEventListener("pagehide", () => {
    clearInterval(interval);
    generation++;
    if (active) {
      clearTimeout(active.timeout);
      active.controller.abort();
    }
  });
  refresh();
})();
