import type {
  ProviderCapability,
  ProviderCatalog,
  ProviderCatalogMode,
  ProviderCatalogModel,
  ProviderHost,
} from "@getpaseo/plugin/server/provider";
import { createHash } from "node:crypto";
import { statSync } from "node:fs";
import path from "node:path";
import { probe } from "./process.ts";
import { ProviderActionError } from "./signals.ts";
import { diagnostic } from "./wire.ts";

export type ProviderDiscovery = ProviderCatalog;

export const DEFAULT_MODE = "full-access";
export const DISCOVERY_TIMEOUT_MS = 20_000;
export const DISCOVERY_RETRY_DELAY_MS = 1_500;

export const SUPPORTED_CAPABILITIES: ProviderCapability[] = [
  "prompt.message",
  "prompt.command",
  "prompt.image",
  "session.persistence",
  "session.configure",
];

export const MODES: ProviderCatalogMode[] = [
  {
    id: "full-access",
    label: "Full access",
    description:
      "Antigravity cannot ask for permission when another app drives it. Paseo starts it with --dangerously-skip-permissions.",
    icon: "ShieldOff",
    colorTier: "dangerous",
    isUnattended: true,
  },
];

export function resolveCommandFingerprint(
  command: string,
  env?: Record<string, string>,
): string {
  const candidates: string[] = [];
  if (command.includes("/") || command.includes("\\") || path.isAbsolute(command)) {
    candidates.push(command);
  } else {
    const pathEnv = env?.PATH ?? env?.Path ?? process.env.PATH ?? process.env.Path ?? "";
    const dirs = pathEnv.split(path.delimiter).filter(Boolean);
    const exts =
      process.platform === "win32"
        ? ["", ".cmd", ".exe", ".bat"]
        : [""];
    for (const dir of dirs) {
      for (const ext of exts) {
        candidates.push(path.join(dir, `${command}${ext}`));
      }
    }
  }
  for (const candidate of candidates) {
    try {
      const stat = statSync(candidate);
      if (stat.isFile()) {
        return `${candidate}:${Math.trunc(stat.mtimeMs)}:${stat.size}`;
      }
    } catch {
      // Continue searching candidates.
    }
  }
  return command;
}

export function getCatalogCacheKey(
  command: string,
  args: string[],
  env?: Record<string, string>,
): string {
  const fingerprint = resolveCommandFingerprint(command, env);
  return createHash("sha256")
    .update(
      JSON.stringify([
        "antigravity-resilient-v2",
        command,
        fingerprint,
        args,
        Object.entries(env ?? {}).sort(([left], [right]) =>
          left.localeCompare(right),
        ),
      ]),
    )
    .digest("hex");
}

/**
 * Input token limit per model family. `agy models` reports only ids and
 * labels, so the window is derived from the family name; a session can
 * override it with `providerOptions.contextWindowMaxTokens`.
 *
 * Gemini's own limit is 1M, but agy runs every conversation in its default
 * `256K` context-window preset (`512K`/`900K` are paid upgrades in the model
 * selector) and checkpoints natively at ~230-250K, so 256K is what the
 * meter should measure against.
 */
export const AGY_DEFAULT_CONTEXT_WINDOW_TOKENS = 262_144;

const FAMILY_CONTEXT_WINDOWS: ReadonlyArray<{
  pattern: RegExp;
  maxTokens: number;
}> = [
  { pattern: /^gemini-/iu, maxTokens: AGY_DEFAULT_CONTEXT_WINDOW_TOKENS },
  { pattern: /^claude-/iu, maxTokens: 200_000 },
  { pattern: /^gpt-oss-/iu, maxTokens: 131_072 },
];

export const DEFAULT_CONTEXT_WINDOW_MAX_TOKENS = AGY_DEFAULT_CONTEXT_WINDOW_TOKENS;

export function resolveContextWindowMaxTokens(model: string | undefined): number {
  const id = model?.trim() ?? "";
  for (const entry of FAMILY_CONTEXT_WINDOWS) {
    if (entry.pattern.test(id)) {
      return entry.maxTokens;
    }
  }
  return DEFAULT_CONTEXT_WINDOW_MAX_TOKENS;
}

/**
 * Fills in `contextWindowMaxTokens` for models that do not carry one, so the
 * daemon can advertise a window even for catalogues cached by older builds.
 * Explicit values (from `agy` or a hand-written catalogue) win.
 */
export function withContextWindows(
  models: ProviderCatalogModel[],
): ProviderCatalogModel[] {
  return models.map((model) =>
    typeof model.contextWindowMaxTokens === "number" &&
    model.contextWindowMaxTokens > 0
      ? model
      : {
          ...model,
          contextWindowMaxTokens: resolveContextWindowMaxTokens(model.id),
        },
  );
}

export function parseModels(stdout: string): ProviderCatalogModel[] {
  const seen = new Set<string>();
  const models: ProviderCatalogModel[] = [];

  for (const rawLine of stdout.split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (!line || !line.includes("\t")) {
      continue;
    }
    const [rawId = "", ...labelParts] = line.split("\t");
    const id = rawId.trim();
    const label = labelParts.join("\t").trim();
    if (!id || seen.has(id)) {
      continue;
    }
    seen.add(id);
    models.push({
      id,
      label: label || id,
      isDefault: models.length === 0,
    });
  }

  return models;
}

export function isTransientDiscoveryFailure(result: {
  exitCode: number | null;
  stderr: string;
  stdout: string;
  timedOut?: boolean;
}): boolean {
  if (result.timedOut) {
    return true;
  }
  const combined = `${result.stderr}\n${result.stdout}`;
  return (
    /\bUNAVAILABLE\b/i.test(combined) ||
    /\bcode\s*50[0234]\b/i.test(combined) ||
    /\bINTERNAL\b/i.test(combined) ||
    /TLS handshake timeout/i.test(combined) ||
    /\bEOF\b/.test(combined) ||
    /connection reset/i.test(combined)
  );
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (ms <= 0) {
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    if (signal) {
      signal.addEventListener(
        "abort",
        () => {
          clearTimeout(timer);
          resolve();
        },
        { once: true },
      );
    }
  });
}

export type GetCatalogOptions = {
  bypassCache?: boolean;
  fallbackCatalog?: ProviderDiscovery | null;
  probeImpl?: typeof probe;
  probeTimeoutMs?: number;
  retryDelayMs?: number;
  signal?: AbortSignal;
};

export async function getCatalog(
  host: ProviderHost | undefined,
  command: string,
  args: string[],
  env?: Record<string, string>,
  options: GetCatalogOptions = {},
): Promise<ProviderDiscovery> {
  const cacheKey = getCatalogCacheKey(command, args, env);
  const cached = host ? await host.readCatalogCache(cacheKey) : null;
  if (!options.bypassCache && cached && cached.models.length > 0) {
    return withWindows(cached);
  }

  const probeFn = options.probeImpl ?? probe;
  const timeoutMs = options.probeTimeoutMs ?? DISCOVERY_TIMEOUT_MS;
  const retryDelayMs = options.retryDelayMs ?? DISCOVERY_RETRY_DELAY_MS;
  const signal = options.signal;
  const fallback =
    (options.fallbackCatalog && options.fallbackCatalog.models.length > 0
      ? options.fallbackCatalog
      : null) ?? (cached && cached.models.length > 0 ? cached : null);

  let result = await probeFn(
    command,
    [...args, "models"],
    env,
    timeoutMs,
    signal,
  );

  if (result.exitCode !== 0 && isTransientDiscoveryFailure(result) && !signal?.aborted) {
    console.warn(
      `[antigravity-resilient] Model discovery failed transiently (${result.timedOut ? "timeout" : `exit=${result.exitCode}`}); retrying once in ${retryDelayMs}ms`,
    );
    await sleep(retryDelayMs, signal);
    result = await probeFn(
      command,
      [...args, "models"],
      env,
      timeoutMs,
      signal,
    );
  }

  if (result.timedOut) {
    if (fallback) {
      console.warn(
        "[antigravity-resilient] Model discovery timed out after retry; using last valid cached catalog",
      );
      return withWindows(fallback);
    }
    throw new ProviderActionError(
      "PROBE_TIMEOUT",
      "Antigravity discovery timed out",
    );
  }

  if (result.exitCode !== 0) {
    if (fallback && isTransientDiscoveryFailure(result)) {
      console.warn(
        "[antigravity-resilient] Model discovery returned transient error after retry; using last valid cached catalog",
      );
      return withWindows(fallback);
    }
    const detail = diagnostic(
      result.stderr.trim() ||
        result.stdout.trim() ||
        `agy models exited with code ${result.exitCode ?? "unknown"}`,
    );
    throw new ProviderActionError("DISCOVERY_FAILED", detail, detail);
  }

  const models = parseModels(result.stdout);
  if (models.length === 0) {
    if (fallback) {
      return withWindows(fallback);
    }
    throw new ProviderActionError(
      "DISCOVERY_FAILED",
      "Antigravity CLI returned no models from `agy models`.",
    );
  }

  const catalog: ProviderDiscovery = {
    models: withContextWindows(models),
    modes: MODES,
    thinkingOptions: [],
    defaultModel: models[0]?.id,
    defaultMode: DEFAULT_MODE,
  };
  if (host) {
    await host.writeCatalogCache(cacheKey, catalog);
  }
  return catalog;
}

function withWindows(catalog: ProviderDiscovery): ProviderDiscovery {
  if (
    catalog.models.every(
      (model) =>
        typeof model.contextWindowMaxTokens === "number" &&
        model.contextWindowMaxTokens > 0,
    )
  ) {
    return catalog;
  }
  return { ...catalog, models: withContextWindows(catalog.models) };
}
