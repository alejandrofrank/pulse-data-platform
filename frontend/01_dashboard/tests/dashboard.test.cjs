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
