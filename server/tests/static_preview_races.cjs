const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const diagnosticPath = process.argv[2];
let phase = "starting";
function record(next) {
  phase = next;
  if (diagnosticPath) fs.writeFileSync(diagnosticPath, JSON.stringify({
    phase, node: process.version, resources: process.getActiveResourcesInfo(),
  }));
}
record(phase);

const source = fs.readFileSync(path.join(__dirname, "../hidagent/static/app.js"), "utf8");
const liveStart = source.indexOf('const live = $("live");');
const liveEnd = source.indexOf("// ---- controls", liveStart);
const pollStart = source.indexOf('let shown = "";');
const pollEnd = source.indexOf("\napplyLang();", pollStart);
assert(liveStart > 0 && liveEnd > liveStart && pollStart > liveEnd && pollEnd > pollStart);

function harness() {
  const handlers = {}, nodes = {}, requests = [], writes = [];
  let blobNumber = 0;
  function node(id) {
    return nodes[id] ||= {
      src: "", complete: true, hidden: false, textContent: "", dataset: {},
      get naturalWidth() { return this.src ? 390 : 0; },
      get naturalHeight() { return this.src ? 844 : 0; },
      parentElement: { scrollTop: 0, clientHeight: 0, scrollHeight: 0 },
      classList: { toggle() {} },
      addEventListener(event, fn) { if (id === "live") handlers[event] = fn; },
      querySelectorAll() { return []; },
      replaceChildren() {},
      getBoundingClientRect() { return { left: 0, top: 0, width: 390, height: 844 }; },
      removeAttribute(name) { if (name === "src") this.src = ""; },
      hasAttribute(name) { return name === "src" && !!this.src; },
    };
  }
  const context = {
    $: node, I18N: { en: {} }, lang: "en", t: value => value, say() {}, calibrationHint() {},
    setTimeout() {}, Date, Math, AbortSignal, aspect: .46, fmt: String,
    formatHid: () => "", isPlan: () => false, logLine() {},
    post: async (...args) => writes.push(args),
    URL: { createObjectURL: () => `blob:${++blobNumber}`, revokeObjectURL() {} },
    fetch: (url) => new Promise((resolve, reject) => requests.push({ url, resolve, reject })),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(liveStart, liveEnd) + source.slice(pollStart, pollEnd)
    + ";globalThis.api={poll,refreshLive,state:()=>({previewState,previewGeneration,expectsFrame,src:live.src})};", context);
  function take(url) {
    const at = requests.findIndex(request => request.url === url);
    assert(at >= 0, `missing request ${url}`);
    return requests.splice(at, 1)[0];
  }
  async function status(generation, available) {
    const done = context.api.poll();
    take("/api/status").resolve({ json: async () => ({
      screen: available, video: { generation, state: available ? "live" : "disconnected" },
      agent: { steps: [], running: false }, models: { planner: "", actor: "" },
    }) });
    await done;
  }
  async function click() {
    handlers.pointerdown({ isPrimary: true, button: 0, pointerId: 1, clientX: 100, clientY: 400 });
    await handlers.pointerup({ pointerId: 1, clientX: 100, clientY: 400 });
  }
  return { ...context.api, take, status, click, writes };
}

function response(generation, blob = async () => new Uint8Array([1])) {
  return { ok: true, headers: { get: key => ({
    "X-Preview-State": "live", "X-Preview-Generation": String(generation), "X-Frame-Age": "0.1",
  })[key] }, blob };
}

async function publish(h, generation) {
  const done = h.refreshLive();
  h.take("/api/frame.jpg").resolve(response(generation));
  await done;
}

async function main() {
  // The old request completes after a disconnect and a new live status.
  {
    record("disconnect-response");
    const h = harness();
    await h.status(1, true);
    const done = h.refreshLive(), pending = h.take("/api/frame.jpg");
    await h.status(2, false);
    await h.status(2, true);
    pending.resolve(response(1));
    await done;
    await h.click();
    assert.equal(h.state().src, "", "pre-disconnect frame became visible again");
    assert.equal(h.writes.length, 0, "old image coordinates reached the tap endpoint");
    await publish(h, 2);
    await h.click();
    assert.equal(h.writes[0][0], "/api/tap", "a current frame must remain usable");
  }
  // Polling can miss the disconnected interval but still observe a new generation.
  {
    record("generation-response");
    const h = harness();
    await h.status(1, true);
    await publish(h, 1);
    const done = h.refreshLive(), pending = h.take("/api/frame.jpg");
    await h.status(2, true);
    await h.click();
    assert.equal(h.writes.length, 0, "previous generation stayed clickable after live status changed");
    pending.resolve(response(1));
    await done;
    assert.equal(h.state().src, "");
  }
  // A generation can change while the response body, rather than its headers, arrives.
  {
    record("late-response-body");
    const h = harness();
    await h.status(1, true);
    let finishBody;
    const body = new Promise(resolve => { finishBody = resolve; });
    const done = h.refreshLive();
    h.take("/api/frame.jpg").resolve(response(1, () => body));
    await Promise.resolve();
    await h.status(2, true);
    finishBody(new Uint8Array([1]));
    await done;
    assert.equal(h.state().src, "", "late body overwrote the new session state");
  }
  // An old request's timeout must not undo a newer successful status response.
  {
    record("late-request-failure");
    const h = harness();
    await h.status(1, true);
    const done = h.refreshLive(), pending = h.take("/api/frame.jpg");
    await h.status(2, false);
    await h.status(2, true);
    pending.reject(new Error("old request timeout"));
    await done;
    assert.equal(h.state().expectsFrame, true);
    await publish(h, 2);
    assert(h.state().src, "a current frame was blocked by an obsolete request failure");
  }
  // Transient fetch timeouts must not drop the live preview until 3 consecutive failures.
  {
    record("transient-frame-timeouts");
    const h = harness();
    await h.status(1, true);
    await publish(h, 1);
    for (let i = 0; i < 2; i += 1) {
      const done = h.refreshLive();
      h.take("/api/frame.jpg").reject(new Error("timeout"));
      await done;
      assert.equal(h.state().expectsFrame, true);
      assert(h.state().src, "transient frame timeout cleared preview prematurely");
    }
    await publish(h, 1);  // valid frame resets failure streak
    const done1 = h.refreshLive();
    h.take("/api/frame.jpg").reject(new Error("timeout"));
    await done1;
    assert.equal(h.state().expectsFrame, true);
    for (let i = 0; i < 2; i += 1) {
      const done = h.refreshLive();
      h.take("/api/frame.jpg").reject(new Error("timeout"));
      await done;
    }
    assert.equal(h.state().expectsFrame, false);
    assert.equal(h.state().previewState, "disconnected");
    assert.equal(h.state().src, "");
  }
  // Transient status timeouts tolerate 2 stalls, while an explicit screen=false disconnects at once.
  {
    record("transient-status-timeouts");
    const h = harness();
    await h.status(1, true);
    await publish(h, 1);
    for (let i = 0; i < 2; i += 1) {
      const done = h.poll();
      h.take("/api/status").reject(new Error("timeout"));
      await done;
      assert.equal(h.state().expectsFrame, true);
      assert(h.state().src, "transient status timeout cleared preview prematurely");
    }
    const third = h.poll();
    h.take("/api/status").reject(new Error("timeout"));
    await third;
    assert.equal(h.state().expectsFrame, false);
    assert.equal(h.state().previewState, "disconnected");

    await h.status(2, true);
    await publish(h, 2);
    await h.status(2, false);
    assert.equal(h.state().expectsFrame, false);
    assert.equal(h.state().previewState, "disconnected");
  }
  // post() must apply an AbortSignal timeout and surface fetch failures to #msg instead of throwing.
  {
    record("post-timeout-feedback");
    const postStart = source.indexOf("async function post(");
    const postEnd = source.indexOf("// ---- step helpers", postStart);
    assert(postStart > 0 && postEnd > postStart);
    let capturedOpts = null;
    const msgNode = { textContent: "" };
    const ctx = {
      $: (id) => (id === "msg" ? msgNode : { textContent: "" }),
      lang: "en",
      AbortSignal,
      fetch: async (_url, opts) => {
        capturedOpts = opts;
        throw new Error("network down");
      },
    };
    vm.createContext(ctx);
    vm.runInContext(source.slice(postStart, postEnd) + ";globalThis.post = post;", ctx);
    const res = await ctx.post("/api/agent/stop", {});
    assert.equal(res, null);
    assert(capturedOpts && capturedOpts.signal, "post() must attach an AbortSignal timeout");
    assert(msgNode.textContent.includes("network down"), "post() must display failure feedback");
  }
}

// A pending Promise alone does not keep Node alive. Mark completion explicitly
// so an accidentally unawaited scenario cannot pass by silently exiting early.
main().then(() => record("complete")).catch(error => {
  record(`failed:${phase}`);
  console.error(error);
  process.exitCode = 1;
});
