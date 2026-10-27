import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { evaluateRemoteSyncSafety } from "./sync-safety.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pluginRoot = path.resolve(__dirname, "..");
const repoRoot = path.resolve(pluginRoot, "..");
const sdkPkgJson = path.join(repoRoot, "setup", "host_sync", "sdk", "package.json");
const requireFromSdk = createRequire(sdkPkgJson);

const { DaemonClient } = await import(
  pathToFileURL(
    path.join(
      repoRoot,
      "setup",
      "host_sync",
      "sdk",
      "node_modules",
      "@getpaseo",
      "client",
      "dist",
      "daemon-client.js",
    ),
  ).href
);
const { parseConnectionOfferFromUrl } = await import(
  pathToFileURL(
    path.join(
      repoRoot,
      "setup",
      "host_sync",
      "sdk",
      "node_modules",
      "@getpaseo",
      "protocol",
      "dist",
      "connection-offer.js",
    ),
  ).href
);
const { buildRelayWebSocketUrl } = await import(
  pathToFileURL(
    path.join(
      repoRoot,
      "setup",
      "host_sync",
      "sdk",
      "node_modules",
      "@getpaseo",
      "protocol",
      "dist",
      "daemon-endpoints.js",
    ),
  ).href
);
const { WebSocket } = requireFromSdk("ws");

const PLUGIN_ID = "antigravity-resilient-provider";
const PROVIDER_ID = "antigravity-resilient";
const REMOTE_REPO_ROOT = process.env.ROUTE_MANAGER_REMOTE_ROOT || repoRoot.replace(/\\/g, "/");
const REMOTE_PLUGIN_ROOT = `${REMOTE_REPO_ROOT}/paseo-antigravity-resilient`;
// The remote user's Paseo home is that host's own fact: read from the upload path, else from this host's
// environment, never guessed in source.
const REMOTE_PASEO_HOME = process.env.ROUTE_MANAGER_REMOTE_PASEO_HOME || null;

function remoteConversationsDir(remoteSync) {
  const home = remoteSync?.remotePaseoHome ?? REMOTE_PASEO_HOME;
  if (!home) {
    throw new Error("Remote Paseo home unknown: run the remote sync or set ROUTE_MANAGER_REMOTE_PASEO_HOME");
  }
  return `${home}/antigravity-resilient/conversations`;
}

function parseArgs(argv) {
  let target = "local";
  let skipSmoke = false;
  let keepAgent = false;
  let dryRunSync = false;
  let replayConversationId = null;
  let model = "gemini-3.8-flash-low";
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--target" && argv[i + 1]) {
      target = argv[i + 1];
      i += 1;
    } else if (arg.startsWith("--target=")) {
      target = arg.slice("--target=".length);
    } else if (arg === "--skip-smoke") {
      skipSmoke = true;
    } else if (arg === "--keep-agent") {
      keepAgent = true;
    } else if (arg === "--dry-run-sync") {
      dryRunSync = true;
    } else if (arg === "--replay-conversation" && argv[i + 1]) {
      replayConversationId = argv[i + 1];
      i += 1;
    } else if (arg.startsWith("--replay-conversation=")) {
      replayConversationId = arg.slice("--replay-conversation=".length);
    } else if (arg === "--model" && argv[i + 1]) {
      model = argv[i + 1];
      i += 1;
    }
  }
  if (target !== "local" && target !== "remote") {
    throw new Error(`Invalid --target "${target}". Expected "local" or "remote".`);
  }
  return {
    target,
    skipSmoke,
    keepAgent,
    dryRunSync,
    replayConversationId,
    model,
  };
}

// The daemon's listen address lives in ~/.paseo/config.json and differs per host.
function localListen() {
  try {
    const listen = JSON.parse(fs.readFileSync(path.join(os.homedir(), ".paseo", "config.json"), "utf8"))?.daemon?.listen;
    if (typeof listen === "string" && listen.trim()) return listen.trim();
  } catch {}
  return "127.0.0.1:6767";
}

function createDaemonClient(target) {
  if (target === "local") {
    const credPath = path.join(os.homedir(), ".paseo", "local-credential");
    return new DaemonClient({
      url: `ws://${localListen()}/ws`,
      clientId: randomUUID(),
      clientType: "cli",
      appVersion: "0.11.2",
      connectTimeoutMs: 20_000,
      localCredential: () => fs.readFileSync(credPath, "utf8").trim(),
      webSocketFactory: (url, config) =>
        new WebSocket(url, {
          headers: config?.headers,
          protocols: config?.protocols,
        }),
      reconnect: { enabled: false },
    });
  }

  const endpointFile = process.env.ROUTE_MANAGER_REMOTE_ENDPOINT || path.join(os.homedir(), ".route-manager", "remote-endpoint.txt");
  const offer = parseConnectionOfferFromUrl(fs.readFileSync(endpointFile, "utf8").trim());
  return new DaemonClient({
    url: buildRelayWebSocketUrl({
      ...offer.relay,
      serverId: offer.serverId,
      role: "client",
    }),
    clientId: randomUUID(),
    clientType: "cli",
    appVersion: "0.11.2",
    connectTimeoutMs: 30_000,
    webSocketFactory: (url, config) =>
      new WebSocket(url, {
        headers: config?.headers,
        protocols: config?.protocols,
      }),
    e2ee: {
      enabled: true,
      daemonPublicKeyB64: offer.daemonPublicKeyB64,
    },
    reconnect: { enabled: false },
  });
}

function decodeExplorerBytes(fileResult) {
  if (fileResult?.bytes instanceof Uint8Array) {
    return Buffer.from(fileResult.bytes).toString("utf8");
  }
  if (typeof fileResult?.content === "string") {
    return fileResult.content;
  }
  return JSON.stringify(fileResult);
}

async function closeTerminalSafely(client, terminalId) {
  if (!terminalId) {
    return;
  }
  try {
    await client.killTerminal(terminalId);
  } catch {
    // Ignore if already exited
  }
  try {
    await client.closeItems({ terminalIds: [terminalId] });
  } catch {
    // Ignore cleanup errors
  }
}

async function runRemotePowerShellAndCaptureMarker(client, cwd, name, psCommand, _markerPrefix, timeoutMs = 30_000) {
  const nonce = randomUUID();
  const relOutDir = "setup/host_sync/out";
  const relOutPath = `${relOutDir}/${name}-${nonce}.json`;
  const absOutDir = `${REMOTE_REPO_ROOT}/${relOutDir}`;
  const absOutPath = `${REMOTE_REPO_ROOT}/${relOutPath}`;
  const absTmpPath = `${absOutPath}.tmp`;

  const wrappedPs = [
    `$ErrorActionPreference = 'Continue'`,
    `New-Item -ItemType Directory -Force -Path '${absOutDir}' | Out-Null`,
    `$lines = @(& { ${psCommand} })`,
    `$joined = ($lines -join "\`n")`,
    `if ($joined -match '<<<[A-Za-z0-9_]+:([A-Za-z0-9+/=\\r\\n\\s]+)>>>') {`,
    `  $rawBytes = [Convert]::FromBase64String(($Matches[1] -replace '\\s+', ''))`,
    `  [IO.File]::WriteAllBytes('${absTmpPath}', $rawBytes)`,
    `  Move-Item -Force -Path '${absTmpPath}' -Destination '${absOutPath}'`,
    `}`,
  ].join("; ");

  const termResp = await client.createTerminal(cwd, name, undefined, {
    command: "powershell.exe",
    args: ["-NoProfile", "-NonInteractive", "-Command", wrappedPs],
  });
  const terminalId = termResp?.terminal?.id ?? termResp?.terminalId ?? termResp?.id;

  try {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 750));
      try {
        const fileRes = await client.readFile(REMOTE_REPO_ROOT, relOutPath, undefined, 1024 * 1024);
        const content = decodeExplorerBytes(fileRes).trim();
        if (content) {
          return content;
        }
      } catch {
        // File not written yet
      }
    }
    throw new Error(`Timed out after ${timeoutMs}ms waiting for ${relOutPath} from remote terminal ${name}`);
  } finally {
    await closeTerminalSafely(client, terminalId);
    try {
      await client.deleteFileEntry({ cwd: REMOTE_REPO_ROOT, path: relOutPath });
    } catch {
      // Ignore cleanup errors
    }
  }
}

async function backupDaemonConfig(client, target) {
  const paseoDir = path.join(os.homedir(), ".paseo");
  if (target === "local") {
    const configPath = path.join(paseoDir, "config.json");
    const backupPath = path.join(paseoDir, "config.json.pre-antigravity-resilient.bak");
    if (fs.existsSync(configPath) && !fs.existsSync(backupPath)) {
      fs.copyFileSync(configPath, backupPath);
    }
    return backupPath;
  }
  const currentConfig = await client.getDaemonConfig();
  const backupPath = path.join(
    os.homedir(),
    ".paseo",
    "remote-config.pre-antigravity-resilient.bak.json",
  );
  if (!fs.existsSync(backupPath)) {
    fs.writeFileSync(backupPath, JSON.stringify(currentConfig, null, 2), "utf8");
  }
  return backupPath;
}

async function waitForAgentTurnCompletion(client, agentId, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const fetched = await client.fetchAgent(agentId);
    const agent = fetched?.agent;
    if (!agent) {
      throw new Error(`Agent ${agentId} disappeared while waiting for turn completion`);
    }
    const status = agent.status ?? agent.lifecycle ?? "";
    if (status === "idle" || status === "completed" || status === "ready") {
      return agent;
    }
    if (status === "error" || status === "failed") {
      throw new Error(
        `Agent ${agentId} entered failure state: ${JSON.stringify(agent.lastError ?? agent)}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 1_500));
  }
  throw new Error(`Timed out after ${timeoutMs}ms waiting for agent ${agentId} to finish turn`);
}

function extractConversationId(agent) {
  const candidates = [
    agent?.persistence?.data?.conversationId,
    agent?.providerPersistence?.data?.conversationId,
    agent?.persistence?.sessionId,
    agent?.providerHandleId,
    agent?.sessionId,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) {
      const trimmed = candidate.trim();
      if (trimmed.startsWith("plugin:")) {
        try {
          const parsed = JSON.parse(trimmed.slice("plugin:".length));
          if (typeof parsed?.data?.conversationId === "string" && parsed.data.conversationId.trim()) {
            return parsed.data.conversationId.trim();
          }
        } catch {
          // Ignore malformed handle
        }
      }
      return trimmed;
    }
  }
  return null;
}

async function cleanupRemoteBundleAndCandidateRef(client, remoteBundlePath) {
  const cleanupPs = [
    `$ErrorActionPreference = 'Continue'`,
    `git -C '${REMOTE_REPO_ROOT}' update-ref -d refs/remotes/origin/bundle-candidate`,
    `Remove-Item -Force -ErrorAction SilentlyContinue '${remoteBundlePath}'`,
  ].join("; ");
  const termResp = await client.createTerminal(REMOTE_REPO_ROOT, "git-bundle-cleanup", undefined, {
    command: "powershell.exe",
    args: ["-NoProfile", "-NonInteractive", "-Command", cleanupPs],
  });
  const terminalId = termResp?.terminal?.id ?? termResp?.terminalId ?? termResp?.id;
  await new Promise((resolve) => setTimeout(resolve, 1_500));
  await closeTerminalSafely(client, terminalId);
}

async function syncRemoteCheckoutViaBundle(client, { dryRun = false } = {}) {
  const localHead = execFileSync("git", ["-C", repoRoot, "rev-parse", "HEAD"], {
    encoding: "utf8",
  }).trim();
  const tmpBundlePath = path.join(os.tmpdir(), `route-manager-${localHead.slice(0, 12)}.bundle`);
  try {
    execFileSync("git", ["-C", repoRoot, "bundle", "create", tmpBundlePath, "main"], {
      stdio: "ignore",
    });
    const bundleBytes = fs.readFileSync(tmpBundlePath);
    const bundleSha256 = createHash("sha256").update(bundleBytes).digest("hex");
    const uploaded = await client.uploadFile({
      fileName: `route-manager-${localHead.slice(0, 12)}.bundle`,
      mimeType: "application/octet-stream",
      bytes: bundleBytes,
    });
    const rawUploadedPath = uploaded?.file?.path ?? uploaded?.path ?? "";
    const remoteBundlePath = String(rawUploadedPath).replaceAll("\\", "/");
    if (!remoteBundlePath) {
      throw new Error(`uploadFile did not return a remote path: ${JSON.stringify(uploaded)}`);
    }
    const remotePaseoHomeMatch = /^(.+?\/\.paseo)(?:\/|$)/i.exec(remoteBundlePath);
    const remotePaseoHome = remotePaseoHomeMatch?.[1] ?? REMOTE_PASEO_HOME;
    if (!remotePaseoHome) {
      throw new Error(`Cannot derive the remote Paseo home from ${remoteBundlePath}; set ROUTE_MANAGER_REMOTE_PASEO_HOME`);
    }

    // Phase 1: Non-destructive inspection. Fetches objects into a candidate ref
    // without touching HEAD, main, index, or the working tree.
    const inspectPs = [
      `$ErrorActionPreference = 'Continue'`,
      `git -C '${REMOTE_REPO_ROOT}' fetch --quiet '${remoteBundlePath}' '+main:refs/remotes/origin/bundle-candidate'`,
      `$branch = ((git -C '${REMOTE_REPO_ROOT}' symbolic-ref --quiet --short HEAD) -join '').Trim()`,
      `$remoteHead = ((git -C '${REMOTE_REPO_ROOT}' rev-parse HEAD) -join '').Trim()`,
      `$porcelain = (git -C '${REMOTE_REPO_ROOT}' status --porcelain=v1 -uall) -join "\`n"`,
      `git -C '${REMOTE_REPO_ROOT}' merge-base --is-ancestor HEAD '${localHead}'`,
      `$isAncestor = ($LASTEXITCODE -eq 0)`,
      `$changed = @()`,
      `if ($isAncestor -and ($remoteHead -ne '${localHead}')) { $changed = @(git -C '${REMOTE_REPO_ROOT}' diff --name-only "HEAD..${localHead}") }`,
      `$payload = @{ branch = $branch; remoteHead = $remoteHead; localHead = '${localHead}'; isAncestor = $isAncestor; porcelainStatus = $porcelain; incomingChangedFiles = $changed } | ConvertTo-Json -Compress -Depth 5`,
      `$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($payload))`,
      `Write-Output "<<<SYNC_INSPECT_B64:$b64>>>"`,
    ].join("; ");

    const rawInspectionJson = await runRemotePowerShellAndCaptureMarker(
      client,
      REMOTE_REPO_ROOT,
      "git-bundle-inspect",
      inspectPs,
      "SYNC_INSPECT_B64",
    );
    const inspection = JSON.parse(rawInspectionJson);
    const decision = evaluateRemoteSyncSafety(inspection);

    if (!decision.safe) {
      await cleanupRemoteBundleAndCandidateRef(client, remoteBundlePath);
      throw new Error(
        `Refusing remote git sync on remote host (fail-closed): ${decision.reasons.join("; ")} | decision=${JSON.stringify(decision)}`,
      );
    }

    if (dryRun || decision.action === "noop") {
      await cleanupRemoteBundleAndCandidateRef(client, remoteBundlePath);
      return {
        dryRun,
        localHead,
        remoteHead: decision.remoteHead,
        remotePaseoHome,
        bundleSha256,
        bundleSize: bundleBytes.length,
        safetyDecision: decision,
      };
    }

    // Phase 2: Provable fast-forward only (no -f, no reset --hard, no clean).
    const ffPs = [
      `$ErrorActionPreference = 'Continue'`,
      `git -C '${REMOTE_REPO_ROOT}' merge --ff-only refs/remotes/origin/bundle-candidate`,
      `if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }`,
      `git -C '${REMOTE_REPO_ROOT}' update-ref refs/remotes/origin/main refs/remotes/origin/bundle-candidate`,
      `git -C '${REMOTE_REPO_ROOT}' update-ref -d refs/remotes/origin/bundle-candidate`,
      `icacls "$env:LOCALAPPDATA\\Programs\\Paseo" /setintegritylevel "(OI)(CI)M" | Out-Null`,
      `icacls "$env:LOCALAPPDATA\\Programs\\Paseo\\Paseo.exe" /setintegritylevel Medium | Out-Null`,
      `Remove-Item -Force -ErrorAction SilentlyContinue '${remoteBundlePath}'`,
    ].join("; ");

    const termResp = await client.createTerminal(REMOTE_REPO_ROOT, "git-bundle-ff", undefined, {
      command: "powershell.exe",
      args: ["-NoProfile", "-NonInteractive", "-Command", ffPs],
    });
    const terminalId = termResp?.terminal?.id ?? termResp?.terminalId ?? termResp?.id;

    let remoteHead = null;
    try {
      const deadline = Date.now() + 30_000;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 1_500));
        const commitsResp = await client.listCheckoutCommits(REMOTE_REPO_ROOT);
        const topSha = commitsResp?.commits?.[0]?.sha ?? null;
        if (topSha === localHead) {
          remoteHead = topSha;
          break;
        }
      }
    } finally {
      await closeTerminalSafely(client, terminalId);
    }

    if (remoteHead !== localHead) {
      throw new Error(
        `Remote checkout HEAD (${remoteHead}) did not fast-forward to local HEAD (${localHead})`,
      );
    }

    return {
      dryRun: false,
      localHead,
      remoteHead,
      remotePaseoHome,
      bundleSha256,
      bundleSize: bundleBytes.length,
      safetyDecision: decision,
    };
  } finally {
    try {
      if (fs.existsSync(tmpBundlePath)) {
        fs.unlinkSync(tmpBundlePath);
      }
    } catch {
      // Ignore cleanup errors
    }
  }
}

function readLocalPersistedSnapshotInfo(conversationId) {
  const snapshotPath = path.join(
    os.homedir(),
    ".paseo",
    "antigravity-resilient",
    "conversations",
    `${conversationId}.json`,
  );
  const eventsPath = path.join(
    os.homedir(),
    ".paseo",
    "antigravity-resilient",
    "conversations",
    `${conversationId}.jsonl`,
  );
  const snapshotData = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));
  const eventsLines = fs.existsSync(eventsPath)
    ? fs
        .readFileSync(eventsPath, "utf8")
        .split(/\r?\n/)
        .filter(Boolean)
    : [];
  return {
    snapshotPath,
    eventsPath,
    persistedItemCount: Array.isArray(snapshotData.items) ? snapshotData.items.length : 0,
    persistedItemTypes: (snapshotData.items ?? []).map((entry) => entry.item?.type),
    eventsLineCount: eventsLines.length,
  };
}

async function readRemotePersistedSnapshotInfo(client, remoteConvDir, conversationId) {
  const snapshotPath = `${remoteConvDir}/${conversationId}.json`;
  const eventsPath = `${remoteConvDir}/${conversationId}.jsonl`;
  const ps = [
    `$ErrorActionPreference = 'Continue'`,
    `$snap = Get-Content -Raw -Encoding UTF8 '${snapshotPath}' | ConvertFrom-Json`,
    `$lines = @()`,
    `if (Test-Path '${eventsPath}') { $lines = @(Get-Content -Encoding UTF8 '${eventsPath}' | Where-Object { $_.Trim() -ne '' }) }`,
    `$out = @{ snapshotPath = '${snapshotPath}'; eventsPath = '${eventsPath}'; persistedItemCount = @($snap.items).Count; persistedItemTypes = @($snap.items | ForEach-Object { $_.item.type }); eventsLineCount = $lines.Count } | ConvertTo-Json -Compress -Depth 5`,
    `$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($out))`,
    `Write-Output "<<<SNAP_INFO_B64:$b64>>>"`,
  ].join("; ");
  const raw = await runRemotePowerShellAndCaptureMarker(
    client,
    REMOTE_REPO_ROOT,
    "inspect-snapshot",
    ps,
    "SNAP_INFO_B64",
  );
  return JSON.parse(raw);
}

const options = parseArgs(process.argv.slice(2));
const client = createDaemonClient(options.target);
const targetPluginPath = options.target === "local" ? pluginRoot.replaceAll("\\", "/") : REMOTE_PLUGIN_ROOT;
const targetCwd = options.target === "local" ? repoRoot.replaceAll("\\", "/") : REMOTE_REPO_ROOT;

await client.connect();
let smokeAgentId = null;

try {
  let remoteSync = null;
  if (options.target === "remote") {
    remoteSync = await syncRemoteCheckoutViaBundle(client, {
      dryRun: options.dryRunSync,
    });
    if (options.dryRunSync) {
      console.log(JSON.stringify({ target: options.target, remoteSync }, null, 2));
      process.exit(0);
    }
  }

  const backupPath = await backupDaemonConfig(client, options.target);
  const daemonConfigResp = await client.getDaemonConfig();
  const pluginsInitiallyEnabled = daemonConfigResp?.config?.pluginsEnabled === true;

  if (!pluginsInitiallyEnabled) {
    await client.patchDaemonConfig({ pluginsEnabled: true });
  }

  let installedPlugin = null;
  try {
    installedPlugin = await client.installPluginSource({
      source: targetPluginPath,
    });
  } catch (err) {
    if (!/already (?:installed|configured)/i.test(String(err?.message ?? err))) {
      throw err;
    }
  }
  const reloadedPlugin = await client.reloadPlugin(PLUGIN_ID);

  await client.refreshProvidersSnapshot({
    cwd: targetCwd,
    providers: [PROVIDER_ID],
  });
  const providersSnapshot = await client.getProvidersSnapshot({ cwd: targetCwd });
  let providerStatus =
    providersSnapshot?.providers?.[PROVIDER_ID] ??
    (Array.isArray(providersSnapshot?.providers)
      ? providersSnapshot.providers.find((entry) => entry.id === PROVIDER_ID || entry.provider === PROVIDER_ID)
      : null);
  if (!providerStatus) {
    try {
      providerStatus = await client.getProviderDiagnostic(PROVIDER_ID);
    } catch {
      // Leave null if diagnostic RPC is unsupported
    }
  }

  const modelsPayload = await client.listProviderModels(PROVIDER_ID, { cwd: targetCwd });
  const modesPayload = await client.listProviderModes(PROVIDER_ID, { cwd: targetCwd });
  const discoveredModels = (modelsPayload?.models ?? []).map((m) => m.id);
  const discoveredModes = (modesPayload?.modes ?? []).map((m) => m.id);

  let smokeSummary = null;
  if (options.replayConversationId) {
    const conversationId = options.replayConversationId.trim();
    const providerHandleId = `plugin:${JSON.stringify({
      version: 1,
      data: { conversationId },
    })}`;
    const imported = await client.importAgent({
      providerId: PROVIDER_ID,
      providerHandleId,
      cwd: targetCwd,
    });
    smokeAgentId = imported.id;

    const timelineBeforeReload = await client.fetchAgentTimeline(smokeAgentId);
    await client.reloadPlugin(PLUGIN_ID);
    const refreshed = await client.refreshAgent(smokeAgentId);
    const timelineAfterReload = await client.fetchAgentTimeline(smokeAgentId);
    const pluginLogs = await client.getPluginLogs(PLUGIN_ID);
    const replayLogEntries = (pluginLogs ?? []).filter((entry) =>
      String(entry?.message ?? "").includes("Replayed"),
    );

    const remoteConvDir = options.target === "local" ? null : remoteConversationsDir(remoteSync);
    const persistedSnapshotInfo =
      options.target === "local"
        ? readLocalPersistedSnapshotInfo(conversationId)
        : await readRemotePersistedSnapshotInfo(client, remoteConvDir, conversationId);

    smokeSummary = {
      mode: "replay-only",
      agentId: smokeAgentId,
      conversationId,
      timelineItemsBeforeReload: (timelineBeforeReload?.items ?? timelineBeforeReload?.entries ?? []).length,
      persistedSnapshotInfo,
      refreshedStatus: refreshed?.status ?? "ok",
      timelineItemsAfterReload: (timelineAfterReload?.items ?? timelineAfterReload?.entries ?? []).length,
      replayLogEntries: replayLogEntries.slice(-5),
    };

    if (!options.keepAgent && smokeAgentId) {
      await archiveAndDeleteAgentSafely(client, smokeAgentId);
      smokeAgentId = null;
    }
  } else if (!options.skipSmoke) {
    const selectedModel = discoveredModels.includes(options.model)
      ? options.model
      : discoveredModels[0] ?? options.model;

    const created = await client.createAgent({
      provider: PROVIDER_ID,
      cwd: targetCwd,
      model: selectedModel,
      modeId: "full-access",
      title: "antigravity-resilient-smoke",
      initialPrompt: "Reply with the single word OK and do not call any tools.",
      idempotencyKey: randomUUID(),
    });
    smokeAgentId = created.id;

    const finishedAgent = await waitForAgentTurnCompletion(client, smokeAgentId);
    const timelineBeforeReload = await client.fetchAgentTimeline(smokeAgentId);

    await client.reloadPlugin(PLUGIN_ID);
    const refreshed = await client.refreshAgent(smokeAgentId);
    const timelineAfterReload = await client.fetchAgentTimeline(smokeAgentId);
    const pluginLogs = await client.getPluginLogs(PLUGIN_ID);
    const replayLogEntries = (pluginLogs ?? []).filter((entry) =>
      String(entry?.message ?? "").includes("Replayed"),
    );

    let conversationId = extractConversationId(finishedAgent);
    if (!conversationId && replayLogEntries.length > 0) {
      const lastMsg = String(replayLogEntries[replayLogEntries.length - 1]?.message ?? "");
      const match = /\bconversation\s+([A-Za-z0-9._-]+)/.exec(lastMsg);
      if (match?.[1]) {
        conversationId = match[1];
      }
    }

    let persistedSnapshotInfo = null;
    if (conversationId) {
      if (options.target === "local") {
        persistedSnapshotInfo = readLocalPersistedSnapshotInfo(conversationId);
      } else {
        const remoteConvDir = remoteConversationsDir(remoteSync);
        persistedSnapshotInfo = {
          snapshotPath: `${remoteConvDir}/${conversationId}.json`,
          eventsPath: `${remoteConvDir}/${conversationId}.jsonl`,
          verifiedViaReplayLog: replayLogEntries.length > 0,
        };
      }
    }

    smokeSummary = {
      mode: "live-turn",
      agentId: smokeAgentId,
      conversationId,
      model: selectedModel,
      statusAfterTurn: finishedAgent.status ?? finishedAgent.lifecycle,
      timelineItemsBeforeReload: (timelineBeforeReload?.items ?? timelineBeforeReload?.entries ?? []).length,
      persistedSnapshotInfo,
      refreshedStatus: refreshed?.status ?? "ok",
      timelineItemsAfterReload: (timelineAfterReload?.items ?? timelineAfterReload?.entries ?? []).length,
      replayLogEntries: replayLogEntries.slice(-5),
    };

    if (!options.keepAgent && smokeAgentId) {
      await archiveAndDeleteAgentSafely(client, smokeAgentId);
      smokeAgentId = null;
    }
  }

  console.log(
    JSON.stringify(
      {
        target: options.target,
        remoteSync,
        backupPath,
        pluginsInitiallyEnabled,
        plugin: {
          id: reloadedPlugin?.id ?? installedPlugin?.id,
          status: reloadedPlugin?.status ?? installedPlugin?.status,
          version: reloadedPlugin?.version ?? installedPlugin?.version,
          source: targetPluginPath,
        },
        providerStatus,
        discoveredModelCount: discoveredModels.length,
        discoveredModels,
        discoveredModes,
        smokeSummary,
      },
      null,
      2,
    ),
  );
} finally {
  if (!options.keepAgent && smokeAgentId) {
    await archiveAndDeleteAgentSafely(client, smokeAgentId);
  }
  await client.close();
}

async function archiveAndDeleteAgentSafely(daemonClient, agentId) {
  if (!agentId) {
    return;
  }
  try {
    await daemonClient.archiveAgent(agentId);
  } catch {
    // Best-effort archive first so connected Desktop UI sessions remove the agent entry
  }
  try {
    await daemonClient.deleteAgent(agentId);
  } catch {
    // Ignore cleanup errors
  }
}
