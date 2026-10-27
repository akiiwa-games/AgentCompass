import fs from "node:fs";
import { randomUUID } from "node:crypto";
import os from "node:os";
import path from "node:path";
import type { RpcInput } from "@getpaseo/plugin";
import type { FastModeState, setFastModeRpc } from "../shared/fast";

const CONFIG_PATH = path.join(os.homedir(), ".pi", "agent", "fast-mode.json");

export function readFastModeState(): FastModeState {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const raw = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
      if (typeof raw?.enabled === "boolean") {
        return {
          enabled: raw.enabled,
          serviceTier: raw.enabled ? "priority" : "default",
          updatedAt: typeof raw.updatedAt === "string" ? raw.updatedAt : undefined,
        };
      }
      throw new Error("Invalid Pi Fast configuration: enabled must be a boolean");
    }
  } catch (error) {
    if ((error as { code?: string }).code !== "ENOENT") throw error;
  }
  return {
    enabled: true,
    serviceTier: "priority",
  };
}

export function writeFastModeState(enabled: boolean): FastModeState {
  const next: FastModeState = {
    enabled,
    serviceTier: enabled ? "priority" : "default",
    updatedAt: new Date().toISOString(),
  };
  fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
  const temporary = `${CONFIG_PATH}.${randomUUID()}.tmp`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(next, null, 2) + "\n", { encoding: "utf-8", flag: "wx" });
    fs.renameSync(temporary, CONFIG_PATH);
  } finally {
    // On failure leave the old configuration intact and reject the RPC.
    fs.rmSync(temporary, { force: true });
  }
  return next;
}

export async function handleGetFastMode(): Promise<FastModeState> {
  return readFastModeState();
}

export async function handleSetFastMode(
  input: RpcInput<typeof setFastModeRpc>,
): Promise<FastModeState> {
  const current = readFastModeState();
  const nextEnabled = typeof input.enabled === "boolean" ? input.enabled : !current.enabled;
  return writeFastModeState(nextEnabled);
}
