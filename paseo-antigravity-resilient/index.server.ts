import type { PluginServerContext } from "@getpaseo/plugin/server";
import { antigravityResilientProvider } from "./server/provider.ts";
import { inputSchema } from "./shared/input.ts";
import {
  discover,
  fetchUsage,
  USAGE_SOURCE_ID,
  USAGE_SOURCE_LABEL,
} from "./server/usage.ts";

export default function contribute(server: PluginServerContext) {
  server.registerProvider(antigravityResilientProvider);
  server.registerUsageSource({
    id: USAGE_SOURCE_ID,
    label: USAGE_SOURCE_LABEL,
    icon: "icon.svg",
    input: inputSchema,
    discover: (scope) => discover(scope),
    fetch: (input) => fetchUsage(input),
  });
  return () => {};
}
