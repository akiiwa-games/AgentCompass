import type { ProviderTimelineItem, ProviderToolCallDetail } from "@getpaseo/plugin/server/provider";
import { isRecord, type ToolResultFrame } from "./wire.ts";

/**
 * Paseo 0.11.2 treats every replayed notification as an opening notice and
 * appends it again when a durable timeline already exists. Compaction is a
 * native timeline event, not a notification. Migrate old snapshots on read
 * without rewriting their timestamps, IDs or chronological storage order.
 */
export function replayTimelineItem(item: ProviderTimelineItem): ProviderTimelineItem {
  if (item.type !== "notification" || !/^turn-\d+:auto-compact$/.test(item.id)) {
    return item;
  }
  const match = /^Auto-compacted context \(([\d,]+) tokens\) 1m before 5m hot cache expiry\.$/.exec(item.message);
  if (!match) return item;
  const preTokens = Number(match[1].replaceAll(",", ""));
  if (!Number.isSafeInteger(preTokens) || preTokens < 0) return item;
  return { id: item.id, type: "compaction", status: "completed", trigger: "auto", preTokens };
}

function stringArg(
  input: Record<string, unknown>,
  keys: readonly string[],
): string | undefined {
  for (const key of keys) {
    const value = input[key];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return undefined;
}

export function toolOutput(frame: ToolResultFrame): string | undefined {
  if (typeof frame.content === "string") {
    return frame.content;
  }
  if (frame.content !== undefined && frame.content !== null) {
    try {
      return JSON.stringify(frame.content, null, 2);
    } catch {
      return String(frame.content);
    }
  }
  if (typeof frame.error === "string") {
    return frame.error;
  }
  return undefined;
}

export function toolDetail(
  name: string,
  input: unknown,
  output?: string,
): ProviderToolCallDetail {
  const args = isRecord(input) ? input : {};
  const command = stringArg(args, ["CommandLine", "command", "cmd"]);
  if (name === "run_command" || command) {
    return {
      type: "shell",
      command: command ?? name,
      cwd: stringArg(args, ["Cwd", "cwd"]),
      ...(output !== undefined ? { output } : {}),
    };
  }

  const filePath = stringArg(args, [
    "AbsolutePath",
    "TargetFile",
    "FilePath",
    "file_path",
    "path",
  ]);
  if (
    (name === "view_file" ||
      name === "view_file_outline" ||
      name === "view_code_item" ||
      name === "read_file") &&
    filePath
  ) {
    return {
      type: "read",
      filePath,
      ...(output !== undefined ? { content: output } : {}),
    };
  }
  if (name === "write_to_file" && filePath) {
    return {
      type: "write",
      filePath,
      ...(typeof args.CodeContent === "string"
        ? { content: args.CodeContent }
        : {}),
    };
  }
  if (
    (name === "replace_file_content" || name === "multi_replace_file_content") &&
    filePath
  ) {
    return {
      type: "edit",
      filePath,
      ...(typeof args.TargetContent === "string"
        ? { oldString: args.TargetContent }
        : {}),
      ...(typeof args.ReplacementContent === "string"
        ? { newString: args.ReplacementContent }
        : {}),
    };
  }

  const query = stringArg(args, ["Query", "query", "Pattern", "pattern"]);
  if (
    name === "grep_search" ||
    name === "find_by_name" ||
    name === "search_web" ||
    query
  ) {
    return {
      type: "search",
      query: query ?? name,
      ...(output !== undefined ? { content: output } : {}),
    };
  }

  const url = stringArg(args, ["Url", "url"]);
  if (name === "read_url_content" || url) {
    return {
      type: "fetch",
      url: url ?? name,
      ...(output !== undefined ? { result: output } : {}),
    };
  }

  return {
    type: "unknown",
    input: input ?? null,
    output: output ?? null,
  };
}
