declare var process: {
  platform: string;
  pid: number;
  env: Record<string, string | undefined>;
  argv: string[];
  exitCode?: number;
  cwd(): string;
  exit(code?: number): never;
};

declare interface Buffer extends Uint8Array {
  toString(encoding?: string, start?: number, end?: number): string;
  equals(other: Uint8Array): boolean;
  subarray(start?: number, end?: number): Buffer;
}

declare var Buffer: {
  from(input: string | Uint8Array, encoding?: string): Buffer;
  alloc(size: number): Buffer;
  allocUnsafe(size: number): Buffer;
  concat(list: readonly Uint8Array[], totalLength?: number): Buffer;
  isBuffer(value: unknown): boolean;
};

declare function setTimeout(callback: (...args: any[]) => void, ms?: number): unknown;
declare function clearTimeout(handle: unknown): void;

declare var console: {
  log(...args: unknown[]): void;
  warn(...args: unknown[]): void;
  error(...args: unknown[]): void;
  info(...args: unknown[]): void;
};

declare interface AbortSignal {
  readonly aborted: boolean;
  readonly reason?: unknown;
  addEventListener(type: "abort", listener: () => void, options?: { once?: boolean }): void;
  removeEventListener(type: "abort", listener: () => void): void;
}

declare class AbortController {
  readonly signal: AbortSignal;
  abort(reason?: unknown): void;
}

declare module "node:crypto" {
  export interface Hash {
    update(data: string | Uint8Array): this;
    digest(encoding: "hex" | "base64" | "base64url"): string;
  }
  export function createHash(algorithm: string): Hash;
  export function randomUUID(): string;
}

declare module "node:events" {
  export class EventEmitter {
    on(event: string, listener: (...args: any[]) => void): this;
    once(event: string, listener: (...args: any[]) => void): this;
    off(event: string, listener: (...args: any[]) => void): this;
    emit(event: string, ...args: any[]): boolean;
    removeAllListeners(event?: string): this;
  }
}

declare module "node:stream" {
  import { EventEmitter } from "node:events";
  export class Readable extends EventEmitter {
    setEncoding(encoding: string): this;
  }
  export class Writable extends EventEmitter {
    writable: boolean;
    destroyed: boolean;
    write(chunk: string | Uint8Array, callback?: (error?: Error | null) => void): boolean;
    end(chunk?: string | Uint8Array, callback?: () => void): this;
  }
  export class PassThrough extends Writable {
    setEncoding(encoding: string): this;
  }
}

declare namespace NodeJS {
  export interface ErrnoException extends Error {
    errno?: number;
    code?: string | number;
    path?: string;
    syscall?: string;
  }
  export type ProcessEnv = Record<string, string | undefined>;
  export type Signals = string;
}

declare function queueMicrotask(callback: () => void): void;

declare module "node:util" {
  export function promisify<T = any>(fn: (...args: any[]) => any): (...args: any[]) => Promise<T>;
}

declare module "node:child_process" {
  import { EventEmitter } from "node:events";
  import { Readable, Writable } from "node:stream";
  export interface ChildProcess extends EventEmitter {
    pid?: number;
    stdin: Writable | null;
    stdout: Readable | null;
    stderr: Readable | null;
    exitCode: number | null;
    signalCode: string | null;
    killed: boolean;
    kill(signal?: string | number): boolean;
  }
  export interface ChildProcessByStdio<I, O, E> extends ChildProcess {
    stdin: I;
    stdout: O;
    stderr: E;
  }
  export interface SpawnOptions {
    cwd?: string;
    env?: Record<string, string | undefined>;
    stdio?: any;
    shell?: boolean | string;
    windowsHide?: boolean;
    signal?: AbortSignal;
  }
  export function spawn(
    command: string,
    args: readonly string[],
    options?: SpawnOptions,
  ): ChildProcessByStdio<Writable, Readable, Readable>;
  export function execFile(
    file: string,
    args: readonly string[],
    options: Record<string, unknown>,
    callback: (error: Error | null, stdout: string, stderr: string) => void,
  ): ChildProcess;
}

declare module "node:readline" {
  import { EventEmitter } from "node:events";
  import { Readable } from "node:stream";
  export interface Interface extends EventEmitter {
    close(): void;
    on(event: "line", listener: (line: string) => void): this;
    on(event: string, listener: (...args: any[]) => void): this;
  }
  export function createInterface(options: {
    input: Readable;
    crlfDelay?: number;
  }): Interface;
}

declare module "node:fs" {
  export interface Stats {
    isFile(): boolean;
    isDirectory(): boolean;
    ino: number;
    size: number;
    mtimeMs: number;
  }
  export function existsSync(path: string): boolean;
  export function mkdirSync(path: string, options?: { recursive?: boolean }): string | undefined;
  export function readFileSync(path: string, encoding: "utf8" | "utf-8"): string;
  export function writeFileSync(
    path: string,
    data: string | Uint8Array,
    options?: "utf8" | "utf-8" | { encoding?: string; mode?: number; flag?: string },
  ): void;
  export function appendFileSync(
    path: string,
    data: string | Uint8Array,
    options?: "utf8" | "utf-8" | { encoding?: string; mode?: number },
  ): void;
  export function renameSync(oldPath: string, newPath: string): void;
  export function copyFileSync(src: string, dest: string): void;
  export function unlinkSync(path: string): void;
  export function statSync(path: string): Stats;
  export function openSync(path: string, flags: string): number;
  export function readSync(fd: number, buffer: Uint8Array, offset: number, length: number, position: number | null): number;
  export function closeSync(fd: number): void;
  export function readdirSync(path: string): string[];
  export function rmSync(path: string, options?: { recursive?: boolean; force?: boolean }): void;
  export function mkdtempSync(prefix: string): string;
}

declare module "node:os" {
  export function homedir(): string;
  export function tmpdir(): string;
}

declare module "node:path" {
  export const sep: string;
  export const delimiter: string;
  export function join(...paths: string[]): string;
  export function resolve(...paths: string[]): string;
  export function dirname(p: string): string;
  export function basename(p: string, ext?: string): string;
  export function extname(p: string): string;
  export function relative(from: string, to: string): string;
  export function isAbsolute(p: string): boolean;
}

declare module "node:url" {
  export function fileURLToPath(url: string | URL): string;
  export function pathToFileURL(path: string): URL;
}

declare module "node:assert/strict" {
  export function equal<T>(actual: unknown, expected: T, message?: string): asserts actual is T;
  export function deepEqual<T>(actual: unknown, expected: T, message?: string): asserts actual is T;
  export function ok(value: unknown, message?: string): asserts value;
  export function fail(message?: string | Error): never;
  export function match(string: string, regexp: RegExp, message?: string): void;
  export function doesNotMatch(string: string, regexp: RegExp, message?: string): void;
  export function throws(
    block: () => unknown,
    error?: RegExp | ((err: unknown) => boolean),
    message?: string,
  ): void;
  export function rejects(
    block: (() => Promise<unknown>) | Promise<unknown>,
    error?: RegExp | ((err: any) => boolean),
    message?: string,
  ): Promise<void>;
}

declare module "node:test" {
  export interface TestContext {
    name: string;
  }
  export function test(name: string, fn: (t: TestContext) => void | Promise<void>): Promise<void>;
  export function describe(name: string, fn: () => void | Promise<void>): Promise<void>;
  export function it(name: string, fn: (t: TestContext) => void | Promise<void>): Promise<void>;
}

declare module "@getpaseo/plugin/server" {
  import type { ChildProcess, ChildProcessByStdio, SpawnOptions } from "node:child_process";
  import type { Readable, Writable } from "node:stream";
  import type { ProviderDefinition } from "@getpaseo/plugin/server/provider";
  import type {
    UsageAccount,
    UsageReport,
    UsageScope,
  } from "@getpaseo/plugin/server/usage";

  export interface UsageSourceDefinition {
    id: string;
    label: string;
    icon?: string;
    /**
     * The host validates the account locator with `parseAsync` before handing
     * it to `fetch`; it is never serialized to the client.
     */
    input: { parseAsync(value: unknown): Promise<unknown> };
    discover(scope: UsageScope): Promise<UsageAccount[]> | UsageAccount[];
    fetch(input: any): Promise<UsageReport> | UsageReport;
  }

  export interface PluginServerContext {
    registerProvider(definition: ProviderDefinition): void;
    registerUsageSource(source: UsageSourceDefinition): void;
  }

  export function spawnProcess(
    command: string,
    args: readonly string[],
    options?: SpawnOptions,
  ): ChildProcessByStdio<Writable, Readable, Readable>;

  export function execCommand(
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
  ): Promise<{ stdout: string; stderr: string }>;

  export function terminateProcess(
    child: ChildProcess,
    signal?: "SIGTERM" | "SIGKILL" | string,
  ): Promise<void>;
}

declare module "@getpaseo/plugin/server/provider" {
  export type ProviderCapability =
    | "prompt.message"
    | "prompt.command"
    | "prompt.image"
    | "prompt.output_schema"
    | "prompt.steer"
    | "session.archive"
    | "session.configure"
    | "session.list"
    | "session.persistence"
    | "session.revert.both"
    | "session.revert.conversation"
    | "session.revert.files"
    | "session.subsession"
    | "session.unarchive"
    | "permission"
    | "permission.tool_policy"
    | "timeline.plugin";

  export interface ProviderLaunchConfig {
    command: string;
    args: string[];
    env: Record<string, string>;
  }

  export type ProviderStatusResult =
    | { available: true; diagnostic?: string }
    | { available: false; diagnostic: string };

  export interface ProviderStatusContext {
    launch?: ProviderLaunchConfig;
  }

  export interface ProviderCatalogCacheKeyContext {
    launch?: ProviderLaunchConfig;
  }

  export interface ProviderCatalogModel {
    id: string;
    label: string;
    aliases?: string[];
    isSelectable?: boolean;
    description?: string;
    isDefault?: boolean;
    metadata?: Record<string, unknown>;
    contextWindowMaxTokens?: number;
  }

  export interface ProviderCatalogMode {
    id: string;
    label: string;
    description?: string;
    icon?: string;
    colorTier?: string;
    isUnattended?: boolean;
  }

  export interface ProviderCatalogSelectOption {
    id: string;
    label: string;
    description?: string;
    isDefault?: boolean;
    metadata?: Record<string, unknown>;
  }

  export interface ProviderCatalog {
    models: ProviderCatalogModel[];
    modes: ProviderCatalogMode[];
    thinkingOptions?: ProviderCatalogSelectOption[];
    defaultModel?: string;
    defaultMode?: string;
    defaultThinkingOption?: string;
  }

  export interface ProviderHost {
    readCatalogCache(key: string): Promise<ProviderCatalog | null>;
    writeCatalogCache(key: string, catalog: ProviderCatalog): Promise<void>;
  }

  export interface ProviderConnectContext {
    versions?: readonly number[];
    protocolVersion?: number;
    capabilities: readonly ProviderCapability[];
    launch?: ProviderLaunchConfig;
    host?: ProviderHost;
    emit?(event: ProviderEvent): void;
  }

  export interface ProviderPersistence {
    version: number;
    data: unknown;
  }

  export interface ProviderSessionConfig {
    cwd: string;
    env: Record<string, string>;
    systemPrompt?: string;
    mcpServers: Record<string, unknown>;
    toolPolicy?: unknown;
    model?: string;
    mode?: string;
    thinkingOption?: string;
    settings: Record<string, unknown>;
    providerOptions?: Record<string, unknown>;
    title?: string;
    persist: boolean;
  }

  export type ProviderContentPart =
    | { type: "text"; text: string; mimeType?: string; contextKind?: string; title?: string | null }
    | { type: "image"; data: string; mimeType: string }
    | { type: "uploaded_file"; id: string; fileName: string; mimeType: string; size: number; path: string }
    | {
        type: "review";
        mimeType: "application/paseo-review";
        cwd: string;
        mode: "uncommitted" | "base";
        baseRef?: string | null;
        comments: Array<{
          filePath: string;
          side: "old" | "new";
          lineNumber: number;
          body: string;
          context: {
            hunkHeader: string;
            targetLine: {
              oldLineNumber: number | null;
              newLineNumber: number | null;
              type: "add" | "remove" | "context";
              content: string;
            };
            lines: Array<{
              oldLineNumber: number | null;
              newLineNumber: number | null;
              type: "add" | "remove" | "context";
              content: string;
            }>;
          };
        }>;
      }
    | {
        type: "github_pr" | "forge_change_request";
        mimeType: string;
        forge?: string;
        number: number;
        title: string;
        url: string;
        body?: string | null;
        projectPath?: string;
        baseRefName?: string | null;
        headRefName?: string | null;
      }
    | {
        type: "github_issue" | "forge_issue";
        mimeType: string;
        forge?: string;
        number: number;
        title: string;
        url: string;
        body?: string | null;
        projectPath?: string;
      };

  export interface ProviderPrompt {
    clientMessageId: string;
    delivery: "auto" | "steer";
    input:
      | { type: "message"; content: ProviderContentPart[] }
      | { type: "command"; name: string; arguments: string };
    outputSchema?: unknown;
    clearPendingPermissions?: boolean;
  }

  export interface ProviderConfigChanges {
    model?: string | null;
    mode?: string | null;
    thinkingOption?: string | null;
    settings?: Record<string, unknown>;
  }

  export type ProviderInput =
    | { type: "catalog"; requestId: string; cwd?: string }
    | { type: "sessions"; requestId: string; query?: string; cwd?: string; limit?: number }
    | {
        type: "session.open";
        requestId: string;
        sessionId: string;
        config: ProviderSessionConfig;
        persistence?: ProviderPersistence;
        history: "replay" | "skip";
      }
    | { type: "session.prompt"; sessionId: string; prompt: ProviderPrompt }
    | { type: "session.interrupt"; requestId: string; sessionId: string }
    | { type: "session.permission"; sessionId: string; permissionId: string; response: unknown }
    | {
        type: "session.configure";
        requestId: string;
        sessionId: string;
        changes: ProviderConfigChanges;
      }
    | {
        type: "session.revert";
        requestId: string;
        sessionId: string;
        token: unknown;
        scope: "conversation" | "files" | "both";
      }
    | { type: "session.archive"; requestId: string; persistence: ProviderPersistence }
    | { type: "session.unarchive"; requestId: string; persistence: ProviderPersistence }
    | { type: "session.close"; requestId: string; sessionId: string };

  export type ProviderToolCallDetail =
    | { type: "shell"; command: string; cwd?: string; output?: string; exitCode?: number | null }
    | { type: "read"; filePath: string; content?: string; offset?: number; limit?: number }
    | {
        type: "edit";
        filePath: string;
        oldString?: string;
        newString?: string;
        unifiedDiff?: string;
      }
    | { type: "write"; filePath: string; content?: string }
    | {
        type: "search";
        query: string;
        toolName?: "search" | "grep" | "glob" | "web_search";
        content?: string;
        filePaths?: string[];
        webResults?: Array<{ title: string; url: string }>;
        annotations?: string[];
        numFiles?: number;
        numMatches?: number;
        durationMs?: number;
        durationSeconds?: number;
        truncated?: boolean;
        mode?: "content" | "files_with_matches" | "count";
      }
    | {
        type: "fetch";
        url: string;
        prompt?: string;
        result?: string;
        code?: number;
        codeText?: string;
        bytes?: number;
        durationMs?: number;
      }
    | {
        type: "plain_text";
        label?: string;
        text?: string;
        icon?:
          | "wrench"
          | "square_terminal"
          | "eye"
          | "pencil"
          | "search"
          | "bot"
          | "sparkles"
          | "brain"
          | "mic_vocal";
      }
    | { type: "plan"; text: string }
    | { type: "unknown"; input: unknown; output: unknown };

  export type ProviderTimelineItem =
    | {
        id: string;
        revertToken?: unknown;
        type: "user_message";
        text: string;
        messageId?: string;
        clientMessageId?: string;
      }
    | {
        id: string;
        revertToken?: unknown;
        type: "assistant_message";
        text: string;
        messageId?: string;
      }
    | {
        id: string;
        revertToken?: unknown;
        type: "reasoning";
        text: string;
      }
    | {
        id: string;
        revertToken?: unknown;
        type: "tool_call";
        callId: string;
        name: string;
        detail: ProviderToolCallDetail;
        metadata?: Record<string, unknown>;
        status: "running" | "completed" | "canceled";
        error: null;
      }
    | {
        id: string;
        revertToken?: unknown;
        type: "tool_call";
        callId: string;
        name: string;
        detail: ProviderToolCallDetail;
        metadata?: Record<string, unknown>;
        status: "failed";
        error: unknown;
      }
    | {
        id: string;
        revertToken?: unknown;
        type: "error";
        message: string;
      }
    | {
        id: string;
        revertToken?: unknown;
        type: "notification";
        level: "info" | "warning" | "error";
        message: string;
      }
    | {
        id: string;
        revertToken?: unknown;
        type: "compaction";
        status: "loading" | "completed";
        trigger?: "auto" | "manual";
        preTokens?: number;
      };

  export interface ProviderErrorShape {
    message: string;
    code?: string;
    diagnostic?: string;
  }

  export type ProviderEvent =
    | { type: "catalog"; requestId: string; catalog: ProviderCatalog }
    | {
        type: "sessions";
        requestId: string;
        sessions: Array<{
          persistence: ProviderPersistence;
          cwd: string;
          title?: string;
          description?: string;
          updatedAt?: string;
        }>;
      }
    | { type: "request.completed"; requestId: string }
    | { type: "request.failed"; requestId: string; error: ProviderErrorShape }
    | {
        type: "session.opened";
        requestId?: string;
        sessionId: string;
        parentSessionId?: string;
        toolCallId?: string;
        capabilities: string[];
        restoration: "core" | "parent";
        persistence?: ProviderPersistence;
        title?: string;
        description?: string;
        cwd: string;
      }
    | { type: "session.ready"; requestId?: string; sessionId: string }
    | { type: "session.closed"; sessionId: string; error?: ProviderErrorShape }
    | { type: "session.runtime_failed"; sessionId: string; error: ProviderErrorShape }
    | { type: "session.persistence"; sessionId: string; persistence: ProviderPersistence }
    | {
        type: "session.prompt_result";
        sessionId: string;
        clientMessageId: string;
        result:
          | { type: "turn"; turnId: string }
          | { type: "steer"; turnId: string }
          | { type: "completed" }
          | { type: "failed"; error: ProviderErrorShape };
      }
    | {
        type: "session.turn";
        sessionId: string;
        turnId: string;
        state: "started" | "completed" | "failed" | "canceled";
        error?: ProviderErrorShape;
      }
    | {
        type: "session.usage";
        sessionId: string;
        turnId?: string;
        usage: {
          inputTokens?: number;
          cachedInputTokens?: number;
          outputTokens?: number;
          totalCostUsd?: number;
          contextWindowMaxTokens?: number;
          contextWindowUsedTokens?: number;
        };
      }
    | {
        type: "session.config";
        sessionId: string;
        config: {
          model?: string;
          mode?: string;
          thinkingOption?: string;
          models: ProviderCatalogModel[];
          modes: ProviderCatalogMode[];
          thinkingOptions: ProviderCatalogSelectOption[];
          settings: unknown[];
        };
      }
    | {
        type: "session.notice";
        sessionId: string;
        notice: {
          id: string;
          severity: "info" | "warning" | "error";
          title: string;
          description?: string;
          dismissed?: boolean;
        };
      }
    | {
        type: "session.commands";
        sessionId: string;
        commands: unknown[];
      }
    | {
        type: "timeline.item";
        sessionId: string;
        item: ProviderTimelineItem;
        timestamp?: string;
      };

  export interface ProviderConnection {
    version?: number;
    capabilities?: readonly string[];
    send(input: ProviderInput): Promise<void>;
    onEvent?(listener: (event: ProviderEvent) => void): () => void;
    close(): Promise<void>;
  }

  export interface ProviderDefinition {
    id: string;
    label: string;
    description?: string;
    command?: string | readonly string[];
    icon?: string;
    status?(ctx: ProviderStatusContext): Promise<ProviderStatusResult>;
    getCatalogCacheKey?(
      ctx: ProviderCatalogCacheKeyContext,
    ): string | undefined | Promise<string | undefined>;
    connect(ctx: ProviderConnectContext): ProviderConnection | Promise<ProviderConnection>;
  }

  export const PROVIDER_CAPABILITIES: readonly ProviderCapability[];
  export const PROVIDER_PROTOCOL_VERSION: number;

  export function negotiateProviderCapabilities(
    offered: readonly ProviderCapability[],
    supported: readonly ProviderCapability[],
  ): ProviderCapability[];

  export function requireProviderCapabilities(
    capabilities: readonly ProviderCapability[],
    input: ProviderInput,
  ): void;
}

declare function fetch(
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
    signal?: AbortSignal;
  },
): Promise<{
  ok: boolean;
  status: number;
  headers: { get(name: string): string | null };
  json(): Promise<unknown>;
  text(): Promise<string>;
}>;

declare module "@getpaseo/plugin/server/usage" {
  export type UsageTone = "default" | "ok" | "warning" | "danger";

  export type UsageScope =
    | { kind: "global" }
    | {
        kind: "session";
        provider: string;
        model?: string;
        env: Record<string, string>;
      };

  export type UsageAccount = {
    key: string;
    label?: string;
    harness?: string;
    input: unknown;
  };

  export type UsageWindow = {
    id: string;
    label: string;
    shortLabel?: string;
    summary?: boolean;
    usedPct?: number | null;
    remainingPct?: number | null;
    resetsAt?: string | null;
    runsOutAt?: string | null;
    shortfallPct?: number | null;
    tone?: UsageTone;
  };

  export type UsageBalance = {
    id: string;
    label: string;
    used?: number | null;
    remaining?: number | null;
    limit?: number | null;
    unit: "usd" | "credits" | "requests" | "tokens";
    resetsAt?: string | null;
    tone?: UsageTone;
  };

  export type UsageDetail = {
    id: string;
    label: string;
    value: string;
    tone?: UsageTone;
  };

  export type UsageProblem =
    | { kind: "expired"; expiresAt: string; refreshedBy?: string }
    | { kind: "rejected"; status: number; refreshedBy?: string }
    | { kind: "no_quota"; detail: string };

  export type UsageReport =
    | {
        status: "available";
        planLabel?: string;
        windows: UsageWindow[];
        balances?: UsageBalance[];
        details?: UsageDetail[];
      }
    | { status: "unavailable"; problem: UsageProblem }
    | { status: "error"; error: string };

  export function windowFromUsedPct(input: {
    id: string;
    label: string;
    shortLabel?: string;
    summary?: boolean;
    utilizationPct?: number | null;
    resetsAt?: string | null;
    tone?: UsageTone;
  }): UsageWindow;

  export function windowFromReportedDuration(input: {
    durationSeconds: number | null;
    unknown: { id: string; label: string; shortLabel: string };
    utilizationPct?: number | null;
    resetsAt?: string | null;
    summary?: boolean;
    tone?: UsageTone;
    scope?: { id: string; label: string; shortLabel?: string };
  }): UsageWindow;

  export function toneFromUsedPct(usedPct: number | null | undefined): UsageTone;

  export function balanceToneFromRemaining(
    remaining: number | null | undefined,
  ): UsageTone;

  export function usedPctOf(
    used: number | null | undefined,
    limit: number | null | undefined,
  ): number | null;

  export function hashAccountKey(value: string): string;

  export function unavailable(problem: UsageProblem): UsageReport;
}
