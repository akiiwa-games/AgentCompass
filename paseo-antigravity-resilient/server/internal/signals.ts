export class ProviderActionError extends Error {
  readonly code: string;
  readonly diagnostic?: string;

  constructor(code: string, message: string, diagnostic?: string) {
    super(message);
    this.code = code;
    this.diagnostic = diagnostic;
  }
}

export class OperationSignal {
  private readonly controller = new AbortController();

  get signal(): AbortSignal {
    return this.controller.signal;
  }

  get aborted(): boolean {
    return this.controller.signal.aborted;
  }

  abort(): void {
    this.controller.abort();
  }
}

export function isAbortError(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === "AbortError" ||
      ("code" in error && (error as { code?: unknown }).code === "ABORT_ERR"))
  );
}

export function createAbortError(): Error {
  const error = new Error("The operation was aborted");
  error.name = "AbortError";
  return error;
}

export function combineAbortSignals(signals: AbortSignal[]): AbortSignal {
  const controller = new AbortController();
  const abort = () => controller.abort();
  for (const signal of signals) {
    if (signal.aborted) {
      controller.abort();
      break;
    }
    signal.addEventListener("abort", abort, { once: true });
  }
  return controller.signal;
}
