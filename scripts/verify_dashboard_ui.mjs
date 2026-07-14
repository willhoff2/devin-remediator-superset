// Drives the dashboard UI in headless Chrome via the DevTools protocol and
// checks the interactive behaviors the pytest suite can't reach: card
// click-filters, the ⓘ popovers, and the collapsible activity feed.
// Zero dependencies (raw CDP over Node's built-in WebSocket; Node >= 22).
//
// Usage: node scripts/verify_dashboard_ui.mjs [url]
//   - The dashboard must be serving (default http://localhost:8090/;
//     `make run` or `python -m src.main`).
//   - CHROME_BIN overrides the Chrome binary (default is the macOS path).
//   - SHOT_DIR is where the two verification screenshots land (default cwd).
// Exits 0 only if every check passes.
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";

const URL_UNDER_TEST = process.argv[2] ?? "http://localhost:8090/";
const CHROME = process.env.CHROME_BIN
  ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9223;
const OUT_DIR = process.env.SHOT_DIR ?? ".";

const chrome = spawn(
  CHROME,
  ["--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
   "--window-size=1440,900", "--hide-scrollbars", "about:blank"],
  { stdio: "ignore" },
);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let targets;
for (let i = 0; i < 40 && !targets; i++) {
  try {
    targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  } catch { await sleep(250); }
}
const page = targets.find((t) => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));

let msgId = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++msgId;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expression) => {
  const r = await send("Runtime.evaluate",
    { expression, returnByValue: true, awaitPromise: true });
  if (r.result.exceptionDetails) {
    throw new Error(r.result.exceptionDetails.exception?.description ?? "eval failed");
  }
  return r.result.result.value;
};
const shot = async (name) => {
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(`${OUT_DIR}/${name}.png`, Buffer.from(r.result.data, "base64"));
};

await send("Page.enable");
await send("Page.navigate", { url: URL_UNDER_TEST });
for (let i = 0; i < 20; i++) {
  if (await evaluate(`document.querySelectorAll("#rows tr").length`).catch(() => 0)) break;
  await sleep(300);
}

const results = [];
const check = (name, ok) => results.push({ name, result: ok ? "PASS" : "FAIL" });
const rowCount = () => evaluate(`document.querySelectorAll("#rows tr").length`);
const click = (sel) => evaluate(`document.querySelector('${sel}').click()`);

check("initial render has task rows", (await rowCount()) > 0);
const allRows = await rowCount();

await click('[data-filter="succeeded"]');
check("succeeded filter keeps succeeded rows", (await rowCount()) === allRows);
check("selected card is highlighted",
  await evaluate(`document.querySelector('[data-filter="succeeded"]').classList.contains("sel")`));

await click('[data-filter="active"]');
check("switching filter shows empty state for 0 active",
  (await evaluate(`document.querySelector("#rows td.empty")?.textContent`)) === "No active tasks");
await click('[data-filter="active"]');
check("clicking selected card clears filter", (await rowCount()) === allRows);
check("no card highlighted when filter is cleared",
  await evaluate(`document.querySelectorAll(".card.sel").length`) === 0);

await click('[data-filter="succeeded"]');
await click('[data-pop="costPop"]');
check("cost popover opens on click",
  await evaluate(`!document.getElementById("costPop").hidden`));
await shot("ui-popover-open");
await evaluate(`document.body.click()`);
check("cost popover dismisses on outside click",
  await evaluate(`document.getElementById("costPop").hidden`));

check("saved card renders an estimate",
  await evaluate(`[...document.querySelectorAll(".card .n")]
    .some(n => n.textContent.startsWith("~$"))`));
await click('[data-pop="costPop"]');
await click('[data-pop="savedPop"]');
check("saved popover opens and closes cost popover",
  await evaluate(`!document.getElementById("savedPop").hidden
    && document.getElementById("costPop").hidden`));
await evaluate(`document.body.click()`);
check("saved popover dismisses on outside click",
  await evaluate(`document.getElementById("savedPop").hidden`));

await evaluate(`refresh()`);
await sleep(400);
check("filter survives the 5s data refresh",
  await evaluate(`document.querySelector('[data-filter="succeeded"]').classList.contains("sel")`));

await click("details.activity summary");
check("activity section collapses",
  await evaluate(`!document.querySelector("details.activity").open`));
await shot("ui-filtered-collapsed");

console.table(results);
chrome.kill();
process.exit(results.every((r) => r.result === "PASS") ? 0 : 1);
