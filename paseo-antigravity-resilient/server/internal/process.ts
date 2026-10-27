import { execCommand, spawnProcess, terminateProcess } from "@getpaseo/plugin/server";
import type { ChildProcess, SpawnOptions } from "node:child_process";
import { createInterface, type Interface } from "node:readline";
import type { Readable, Writable } from "node:stream";
import {
  combineAbortSignals,
  createAbortError,
  isAbortError,
  ProviderActionError,
} from "./signals.ts";

export type DriverProcess = {
  onClose: (
    listener: (code: number | null, signal: string | null) => void,
  ) => void;
  onError: (listener: (error: Error) => void) => void;
  onStderrLine: (listener: (line: string) => void) => void;
  onStdoutLine: (listener: (line: string) => void) => void;
  sendLine: (line: string) => Promise<void>;
  stop: () => Promise<void>;
};

export type SpawnFunction = (
  command: string,
  args: readonly string[],
  options?: SpawnOptions,
) => ChildProcess & {
  stdin: Writable;
  stdout: Readable;
  stderr: Readable;
};

export async function probe(
  command: string,
  args: string[],
  env?: Record<string, string>,
  timeoutMs = 20_000,
  signal?: AbortSignal,
): Promise<{
  exitCode: number | null;
  stderr: string;
  stdout: string;
  timedOut?: boolean;
}> {
  if (signal?.aborted) {
    throw createAbortError();
  }
  const timeoutController = new AbortController();
  const timer = setTimeout(() => {
    timeoutController.abort();
  }, timeoutMs);
  const combinedSignal = signal
    ? combineAbortSignals([signal, timeoutController.signal])
    : timeoutController.signal;

  try {
    const result = await execCommand(command, args, {
      env,
      signal: combinedSignal,
      maxBuffer: 1024 * 1024,
    });
    return { exitCode: 0, stdout: result.stdout, stderr: result.stderr };
  } catch (error) {
    if (signal?.aborted) {
      throw createAbortError();
    }
    if (timeoutController.signal.aborted && isAbortError(error)) {
      return {
        exitCode: null,
        stdout: "",
        stderr: `Command timed out after ${timeoutMs}ms`,
        timedOut: true,
      };
    }
    const err = error as NodeJS.ErrnoException & {
      code?: number | string;
      killed?: boolean;
      stderr?: string;
      stdout?: string;
    };
    if (err.code === "ENOENT") {
      throw new ProviderActionError(
        "BINARY_NOT_FOUND",
        `Antigravity CLI not found: ${command}`,
      );
    }
    return {
      exitCode: typeof err.code === "number" ? err.code : 1,
      stdout: err.stdout ?? "",
      stderr: err.stderr ?? err.message ?? String(error),
    };
  } finally {
    clearTimeout(timer);
  }
}

export function spawnDriver(input: {
  args: string[];
  command: string;
  cwd: string;
  env?: Record<string, string>;
  spawn?: SpawnFunction;
}): DriverProcess {
  const spawnImpl = input.spawn ?? spawnProcess;
  const child = spawnImpl(input.command, input.args, {
    cwd: input.cwd,
    env: input.env,
    stdio: ["pipe", "pipe", "pipe"],
  });

  let stdoutInterface: Interface | null = createInterface({
    input: child.stdout,
    crlfDelay: Infinity,
  });
  let stderrInterface: Interface | null = createInterface({
    input: child.stderr,
    crlfDelay: Infinity,
  });

  const closeInterfaces = () => {
    stdoutInterface?.close();
    stderrInterface?.close();
    stdoutInterface = null;
    stderrInterface = null;
  };
  child.once("close", closeInterfaces);

  return {
    sendLine(line) {
      return new Promise((resolve, reject) => {
        if (!child.stdin.writable || child.stdin.destroyed) {
          reject(new Error("Antigravity CLI stdin is closed"));
          return;
        }
        child.stdin.write(`${line}\n`, (error) => {
          if (error) {
            reject(error);
            return;
          }
          resolve();
        });
      });
    },
    onStdoutLine(listener) {
      stdoutInterface?.on("line", listener);
    },
    onStderrLine(listener) {
      stderrInterface?.on("line", listener);
    },
    onError(listener) {
      child.on("error", listener);
    },
    onClose(listener) {
      child.on("close", listener);
    },
    async stop() {
      closeInterfaces();
      if (child.exitCode !== null || child.signalCode !== null) {
        return;
      }
      child.stdin.end();
      await new Promise<void>((resolve) => {
        let killTimer: ReturnType<typeof setTimeout> | null = null;
        let giveUpTimer: ReturnType<typeof setTimeout> | null = null;
        const done = () => {
          if (killTimer !== null) {
            clearTimeout(killTimer);
          }
          if (giveUpTimer !== null) {
            clearTimeout(giveUpTimer);
          }
          child.off("close", done);
          resolve();
        };
        child.once("close", done);
        killTimer = setTimeout(() => {
          void terminateProcess(child, "SIGKILL").catch(() => undefined);
        }, 1_500);
        giveUpTimer = setTimeout(done, 7_000);
        if (process.platform === "win32") {
          void terminateProcess(child, "SIGTERM").catch(() => undefined);
        } else {
          try {
            child.kill("SIGTERM");
          } catch {
            done();
          }
        }
      });
    },
  };
}
