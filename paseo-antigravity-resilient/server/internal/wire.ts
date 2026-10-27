export type InitFrame = {
  conversation_id?: string;
  cwd?: string;
  event: "init";
  init?: {
    model?: string | null;
  };
  model?: string;
  timestamp?: string;
  tools?: string[];
  version?: string;
};

export type StepUpdateToolInfo = {
  name: string;
  parameters?: Record<string, unknown>;
  output?: string;
  error?: { type: string; message: string } | null;
};

export type StepUpdateChildInfo = {
  type_name: string;
  role?: string;
  initial_prompt?: string;
  conversation_id?: string | null;
  log_uri?: string;
  workspace_uris?: string[];
};

/**
 * Token accounting as `agy` reports it. Both `step_update.usage` and
 * `result.usage` use this shape (verified against `agy --output-format
 * stream-json`, e.g. `{"input_tokens":14559,"output_tokens":5,
 * "thinking_tokens":0,"cache_read_tokens":0,"total_tokens":14564}`).
 */
export type TokenUsage = {
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  thinking_tokens?: number;
  total_tokens?: number;
};

export type StepUpdate = {
  conversation_id?: string;
  step_index: number;
  state: "ACTIVE" | "DONE" | "ERROR";
  step_type: string;
  text_delta?: string;
  duration_seconds?: number;
  tool_name?: string;
  tool_info?: StepUpdateToolInfo | null;
  subagent_info?: {
    subagents: StepUpdateChildInfo[];
  } | null;
  usage?: TokenUsage | null;
};

export type StepUpdateFrame = {
  conversation_id?: string;
  event: "step_update";
  step_update: StepUpdate;
  timestamp?: string;
};

export type MessageFrame = {
  content?: string;
  conversation_id?: string;
  event: "message";
  role?: string;
  timestamp?: string;
};

export type ThinkingFrame = {
  content?: string;
  conversation_id?: string;
  event: "thinking";
  timestamp?: string;
};

export type ToolCallFrame = {
  arguments?: unknown;
  conversation_id?: string;
  event: "tool_call";
  id?: string;
  name?: string;
  timestamp?: string;
};

export type ToolResultFrame = {
  content?: unknown;
  conversation_id?: string;
  error?: unknown;
  event: "tool_result";
  id?: string;
  is_error?: boolean;
  name?: string;
  timestamp?: string;
};

export type NestedResultPayload = {
  conversation_id?: string;
  status?: "SUCCESS" | "ERROR" | string;
  response?: string;
  error?: string;
  duration_seconds?: number;
  num_turns?: number;
  /**
   * Final token accounting for the turn. `agy` sends it here (nested under
   * `result`), not as a top-level `stats` object.
   */
  usage?: TokenUsage | null;
  denied_actions?: Array<{
    action: string;
    display_name: string;
  }>;
};

export type ResultFrame = {
  conversation_id?: string;
  duration_ms?: number;
  errors?: unknown[];
  event: "result";
  response?: string;
  result?: NestedResultPayload;
  /**
   * Legacy top-level stats object. Retained only so transcripts captured by
   * older `agy` builds keep replaying; current builds report the same numbers
   * as `result.usage`.
   */
  stats?: {
    cache_read_tokens?: number;
    cache_write_tokens?: number;
    input_tokens?: number;
    output_tokens?: number;
    thinking_tokens?: number;
    tool_calls_ExecutionFailed?: number;
    tool_calls_Succeeded?: number;
    tool_calls_total?: number;
    total_tokens?: number;
  };
  status?: string;
  timestamp?: string;
};

export type Frame =
  | InitFrame
  | StepUpdateFrame
  | MessageFrame
  | ThinkingFrame
  | ToolCallFrame
  | ToolResultFrame
  | ResultFrame;

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseFrame(line: string): Frame | null {
  const trimmed = line.trim();
  if (!trimmed.startsWith("{")) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(trimmed);
    return isRecord(parsed) && typeof parsed.event === "string"
      ? (parsed as Frame)
      : null;
  } catch {
    return null;
  }
}

export function frameConversationId(frame: Frame): string | undefined {
  if (typeof frame.conversation_id === "string" && frame.conversation_id.trim()) {
    return frame.conversation_id.trim();
  }
  if (
    frame.event === "step_update" &&
    typeof frame.step_update?.conversation_id === "string" &&
    frame.step_update.conversation_id.trim()
  ) {
    return frame.step_update.conversation_id.trim();
  }
  if (
    frame.event === "result" &&
    typeof frame.result?.conversation_id === "string" &&
    frame.result.conversation_id.trim()
  ) {
    return frame.result.conversation_id.trim();
  }
  return undefined;
}

export function isResultSuccess(frame: ResultFrame): boolean {
  if (frame.result && typeof frame.result.status === "string") {
    return frame.result.status.toUpperCase() === "SUCCESS";
  }
  if (typeof frame.status === "string") {
    return frame.status.toLowerCase() === "success";
  }
  return true;
}

export function resultResponse(frame: ResultFrame): string | undefined {
  if (frame.result && typeof frame.result.response === "string") {
    return frame.result.response;
  }
  if (typeof frame.response === "string") {
    return frame.response;
  }
  return undefined;
}

export function resultErrors(frame: ResultFrame): string[] {
  const collected: string[] = [];
  if (
    frame.result &&
    typeof frame.result.error === "string" &&
    frame.result.error.trim() &&
    !isResultSuccess(frame)
  ) {
    collected.push(frame.result.error.trim());
  }
  for (const entry of frame.errors ?? []) {
    if (typeof entry === "string" && entry.trim()) {
      collected.push(entry.trim());
    } else if (isRecord(entry) && typeof entry.message === "string" && entry.message.trim()) {
      collected.push(entry.message.trim());
    }
  }
  return collected;
}

export function resultError(frame: ResultFrame): string {
  const messages = resultErrors(frame);
  return messages.join("\n") || "Antigravity turn failed";
}

/**
 * Matches transient OAuth 401 refresh-race and token-endpoint failures
 * observed in local Antigravity CLI logs:
 * - "generating and executing: UNAUTHENTICATED (code 401): Request had invalid authentication credentials..."
 * - "pre-invocation hook: context summarization: UNAUTHENTICATED (code 401): Request had invalid authentication credentials..."
 * - "GetToken failed: oauth2: cannot fetch token: Post \"https://oauth2.googleapis.com/token\": net/http: TLS handshake timeout"
 */
export function isTokenGap(message: string): boolean {
  return (
    /\bunauthenticated\b/i.test(message) ||
    /\bcode\s*401\b/i.test(message) ||
    /invalid authentication credentials/i.test(message) ||
    /expected oauth 2 access token/i.test(message) ||
    /oauth2:\s*cannot fetch token/i.test(message) ||
    /oauth2\.googleapis\.com\/token/i.test(message)
  );
}

/**
 * Matches Antigravity stream-interruption trajectory errors:
 * - "The stream was interrupted. Please continue the task you were working on."
 * - "API error (attempt 1): request failed: stream idle timeout (5m0s): context deadline exceeded"
 * The CLI may recover internally but retain either error in later result frames.
 */
export function isStreamInterruption(message: string): boolean {
  return (
    /the stream was interrupted/i.test(message) ||
    /please continue the task you were working on/i.test(message) ||
    /\brequest failed:\s*stream idle timeout\s*\([^\r\n)]+\):\s*context deadline exceeded\b/i.test(message)
  );
}

/**
 * Narrow check for genuine Antigravity authentication/login errors.
 * Does NOT match arbitrary error strings that merely contain the substring "auth"
 * (such as "author", "authority", or application-level error messages).
 */
export function isAuthDiagnosticError(message: string): boolean {
  return (
    isTokenGap(message) ||
    /you are not logged in/i.test(message) ||
    /not authenticated/i.test(message) ||
    /\bsign[\s_-]?in\b/i.test(message) ||
    /\blog[\s_-]?in\b/i.test(message)
  );
}

export function diagnostic(message: string): string {
  if (isAuthDiagnosticError(message)) {
    return "Antigravity authentication failed. Run `agy` and sign in, then retry.";
  }
  return message;
}
