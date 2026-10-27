// Pi's documented before_provider_request payload transformer. Kept beside the
// Paseo RPC implementation so both surfaces use the same atomic state contract.
import { readFastModeState, writeFastModeState } from "./fast";

type Context = {
  model?: { provider: string; api: string };
  hasUI: boolean;
  ui: {
    setStatus(key: string, value: string | undefined): void;
    notify(message: string, level: "info" | "error"): void;
    select(title: string, options: string[]): Promise<string | undefined>;
  };
};
type FastExtensionApi = {
  on(name: "session_start" | "model_select", handler: (event: unknown, ctx: Context) => void): unknown;
  on(name: "before_provider_request", handler: (event: { payload: unknown }, ctx: Context) => unknown): unknown;
  registerCommand(name: string, options: {
    description: string;
    getArgumentCompletions?: (prefix: string) => { value: string; label: string }[];
    handler: (args: string, ctx: Context) => Promise<void>;
  }): void;
};

export default function contribute(pi: FastExtensionApi) {
  const compatible = (ctx: Context) => ["openai", "openai-codex"].includes(ctx.model?.provider ?? "");
  const status = (ctx: Context, enabled: boolean) => {
    if (ctx.hasUI) ctx.ui.setStatus("fast-mode", enabled && compatible(ctx) ? "⚡ fast" : undefined);
  };
  const apply = (enabled: boolean, ctx: Context) => {
    const persisted = writeFastModeState(enabled); // Fail visibly before claiming success.
    status(ctx, persisted.enabled);
    if (ctx.hasUI) ctx.ui.notify(`Fast mode: ${persisted.enabled ? "ON (priority)" : "OFF (default)"} — host-wide`, "info");
  };
  for (const event of ["session_start", "model_select"] as const)
    pi.on(event, (_event, ctx) => status(ctx, readFastModeState().enabled));
  pi.on("before_provider_request", (event, ctx) => {
    if (!compatible(ctx) || !event.payload || typeof event.payload !== "object" || Array.isArray(event.payload)) return undefined;
    const state = readFastModeState();
    // Explicit default also clears a priority tier supplied earlier in the request pipeline.
    return { ...event.payload as Record<string, unknown>, service_tier: state.serviceTier };
  });
  pi.registerCommand("fast", {
    description: "OpenAI/Codex Fast mode (host-wide): on|off|toggle|status|select",
    getArgumentCompletions: prefix => ["on", "off", "toggle", "status", "select"]
      .filter(value => value.startsWith(prefix.trim().toLowerCase())).map(value => ({ value, label: value })),
    async handler(args, ctx) {
      const command = args.trim().toLowerCase();
      const state = readFastModeState();
      if (command === "status") {
        if (ctx.hasUI) ctx.ui.notify(`Fast mode: ${state.enabled ? "ON (priority)" : "OFF (default)"} — host-wide`, "info");
        return;
      }
      if (command === "select") {
        if (!ctx.hasUI) throw new Error("/fast select needs an interactive UI; use /fast on or off");
        const value = await ctx.ui.select("OpenAI/Codex speed", ["Fast (priority)", "Normal (default)"]);
        if (value) apply(value.startsWith("Fast"), ctx);
        return;
      }
      if (["on", "true", "1", "priority"].includes(command)) return apply(true, ctx);
      if (["off", "false", "0", "normal", "default"].includes(command)) return apply(false, ctx);
      if (command && command !== "toggle") throw new Error("Use /fast on|off|toggle|status|select");
      apply(!state.enabled, ctx);
    },
  });
  for (const [command, enabled] of [["fast-on", true], ["fast-off", false]] as const)
    pi.registerCommand(command, { description: `${enabled ? "Enable" : "Disable"} host-wide OpenAI/Codex priority`,
      async handler(_args, ctx) { apply(enabled, ctx); } });
}
