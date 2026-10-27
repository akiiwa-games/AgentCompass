Pi Fast Mode adds a one-click Fast mode button (`service_tier: "priority"`) to Paseo for Pi agents using OpenAI or Codex models.

The composer button changes a host-wide setting shared by all Pi sessions, not a per-session preference. Failed writes are reported without claiming the setting changed.

Both hosts need the bundled Pi request extension (`server/pi-extension.ts`) linked from `~/.pi/agent/extensions/fast-mode.ts`. It shares the RPC’s atomic settings implementation. Existing Pi processes load extension code on their next reload or restart; settings are read on every request. Only OpenAI/Codex requests are changed, and OFF explicitly sends the default service tier.

The plugin synchronizes state with `~/.pi/agent/fast-mode.json`, which is read on each request by the `~/.pi/agent/extensions/fast-mode.ts` Pi extension. You can toggle Fast mode from the composer pill on any Pi agent, from the `/pi-fast` slash command, or from the Command Center.
