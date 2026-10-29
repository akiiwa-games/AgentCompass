// Registers the Dot2API address as a custom MCP plugin in the logged-in ChatGPT web session of the bridge.
//
// ChatGPT cannot edit the URL of a plugin, and a quick tunnel gets a new address on every start, so after a
// reboot a new plugin has to be added (and the dead ones removed). The bridge's ChatGPT view is hidden: its
// document is `hidden`, screenshots hang and real mouse events do nothing, so the page is driven through its
// DOM instead (native value setters, and pointer events because Radix menus open on pointerdown).
//
// usage: node dot_plugin.mjs <cdp-endpoint> register <name> <description> <url-file>
//        node dot_plugin.mjs <cdp-endpoint> remove <name>...
//        node dot_plugin.mjs <cdp-endpoint> wake <message-file>
//        node dot_plugin.mjs <cdp-endpoint> list
//        node dot_plugin.mjs <cdp-endpoint> eval <js>
// The URL is read from a file so that it (it carries a secret) never appears in a process listing.
// Prints one JSON line: {"ok": true, ...} or {"ok": false, "step": "...", "error": "..."}.
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const [endpoint, command, ...rest] = process.argv.slice(2);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function connect() {
  const pages = await (await fetch(new URL("/json/list", endpoint))).json();
  const page = pages.find((p) => p.type === "page" && p.url.startsWith("https://chatgpt.com"));
  if (!page) throw new Error("no ChatGPT page in the bridge browser");
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = () => reject(new Error("cannot open the debug socket")); });
  let nextId = 1;
  const pending = new Map();
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
  };
  const call = (method, params = {}) => new Promise((resolve) => {
    const id = nextId++;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const reply = await call("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (reply.result?.exceptionDetails) throw new Error(reply.result.exceptionDetails.exception?.description?.split("\n")[0] ?? "script error");
    return reply.result?.result?.value;
  };
  return { call, evaluate, close: () => ws.close() };
}

class Step extends Error {
  constructor(step, message) { super(message); this.step = step; }
}

// Dispatches the pointer sequence a real click produces on the first element matching the predicate source.
const CLICK = `(find) => {
  const el = find();
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const o = { bubbles: true, cancelable: true, button: 0, pointerType: "mouse", isPrimary: true, clientX: r.x + 5, clientY: r.y + 5, view: window };
  for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) el.dispatchEvent(t.startsWith("pointer") ? new PointerEvent(t, o) : new MouseEvent(t, o));
  return true;
}`;

async function until(page, step, expression, seconds = 20) {
  for (let i = 0; i < seconds * 4; i++) {
    if (await page.evaluate(expression)) return;
    await sleep(250);
  }
  throw new Step(step, `timed out waiting for: ${step}`);
}

async function click(page, step, findSource) {
  const done = await page.evaluate(`(${CLICK})(() => { ${findSource} })`);
  if (!done) throw new Step(step, `element not found: ${step}`);
}

// Attributes and roles take precedence over translated labels. Refuse ambiguous matches.
export function findControl(root, selectors, labels, role = "button, [role=button]") {
  if (!root) return null;
  const enabled = (element) => !element.disabled && element.getAttribute("aria-disabled") !== "true";
  for (const selector of selectors) {
    const matches = [...root.querySelectorAll(selector)].filter(enabled);
    if (matches.length === 1) return matches[0];
  }
  for (const label of labels) {
    const matches = [...root.querySelectorAll(role)].filter((element) => {
      const labelledBy = (element.getAttribute("aria-labelledby") || "").split(/\s+/)
        .map((id) => element.ownerDocument?.getElementById(id)?.textContent || "").join(" ").trim();
      return enabled(element) && [element.getAttribute("aria-label"), labelledBy,
        element.getAttribute("title"), element.innerText].some((value) => (value || "").trim().toLowerCase() === label.toLowerCase());
    });
    if (matches.length === 1) return matches[0];
  }
  return null;
}

const actions = {
  add: [["button[data-testid=add-plugin]", "button[data-action=add-plugin]"], ["Add", "添加"]],
  custom: [["[role=menuitem][data-testid=add-custom-mcp]", "[role=menuitem][data-action=add-custom-mcp]"],
    ["Add custom MCP server", "Add a custom MCP server", "添加自定义 MCP 服务器"], "[role=menuitem]"],
  refresh: [["button[data-testid=refresh-tools]", "button[data-action=refresh-tools]"], ["Refresh tools", "刷新工具"]],
  connect: [["button[data-testid=connect-plugin]", "button[data-action=connect-plugin]"], ["Connect", "关联", "连接"]],
  remove: [["button[data-testid=delete-plugin]", "button[data-action=delete-plugin]"], ["Delete app", "Delete plugin", "Remove app", "删除应用"]],
  cancel: [["button[data-testid=cancel]", "button[data-action=cancel]"] , ["Cancel", "取消"]],
  create: [["button[type=submit]", "button[data-testid=create-plugin]"], ["Create plugin", "Create as plugin", "创建为插件"]],
  confirmDelete: [["button[type=submit]", "button[data-testid=confirm-delete]", "button[data-action=delete-plugin]"], ["Delete", "Remove", "删除", "删除应用"]],
  send: [["button[data-testid=send-button]", "button[data-testid=fruitjuice-send-button]"], ["Send", "Send message", "发送"]],
};

export function actionExpression(action, root = "document", name = "") {
  const [selectors, labels, role] = actions[action];
  const names = action === "connect" && name ? [`Connect ${name}`, `连接 ${name}`, ...labels] : labels;
  return `(${findControl.toString()})(${root}, ${JSON.stringify(selectors)}, ${JSON.stringify(names)}, ${JSON.stringify(role || "button, [role=button]")})`;
}

const actionSource = (action, root, name) => `return ${actionExpression(action, root, name)};`;
const hasAction = (action, root, name) => `!!${actionExpression(action, root, name)}`;

async function go(page, url, readyExpression) {
  await page.call("Page.navigate", { url });
  await sleep(1500);
  await until(page, `open ${url}`, readyExpression, 40);
}

async function listPlugins(page) {
  await go(page, "https://chatgpt.com/plugins", hasAction("add"));
  await sleep(1500);
  return page.evaluate(`[...document.querySelectorAll('a[href^="/plugins/plugin_"]')].map((a) => (a.innerText || "").trim().split(String.fromCharCode(10))[0].trim())`);
}

// A plugin is connected when its detail page offers 刷新工具 (refresh tools); until then it offers 关联 (connect).
async function ensureConnected(page, name) {
  const installed = await listPlugins(page);
  if (!installed.includes(name)) throw new Step("verify", "the new plugin is not in the installed list");
  const href = await page.evaluate(`[...document.querySelectorAll('a[href^="/plugins/plugin_"]')].find((a) => (a.innerText || "").trim().split(String.fromCharCode(10))[0].trim() === ${JSON.stringify(name)})?.href ?? ""`);
  if (!href) throw new Step("verify", `no manage link for ${name}`);
  await go(page, href, hasAction("remove"));
  await sleep(1500);
  const state = `(${hasAction("refresh")} ? "connected" : ${hasAction("connect")} ? "unconnected" : "unknown")`;
  if ((await page.evaluate(state)) === "unconnected") {
    await click(page, "open the connect dialog", actionSource("connect"));
    await until(page, "connect dialog", hasAction("connect", 'document.querySelector("[role=dialog]")', name), 20);
    await click(page, "connect", actionSource("connect", 'document.querySelector("[role=dialog]")', name));
    await until(page, "connected", `!document.querySelector("[role=dialog]")`, 30);
    await sleep(2000);
  }
  if ((await page.evaluate(state)) !== "connected") throw new Step("verify", `${name} is not connected`);
  return installed;
}

async function register(page, name, description, url) {
  const before = await listPlugins(page);
  if (!before.includes(name)) {
    await click(page, "open the add menu", actionSource("add"));
    await until(page, "add menu", hasAction("custom"));
    await click(page, "choose custom MCP server", actionSource("custom"));
    await until(page, "form", `!!document.querySelector('[role=dialog] input[type=url], [role=dialog] input[name=url], [role=dialog] input[placeholder="https://example.com/mcp"]')`);
    const filled = await page.evaluate(`(() => {
      const set = (el, v) => {
        const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      };
      const d = document.querySelector("[role=dialog]");
      const urlInput = d.querySelector('input[type=url], input[name=url], input[placeholder="https://example.com/mcp"]');
      const textInputs = [...d.querySelectorAll('input:not([type=hidden]):not([type=checkbox])')].filter((e) => e !== urlInput);
      const fields = [d.querySelector('input[name=name], input[data-testid=plugin-name]') || (textInputs.length === 1 ? textInputs[0] : null) ||
        d.querySelector('input[placeholder="Custom tool"], input[placeholder="自定义工具"]'), d.querySelector("textarea"), urlInput, d.querySelector("select")];
      if (fields.some((f) => !f)) return false;
      set(fields[0], ${JSON.stringify(name)});
      set(fields[1], ${JSON.stringify(description)});
      set(fields[2], ${JSON.stringify(url)});
      set(fields[3], "NONE");
      return true;
    })()`);
    if (!filled) throw new Step("fill", "the add form has changed shape");
    await click(page, "accept the risk box", `return document.querySelector("[role=dialog] button[role=checkbox]");`);
    await until(page, "submit enabled", `(() => { const b = document.querySelector("[role=dialog] button[type=submit]"); return !!b && !b.disabled; })()`, 10);
    await click(page, "create", actionSource("create", 'document.querySelector("[role=dialog]")'));
    // Success opens the connect dialog (it can take a minute); a refused address shows the error in the form instead.
    // If the dialog never shows, the plugin still exists and is connected from its detail page below.
    for (let i = 0; i < 360; i++) {
      const text = await page.evaluate(`document.querySelector("[role=dialog]")?.innerText ?? ""`);
      if (/无法连接到 MCP 服务器|unable to connect to (the )?MCP server|could not connect to (the )?MCP server/i.test(text)) throw new Step("create", "ChatGPT could not reach the MCP server at that address");
      if (await page.evaluate(hasAction("connect", 'document.querySelector("[role=dialog]")', name))) {
        await click(page, "connect", actionSource("connect", 'document.querySelector("[role=dialog]")', name));
        await until(page, "connect dialog closed", `!document.querySelector("[role=dialog]")`, 30);
        break;
      }
      await sleep(250);
    }
  }
  return { installed: await ensureConnected(page, name) };
}

async function remove(page, names) {
  const removed = [];
  for (const name of names) {
    const installed = await listPlugins(page);
    if (!installed.includes(name)) continue;
    const href = await page.evaluate(`[...document.querySelectorAll('a[href^="/plugins/plugin_"]')].find((a) => (a.innerText || "").trim().split(String.fromCharCode(10))[0].trim() === ${JSON.stringify(name)})?.href ?? ""`);
    if (!href) throw new Step("find", `no manage link for ${name}`);
    await go(page, href, hasAction("remove"));
    await click(page, "delete", actionSource("remove"));
    await until(page, "confirm", `!!document.querySelector("[role=dialog]")`, 10);
    const dialog = await page.evaluate(`document.querySelector("[role=dialog]").innerText`);
    if (!dialog.includes(name)) throw new Step("confirm", `the delete dialog does not name ${name}: ${dialog.slice(0, 80)}`);
    if (process.env.DOT_PLUGIN_DRY) {
      await click(page, "cancel", actionSource("cancel", 'document.querySelector("[role=dialog]")'));
      return { dry: true, dialog };
    }
    await click(page, "confirm delete", actionSource("confirmDelete", 'document.querySelector("[role=dialog]")'));
    await until(page, "deleted", `!document.querySelector("[role=dialog]")`, 20);
    removed.push(name);
  }
  return { removed, installed: await listPlugins(page) };
}

// Sends a message to the Dot (the "你的 dot" thread): it holds the standing instructions, and only a message
// there makes it subscribe to the queue through the plugin that is connected at that moment.
async function wake(page, text) {
  const composer = `document.querySelector('#prompt-textarea[contenteditable=true], [data-testid=dot-composer][contenteditable=true], [role=textbox][contenteditable=true], div[contenteditable=true][aria-label=Message], div[contenteditable=true][aria-label="消息"]')`;
  await go(page, "https://chatgpt.com/dots/home", `!!${composer}`);
  await sleep(2500);
  const typed = await page.evaluate(`(() => {
    const el = ${composer};
    el.focus();
    document.execCommand("selectAll", false);
    document.execCommand("insertText", false, ${JSON.stringify(text)});
    return el.innerText.trim().length;
  })()`);
  if (!typed) throw new Step("type", "the message did not reach the Dot composer");
  await sleep(500);
  await click(page, "send", actionSource("send"));
  await until(page, "message sent", `(${composer}?.innerText ?? "").trim().length === 0`, 15);
  return { sent: true };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
let page;
try {
  page = await connect();
  let result;
  if (command === "register") {
    const [name, description, urlFile] = rest;
    result = await register(page, name, description, fs.readFileSync(urlFile, "utf8").trim());
  } else if (command === "remove") {
    result = await remove(page, rest);
  } else if (command === "wake") {
    result = await wake(page, fs.readFileSync(rest[0], "utf8").trim());
  } else if (command === "list") {
    result = { installed: await listPlugins(page) };
  } else if (command === "eval") {
    result = { value: await page.evaluate(rest[0]) };
  } else {
    throw new Step("usage", `unknown command ${command}`);
  }
  console.log(JSON.stringify({ ok: true, ...result }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, step: error.step ?? "run", error: String(error.message ?? error) }));
}
page?.close();
process.exit(0);
}
