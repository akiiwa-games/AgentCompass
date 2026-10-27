/**
 * The credential store key `agy` writes under (Windows Credential Manager
 * target, macOS Keychain service). It is the same on every platform because it
 * comes from the CLI's own store name, not the OS.
 */
export const DEFAULT_KEYRING_TARGET = "gemini:antigravity";

export type UsageInput = {
  target: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Hand-written stand-in for a zod schema. The host only ever calls
 * `parseAsync` on this, and never serializes it, so a structural validator
 * keeps the plugin free of a runtime dependency it would otherwise have to
 * bundle. Unknown/invalid input falls back to the default target.
 */
export const inputSchema = {
  async parseAsync(value: unknown): Promise<UsageInput> {
    const target =
      isRecord(value) &&
      typeof value.target === "string" &&
      value.target.trim()
        ? value.target.trim()
        : DEFAULT_KEYRING_TARGET;
    return { target };
  },
};
