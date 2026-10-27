// Sign Pi in to a ChatGPT subscription (provider openai-codex) with the device-code flow, so no TUI is
// needed and it works on a remote host. Uses Pi's own OAuth and credential store; writes <agent-dir>/auth.json.
//
//   node setup/pi_device_login.mjs [--status-file FILE]
//
// Prints the verification URL and user code, then waits (up to 15 minutes) until the code is entered.
import { execSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const statusIndex = process.argv.indexOf("--status-file");
const statusFile = statusIndex > 0 ? process.argv[statusIndex + 1] : undefined;

function report(record) {
    const line = JSON.stringify({ at: new Date().toISOString(), ...record });
    console.log(line);
    if (statusFile) writeFileSync(statusFile, line + "\n", { flag: "a" });
}

const globalRoot = execSync("npm root -g", { encoding: "utf8" }).trim();
const agentPackage = join(globalRoot, "@earendil-works", "pi-coding-agent");
const piAi = join(agentPackage, "node_modules", "@earendil-works", "pi-ai", "dist");
const { openaiCodexOAuth } = await import(pathToFileURL(join(piAi, "auth", "oauth", "openai-codex.js")).href);
const { AuthStorage } = await import(pathToFileURL(join(agentPackage, "dist", "core", "auth-storage.js")).href);

const controller = new AbortController();
setTimeout(() => controller.abort(), 15 * 60 * 1000).unref();

const credential = await openaiCodexOAuth.login({
    signal: controller.signal,
    async prompt(prompt) {
        if (prompt.type === "select") return "device_code";
        throw new Error(`Unexpected prompt: ${JSON.stringify(prompt)}`);
    },
    notify(event) {
        if (event.type === "device_code") {
            report({ event: "device_code", url: event.verificationUri, code: event.userCode, expiresInSeconds: event.expiresInSeconds });
        } else {
            report({ event: event.type });
        }
    },
});

const storage = AuthStorage.create();
await storage.modify("openai-codex", async () => credential);
report({ event: "saved", provider: "openai-codex", accountId: credential.accountId, expires: new Date(credential.expires).toISOString() });
