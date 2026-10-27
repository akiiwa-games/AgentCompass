import type {
  ProviderContentPart,
  ProviderPrompt,
} from "@getpaseo/plugin/server/provider";

function renderPart(part: ProviderContentPart): string {
  switch (part.type) {
    case "text":
      return part.text;
    case "uploaded_file":
      return [
        "Uploaded file available on the daemon filesystem:",
        `Path: ${part.path}`,
        `Filename: ${part.fileName}`,
        `MIME type: ${part.mimeType}`,
        `Size: ${part.size} bytes`,
      ].join("\n");
    case "review":
      return [
        `Code review (${part.mode}${part.baseRef ? ` vs ${part.baseRef}` : ""}):`,
        ...part.comments.map((comment) => {
          const lines = comment.context.lines
            .map((line) => {
              const prefix =
                line.type === "add" ? "+" : line.type === "remove" ? "-" : " ";
              return `${prefix} ${line.content}`;
            })
            .join("\n");
          return [
            `${comment.filePath}:${comment.lineNumber} (${comment.side})`,
            comment.context.hunkHeader,
            lines,
            `Comment: ${comment.body}`,
          ].join("\n");
        }),
      ].join("\n\n");
    case "github_pr":
    case "forge_change_request":
      return [
        `Change request #${part.number}: ${part.title}`,
        `URL: ${part.url}`,
        ...(part.body ? [part.body] : []),
      ].join("\n");
    case "github_issue":
    case "forge_issue":
      return [
        `Issue #${part.number}: ${part.title}`,
        `URL: ${part.url}`,
        ...(part.body ? [part.body] : []),
      ].join("\n");
    case "image":
      throw new Error("Antigravity CLI does not support image attachments.");
  }
}

export function renderPrompt(
  prompt: ProviderPrompt,
  systemPrompt?: string,
): { cliText: string; userText: string } {
  const userText =
    prompt.input.type === "command"
      ? `/${prompt.input.name}${prompt.input.arguments.trim() ? ` ${prompt.input.arguments.trim()}` : ""}`
      : prompt.input.content.map(renderPart).filter(Boolean).join("\n\n").trim();

  if (!userText) {
    throw new Error("Antigravity prompt cannot be empty.");
  }

  const trimmedSystem = systemPrompt?.trim();
  return {
    userText,
    cliText: trimmedSystem ? `${trimmedSystem}\n\n${userText}` : userText,
  };
}
