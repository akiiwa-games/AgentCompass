import { execFile, spawn, type ChildProcess, type SpawnOptions } from "node:child_process";
import { createHash } from "node:crypto";
import { extname } from "node:path";
import { promisify } from "node:util";
import type {
  ProviderCapability,
  ProviderInput,
} from "@getpaseo/plugin/server/provider";
import type {
  UsageProblem,
  UsageReport,
  UsageTone,
  UsageWindow,
} from "@getpaseo/plugin/server/usage";

const execFileAsync = promisify(execFile);

export const PROVIDER_PROTOCOL_VERSION = 1;

export const PROVIDER_CAPABILITIES: readonly ProviderCapability[] = [
  "prompt.message",
  "prompt.command",
  "prompt.image",
  "prompt.output_schema",
  "prompt.steer",
  "session.archive",
  "session.configure",
  "session.list",
  "session.persistence",
  "session.revert.both",
  "session.revert.conversation",
  "session.revert.files",
  "session.subsession",
  "session.unarchive",
  "permission",
  "permission.tool_policy",
  "timeline.plugin",
];

export function negotiateProviderCapabilities(
  offered: readonly ProviderCapability[],
  supported: readonly ProviderCapability[],
): ProviderCapability[] {
  const known = new Set<string>(PROVIDER_CAPABILITIES);
  const supportedSet = new Set<ProviderCapability>(
    supported.filter((cap) => known.has(cap)),
  );
  return offered.filter(
    (cap, idx) =>
      known.has(cap) && supportedSet.has(cap) && offered.indexOf(cap) === idx,
  );
}

function requiredCapabilitiesForInput(input: ProviderInput): ProviderCapability[] {
  switch (input.type) {
    case "catalog":
    case "session.interrupt":
    case "session.close":
      return [];
    case "sessions":
      return ["session.list"];
    case "session.open": {
      const caps: ProviderCapability[] = [];
      if (input.persistence) {
        caps.push("session.persistence");
      }
      if (input.config.toolPolicy) {
        caps.push("permission.tool_policy");
      }
      return caps;
    }
    case "session.prompt": {
      const caps: ProviderCapability[] = [];
      if (input.prompt.delivery === "steer") {
        caps.push("prompt.steer");
      }
      if (input.prompt.input.type === "command") {
        caps.push("prompt.command");
      } else {
        caps.push("prompt.message");
        if (input.prompt.input.content.some((part) => part.type === "image")) {
          caps.push("prompt.image");
        }
      }
      if (input.prompt.outputSchema !== undefined) {
        caps.push("prompt.output_schema");
      }
      return caps;
    }
    case "session.permission":
      return ["permission"];
    case "session.configure":
      return ["session.configure"];
    case "session.revert":
      return [`session.revert.${input.scope}`];
    case "session.archive":
      return ["session.archive"];
    case "session.unarchive":
      return ["session.unarchive"];
  }
}

export function requireProviderCapabilities(
  capabilities: readonly ProviderCapability[],
  input: ProviderInput,
): void {
  for (const cap of requiredCapabilitiesForInput(input)) {
    if (!capabilities.includes(cap)) {
      throw new Error(`Provider does not support ${cap}`);
    }
  }
}

function shouldUseShell(command: string, requestedShell?: boolean | string): boolean | string {
  if (/\.(cmd|bat)$/i.test(command)) {
    return true;
  }
  if (requestedShell !== undefined) {
    return requestedShell;
  }
  const hasSep = command.includes("/") || command.includes("\\");
  return process.platform === "win32" && !hasSep && !extname(command);
}

export function spawnProcess(
  command: string,
  args: readonly string[],
  options?: SpawnOptions,
) {
  return spawn(command, args, {
    ...options,
    shell: shouldUseShell(command, options?.shell),
    windowsHide: true,
  });
}

export async function execCommand(
  command: string,
  args: readonly string[],
  options?: {
    cwd?: string;
    env?: Record<string, string | undefined>;
    encoding?: string;
    signal?: AbortSignal;
    killSignal?: string | number;
    timeout?: number;
    maxBuffer?: number;
    shell?: boolean;
  },
): Promise<{ stdout: string; stderr: string }> {
  const res = await execFileAsync(command, [...args], {
    cwd: options?.cwd,
    env: options?.env as NodeJS.ProcessEnv | undefined,
    encoding: "utf8",
    signal: options?.signal,
    timeout: options?.timeout,
    maxBuffer: options?.maxBuffer,
    shell: Boolean(shouldUseShell(command, options?.shell)),
    windowsHide: true,
  });
  return {
    stdout: String(res.stdout ?? ""),
    stderr: String(res.stderr ?? ""),
  };
}

export async function terminateProcess(
  child: ChildProcess,
  signal: "SIGTERM" | "SIGKILL" | string = "SIGKILL",
): Promise<void> {
  try {
    child.kill(signal as NodeJS.Signals);
  } catch {
    // Ignore already exited child.
  }
}

// ---------------------------------------------------------------------------
// `@getpaseo/plugin/server/usage` helpers, mirrored from the host runtime so
// the usage source can be exercised without the Paseo daemon.
// ---------------------------------------------------------------------------

export function windowFromUsedPct(input: {
  id: string;
  label: string;
  shortLabel?: string;
  summary?: boolean;
  utilizationPct?: number | null;
  resetsAt?: string | null;
  tone?: UsageTone;
}): UsageWindow {
  const usedPct =
    typeof input.utilizationPct === "number" ? input.utilizationPct : null;
  const window: UsageWindow = {
    id: input.id,
    label: input.label,
    usedPct,
    remainingPct: usedPct === null ? null : Math.max(0, 100 - usedPct),
    resetsAt: input.resetsAt ?? null,
  };
  if (input.shortLabel !== undefined) {
    window.shortLabel = input.shortLabel;
  }
  if (input.summary) {
    window.summary = true;
  }
  if (input.tone) {
    window.tone = input.tone;
  }
  return window;
}

export function toneFromUsedPct(usedPct: number | null | undefined): UsageTone {
  if (typeof usedPct !== "number") {
    return "default";
  }
  if (usedPct > 90) {
    return "danger";
  }
  if (usedPct >= 70) {
    return "warning";
  }
  return "ok";
}

export function balanceToneFromRemaining(
  remaining: number | null | undefined,
): UsageTone {
  if (typeof remaining !== "number") {
    return "default";
  }
  if (remaining <= 0) {
    return "danger";
  }
  return "ok";
}

export function usedPctOf(
  used: number | null | undefined,
  limit: number | null | undefined,
): number | null {
  if (typeof used !== "number" || typeof limit !== "number" || limit <= 0) {
    return null;
  }
  return (used / limit) * 100;
}

export function hashAccountKey(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

export function unavailable(problem: UsageProblem): UsageReport {
  return { status: "unavailable", problem };
}
