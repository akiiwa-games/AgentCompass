import type {
  PluginButton,
  PluginButtonRegistration,
  PluginClientContext,
} from "@getpaseo/plugin/client";
import { getFastModeRpc, setFastModeRpc } from "./shared/fast";

function isPiProvider(provider?: string | null): boolean {
  if (!provider) return false;
  return provider === "pi" || provider.startsWith("pi/") || provider.startsWith("pi:");
}

export default function contribute(client: PluginClientContext) {
  let fastEnabled = true;
  const pills = new Map<string, { reg: PluginButtonRegistration; provider: string }>();
  const lifetime = new AbortController();

  const buildPillDescriptor = (enabled: boolean, visible: boolean): PluginButton => ({
    title: enabled
      ? 'Pi Fast mode: ON (host-wide, service_tier: "priority") - Click to disable'
      : "Pi Fast mode: OFF (host-wide, default speed) - Click to enable",
    icon: enabled ? "Zap" : "ZapOff",
    label: enabled ? "Fast: ON" : "Fast: OFF",
    visible,
    behavior: {
      kind: "action",
      async onPress() {
        const res = await client.rpc(setFastModeRpc, {});
        applyState(res.enabled);
      },
    },
  });

  const applyState = (enabled: boolean) => {
    fastEnabled = enabled;
    for (const { reg, provider } of pills.values()) {
      reg.update(buildPillDescriptor(fastEnabled, isPiProvider(provider)));
    }
  };

  const refreshStateFromDaemon = async () => {
    try {
      const state = await client.rpc(getFastModeRpc, {});
      if (!lifetime.signal.aborted) {
        applyState(state.enabled);
      }
    } catch {
      // Ignore transient RPC errors during startup
    }
  };

  const registerOrUpdateAgentPill = (agent: {
    id: string;
    workspaceId?: string | null;
    provider?: string | null;
  }) => {
    if (lifetime.signal.aborted || !agent.workspaceId) return;
    const agentId = agent.id;
    const workspaceId = agent.workspaceId;
    const provider = agent.provider ?? "";
    const existing = pills.get(agentId);
    if (existing) {
      existing.provider = provider;
      existing.reg.update(buildPillDescriptor(fastEnabled, isPiProvider(provider)));
      return;
    }
    const reg = client.addComposerPill({
      id: "pi-fast",
      workspaceId,
      agentId,
      button: buildPillDescriptor(fastEnabled, isPiProvider(provider)),
    });
    pills.set(agentId, { reg, provider });
  };

  const removeAllPills = () => {
    for (const { reg } of pills.values()) reg.remove();
    pills.clear();
  };

  void refreshStateFromDaemon();

  // Observe agents for Composer Pills
  void client.paseo.agents
    .list({ subscribe: {}, signal: lifetime.signal })
    .then(({ subscription }) => {
      subscription.subscribe({
        snapshot: ({ entries }) => {
          removeAllPills();
          for (const { agent } of entries) {
            registerOrUpdateAgentPill(agent);
          }
          void refreshStateFromDaemon();
        },
        update: (message) => {
          if (message.type !== "agent_update") return;
          const update = message.payload;
          if (update.kind === "upsert") {
            registerOrUpdateAgentPill(update.agent);
            void refreshStateFromDaemon();
            return;
          }
          pills.get(update.agentId)?.reg.remove();
          pills.delete(update.agentId);
        },
      });
      return undefined;
    })
    .catch((error) => {
      if (!lifetime.signal.aborted) {
        console.error("[paseo-pi-fast] Agent observation failed", error);
      }
    });

  const removeCommandCenter = client.addCommandCenterItem({
    id: "toggle-pi-fast",
    title: "Toggle Pi Fast Mode (service_tier: priority)",
    icon: "Zap",
    keywords: ["fast", "priority", "pi", "codex", "openai", "speed"],
    context: "global",
    async onSelect({ rpc }) {
      const res = await rpc(setFastModeRpc, {});
      applyState(res.enabled);
    },
  });

  const removeSlashCommand = client.addSlashCommand({
    name: "pi-fast",
    description: "Toggle Pi OpenAI/Codex Fast mode (service_tier: priority)",
    argumentHint: "[on|off]",
    context: "workspace",
    async onSubmit({ args, rpc }) {
      const normalized = args.trim().toLowerCase();
      const desired =
        normalized === "on" || normalized === "true" || normalized === "1"
          ? true
          : normalized === "off" || normalized === "false" || normalized === "0"
            ? false
            : undefined;
      const res = await rpc(setFastModeRpc, { enabled: desired });
      applyState(res.enabled);
    },
  });

  return () => {
    lifetime.abort();
    removeCommandCenter();
    removeSlashCommand();
    removeAllPills();
  };
}
