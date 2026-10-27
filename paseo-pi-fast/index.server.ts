import type { PluginServerContext } from "@getpaseo/plugin/server";
import { handleGetFastMode, handleSetFastMode, readFastModeState, writeFastModeState } from "./server/fast";
import { getFastModeRpc, setFastModeRpc } from "./shared/fast";

export default function contribute(server: PluginServerContext) {
  // Ensure default config exists on startup
  const initial = readFastModeState();
  if (!initial.updatedAt) {
    writeFastModeState(initial.enabled);
  }

  server.handle(getFastModeRpc, handleGetFastMode);
  server.handle(setFastModeRpc, handleSetFastMode);
  return () => {};
}
