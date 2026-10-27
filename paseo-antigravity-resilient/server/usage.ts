import { execFile } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import {
  hashAccountKey,
  toneFromUsedPct,
  unavailable,
  windowFromUsedPct,
  type UsageAccount,
  type UsageDetail,
  type UsageReport,
  type UsageScope,
  type UsageWindow,
} from "@getpaseo/plugin/server/usage";
import { DEFAULT_KEYRING_TARGET, type UsageInput } from "../shared/input.ts";
import {
  readAntigravityCredential,
  type CredentialResult,
} from "./credentials.ts";
import { isRecord } from "./internal/wire.ts";

const execFileAsync = promisify(execFile) as (
  file: string,
  args: readonly string[],
  options: Record<string, unknown>,
) => Promise<{ stdout: string; stderr: string }>;

export const USAGE_SOURCE_ID = "antigravity-resilient";
export const USAGE_SOURCE_LABEL = "Antigravity";

/**
 * Provider ids whose sessions may surface this account. `antigravity` is the
 * CLI's bundled provider; `antigravity-resilient` is this plugin's.
 */
const AFFECTED_PROVIDERS = new Set(["antigravity", "antigravity-resilient"]);

/**
 * Fallback HTTP endpoint when `agy -p /usage --output-format json` is
 * unavailable. Note that `agy` itself queries the `daily-cloudcode-pa` quota
 * pool via its internal language server; `readCliQuotaFromAgy` is preferred
 * so Paseo matches `agy -p /usage` and `usage-float`.
 */
const QUOTA_URL =
  "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary";

/**
 * The endpoint rejects callers that do not look like the CLI (a plain
 * Gemini/HTTP user agent gets 403); `antigravity/<version>` is accepted.
 */
const USER_AGENT = "antigravity/1.0";

const FETCH_TIMEOUT_MS = 15_000;
const CLI_USAGE_TIMEOUT_MS = 20_000;
const CLI_MAX_BUFFER = 1024 * 1024;

/** `agy` owns token refresh, so a rejected/expired token is reported for it to fix. */
const REFRESHED_BY = "agy";

const WINDOW_LABELS: Record<string, { label: string; shortLabel: string }> = {
  weekly: { label: "Weekly", shortLabel: "wk" },
  week: { label: "Weekly", shortLabel: "wk" },
  "5h": { label: "5-hour", shortLabel: "5h" },
  five_hour: { label: "5-hour", shortLabel: "5h" },
  daily: { label: "Daily", shortLabel: "day" },
  day: { label: "Daily", shortLabel: "day" },
  monthly: { label: "Monthly", shortLabel: "mo" },
  month: { label: "Monthly", shortLabel: "mo" },
};

export type UsageDeps = {
  fetchApi?: typeof fetch;
  now?: () => number;
  readCredential?: (target: string) => Promise<CredentialResult>;
  readCliQuota?: () => Promise<unknown | null>;
};

export type QuotaBucket = {
  bucketId: string;
  displayName?: string;
  window?: string;
  resetTime?: string;
  remainingFraction: number;
};

export type QuotaGroup = {
  displayName?: string;
  description?: string;
  buckets: QuotaBucket[];
};

/**
 * One account: the machine-wide `agy` login. It is only listed when the
 * credential store actually holds a token, so a machine without Antigravity
 * sees no card at all.
 */
export async function discover(
  scope: UsageScope,
  deps: UsageDeps = {},
): Promise<UsageAccount[]> {
  if (scope.kind === "session" && !AFFECTED_PROVIDERS.has(scope.provider)) {
    return [];
  }
  const target = DEFAULT_KEYRING_TARGET;
  const read = deps.readCredential ?? readAntigravityCredential;
  const result = await read(target);
  if (!result.ok) {
    return [];
  }
  const { credential } = result;
  return [
    {
      key: hashAccountKey(target),
      ...(credential.email ? { label: credential.email } : {}),
      harness: USAGE_SOURCE_LABEL,
      input: { target } satisfies UsageInput,
    },
  ];
}

export async function fetchUsage(
  input: UsageInput,
  deps: UsageDeps = {},
): Promise<UsageReport> {
  const target = input?.target?.trim() || DEFAULT_KEYRING_TARGET;
  const read = deps.readCredential ?? readAntigravityCredential;
  const now = deps.now ?? Date.now;

  const result = await read(target);
  if (!result.ok) {
    return unavailable({
      kind: "no_quota",
      detail:
        result.problem.kind === "unsupported"
          ? `Reading the Antigravity login is not supported on this platform (${result.problem.platform}).`
          : "No Antigravity login found on this machine. Run `agy` and sign in.",
    });
  }

  const { credential } = result;
  const readCliQuota =
    deps.readCliQuota ??
    (deps.fetchApi === undefined ? readCliQuotaFromAgy : undefined);
  if (readCliQuota) {
    try {
      const cliPayload = await readCliQuota();
      if (cliPayload !== null && parseQuotaGroups(cliPayload).length > 0) {
        return reportFromQuotaResponse(cliPayload);
      }
    } catch {
      // Fall back to direct HTTP quota lookup if CLI execution fails.
    }
  }

  if (credential.expiresAt !== undefined && credential.expiresAt <= now()) {
    return unavailable({
      kind: "expired",
      expiresAt: new Date(credential.expiresAt).toISOString(),
      refreshedBy: REFRESHED_BY,
    });
  }

  const fetchApi = deps.fetchApi ?? fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let response: Awaited<ReturnType<typeof fetch>>;
  try {
    response = await fetchApi(QUOTA_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${credential.accessToken}`,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
      },
      body: "{}",
      signal: controller.signal,
    });
  } catch (error) {
    return {
      status: "error",
      error: `Antigravity quota request failed: ${describe(error)}`,
    };
  } finally {
    clearTimeout(timer);
  }

  if (response.status === 401 || response.status === 403) {
    return unavailable({
      kind: "rejected",
      status: response.status,
      refreshedBy: REFRESHED_BY,
    });
  }
  if (!response.ok) {
    return {
      status: "error",
      error: `Antigravity quota API returned ${response.status}`,
    };
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch (error) {
    return {
      status: "error",
      error: `Antigravity quota response was not JSON: ${describe(error)}`,
    };
  }
  return reportFromQuotaResponse(payload);
}

/**
 * Builds an `available` report from `retrieveUserQuotaSummary`, e.g.
 * `{"groups":[{"displayName":"Gemini Models","description":"Models within this
 * group: Gemini Flash, Gemini Pro","buckets":[{"bucketId":"gemini-weekly",
 * "displayName":"Weekly Limit Remaining","window":"weekly","resetTime":
 * "2026-10-22T16:06:07Z","remainingFraction":0.9470819}]}]}`.
 */
export function reportFromQuotaResponse(value: unknown): UsageReport {
  const groups = parseQuotaGroups(value);
  const windows = windowsFromGroups(groups);
  if (windows.length === 0) {
    // The response parsed but described nothing; make that greppable rather
    // than silently rendering an empty card.
    console.warn(
      "[antigravity-resilient] Antigravity quota response produced no windows",
    );
  }
  return {
    status: "available",
    windows,
    balances: [],
    details: detailsFromGroups(groups),
  };
}

export function windowsFromQuotaResponse(value: unknown): UsageWindow[] {
  return windowsFromGroups(parseQuotaGroups(value));
}

function windowsFromGroups(groups: QuotaGroup[]): UsageWindow[] {
  const windows: UsageWindow[] = [];
  const taken = new Set<string>();
  for (const group of groups) {
    const groupName = group.displayName ?? USAGE_SOURCE_LABEL;
    const shortGroup = shortGroupName(groupName);
    for (const bucket of group.buckets) {
      const scope = windowScope(bucket);
      const usedPct = (1 - clamp01(bucket.remainingFraction)) * 100;
      windows.push(
        windowFromUsedPct({
          id: uniqueId(bucket.bucketId, taken),
          label: `${groupName} · ${scope.label}`,
          shortLabel: `${shortGroup} ${scope.shortLabel}`.trim(),
          summary: scope.shortLabel === "5h",
          utilizationPct: usedPct,
          resetsAt: bucket.resetTime ?? null,
          tone: toneFromUsedPct(usedPct),
        }),
      );
    }
  }
  return windows;
}

function detailsFromGroups(groups: QuotaGroup[]): UsageDetail[] {
  const details: UsageDetail[] = [];
  groups.forEach((group, index) => {
    const value = group.description
      ?.replace(/^models within this group:\s*/iu, "")
      .trim();
    if (!value) {
      return;
    }
    details.push({
      id: `group:${index}`,
      label: group.displayName ?? `Group ${index + 1}`,
      value,
    });
  });
  return details;
}

export function parseQuotaGroups(value: unknown): QuotaGroup[] {
  const root = unwrapQuotaRoot(value);
  if (!isRecord(root) || !Array.isArray(root.groups)) {
    return [];
  }
  const groups: QuotaGroup[] = [];
  for (const rawGroup of root.groups) {
    if (!isRecord(rawGroup)) {
      continue;
    }
    const buckets: QuotaBucket[] = [];
    for (const rawBucket of Array.isArray(rawGroup.buckets)
      ? rawGroup.buckets
      : []) {
      if (!isRecord(rawBucket)) {
        continue;
      }
      const bucketId =
        asString(rawBucket.bucketId) ?? asString(rawBucket.id);
      const remainingFraction =
        asNumber(rawBucket.remainingFraction) ??
        asNumber(rawBucket.remaining_fraction);
      if (!bucketId || remainingFraction === null) {
        continue;
      }
      const displayName =
        asString(rawBucket.displayName) ?? asString(rawBucket.name);
      const window = asString(rawBucket.window);
      const resetTime =
        asString(rawBucket.resetTime) ?? asString(rawBucket.reset_time);
      buckets.push({
        bucketId,
        remainingFraction,
        ...(displayName ? { displayName } : {}),
        ...(window ? { window } : {}),
        ...(resetTime ? { resetTime } : {}),
      });
    }
    if (buckets.length === 0) {
      continue;
    }
    const displayName =
      asString(rawGroup.displayName) ?? asString(rawGroup.name);
    const description = asString(rawGroup.description);
    groups.push({
      buckets,
      ...(displayName ? { displayName } : {}),
      ...(description ? { description } : {}),
    });
  }
  return groups;
}

function unwrapQuotaRoot(value: unknown): unknown {
  if (
    isRecord(value) &&
    isRecord(value.command) &&
    isRecord(value.command.data) &&
    Array.isArray(value.command.data.groups)
  ) {
    return value.command.data;
  }
  return value;
}

function resolveAgyCandidates(platform: string = process.platform): string[] {
  const candidates: string[] = [];
  if (platform === "win32") {
    const localAppData =
      process.env.LOCALAPPDATA?.trim() ||
      path.join(os.homedir(), "AppData", "Local");
    const agyExe = path.join(localAppData, "agy", "bin", "agy.exe");
    if (fs.existsSync(agyExe)) {
      candidates.push(agyExe);
    }
    candidates.push("agy.exe");
  }
  candidates.push("agy");
  return candidates;
}

export async function readCliQuotaFromAgy(): Promise<unknown | null> {
  for (const bin of resolveAgyCandidates()) {
    try {
      const { stdout } = await execFileAsync(
        bin,
        ["-p", "/usage", "--output-format", "json"],
        {
          encoding: "utf8",
          timeout: CLI_USAGE_TIMEOUT_MS,
          maxBuffer: CLI_MAX_BUFFER,
          windowsHide: true,
        },
      );
      const trimmed = String(stdout ?? "").trim();
      const firstBrace = trimmed.indexOf("{");
      const lastBrace = trimmed.lastIndexOf("}");
      if (firstBrace < 0 || lastBrace <= firstBrace) {
        continue;
      }
      const parsed: unknown = JSON.parse(trimmed.slice(firstBrace, lastBrace + 1));
      if (parseQuotaGroups(parsed).length > 0) {
        return parsed;
      }
    } catch {
      continue;
    }
  }
  return null;
}

function windowScope(bucket: QuotaBucket): {
  label: string;
  shortLabel: string;
} {
  const dash = bucket.bucketId.indexOf("-");
  const suffix = dash >= 0 ? bucket.bucketId.slice(dash + 1) : undefined;
  for (const candidate of [bucket.window, suffix, bucket.bucketId]) {
    const key = candidate?.trim().toLowerCase();
    if (key && WINDOW_LABELS[key]) {
      return WINDOW_LABELS[key];
    }
  }
  const cleaned = (bucket.displayName ?? bucket.bucketId)
    .replace(/\s*limit remaining\s*$/iu, "")
    .trim();
  return { label: cleaned || bucket.bucketId, shortLabel: "" };
}

function shortGroupName(name: string): string {
  if (/gemini/iu.test(name)) {
    return "Gemini";
  }
  if (/claude/iu.test(name)) {
    return /gpt/iu.test(name) ? "Claude+GPT" : "Claude";
  }
  const first = name.trim().split(/\s+/u)[0] ?? "";
  return first || name;
}

function uniqueId(candidate: string, taken: Set<string>): string {
  let id = candidate;
  let suffix = 2;
  while (taken.has(id)) {
    id = `${candidate}-${suffix}`;
    suffix += 1;
  }
  taken.add(id);
  return id;
}

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
