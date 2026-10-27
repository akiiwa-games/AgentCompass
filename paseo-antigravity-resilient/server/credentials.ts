import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { isRecord } from "./internal/wire.ts";

const execFileAsync = promisify(execFile) as (
  file: string,
  args: readonly string[],
  options: Record<string, unknown>,
) => Promise<{ stdout: string; stderr: string }>;

const READ_TIMEOUT_MS = 5_000;
const MAX_BUFFER = 1024 * 1024;

export type AntigravityCredential = {
  accessToken: string;
  /** Epoch ms from the token's `expiry` field, when it parses. */
  expiresAt?: number;
  /** Account email decoded from the OAuth `id_token`, when present. */
  email?: string;
};

export type CredentialProblem =
  | { kind: "missing"; detail?: string }
  | { kind: "unsupported"; platform: string };

export type CredentialResult =
  | { ok: true; credential: AntigravityCredential }
  | { ok: false; problem: CredentialProblem };

/**
 * Reads the credential blob `agy` stores for the given keyring target,
 * without ever using or touching the refresh token: the plugin only reads the
 * current access token, and leaves refresh (which rotates the stored token) to
 * `agy` itself.
 *
 * Supported stores:
 * - win32: Windows Credential Manager, via a CredRead P/Invoke.
 * - darwin: Keychain, via `security find-generic-password`.
 *
 * Everything else reports `unsupported` rather than guessing at a store shape.
 */
export async function readAntigravityCredential(
  target: string,
  platform: string = process.platform,
): Promise<CredentialResult> {
  const command = credentialReadCommand(target, platform);
  if (!command) {
    return { ok: false, problem: { kind: "unsupported", platform } };
  }

  let stdout: string;
  try {
    const result = await execFileAsync(command.file, command.args, {
      encoding: "utf8",
      timeout: READ_TIMEOUT_MS,
      maxBuffer: MAX_BUFFER,
      windowsHide: true,
    });
    stdout = String(result.stdout ?? "");
  } catch (error) {
    return {
      ok: false,
      problem: {
        kind: "missing",
        detail: error instanceof Error ? error.message : String(error),
      },
    };
  }

  const credential = parseCredentialBlob(stdout);
  if (!credential) {
    return {
      ok: false,
      problem: {
        kind: "missing",
        detail: `no usable token in the "${target}" credential`,
      },
    };
  }
  return { ok: true, credential };
}

/**
 * Parses the JSON blob stored under `gemini:antigravity`, e.g.
 * `{"auth_method":"consumer","token":{"access_token":"ya29...","expiry":
 * "2026-10-16T10:13:38.9538207+08:00","refresh_token":"...","token_type":
 * "Bearer"},"id_token":"<jwt>"}`.
 */
export function parseCredentialBlob(blob: string): AntigravityCredential | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(blob);
  } catch {
    return null;
  }
  if (!isRecord(parsed) || !isRecord(parsed.token)) {
    return null;
  }
  const token = parsed.token;
  const accessToken =
    typeof token.access_token === "string" ? token.access_token.trim() : "";
  if (!accessToken) {
    return null;
  }
  const expiresAt =
    typeof token.expiry === "string" ? Date.parse(token.expiry) : Number.NaN;
  const email = emailFromIdToken(parsed.id_token);
  return {
    accessToken,
    ...(Number.isFinite(expiresAt) ? { expiresAt } : {}),
    ...(email ? { email } : {}),
  };
}

function credentialReadCommand(
  target: string,
  platform: string,
): { file: string; args: string[] } | null {
  if (platform === "win32") {
    return {
      file: "powershell.exe",
      args: [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encodePowerShell(windowsCredentialScript(target)),
      ],
    };
  }
  if (platform === "darwin") {
    return {
      file: "security",
      args: ["find-generic-password", "-s", target, "-w"],
    };
  }
  return null;
}

/** `-EncodedCommand` takes UTF-16LE base64, which sidesteps all quoting rules. */
function encodePowerShell(script: string): string {
  return Buffer.from(script, "utf16le").toString("base64");
}

function windowsCredentialScript(target: string): string {
  const literal = target.replace(/'/gu, "''");
  return `$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct AGY_CREDENTIAL {
    public UInt32 Flags;
    public UInt32 Type;
    public IntPtr TargetName;
    public IntPtr Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public UInt32 CredentialBlobSize;
    public IntPtr CredentialBlob;
    public UInt32 Persist;
    public UInt32 AttributeCount;
    public IntPtr Attributes;
    public IntPtr TargetAlias;
    public IntPtr UserName;
}
public static class AgyCred {
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredRead(string target, UInt32 type, UInt32 flags, out IntPtr credential);
    [DllImport("advapi32.dll", SetLastError = true)]
    public static extern void CredFree(IntPtr cred);
}
'@
$ptr = [IntPtr]::Zero
if (-not [AgyCred]::CredRead('${literal}', 1, 0, [ref]$ptr)) { exit 3 }
$cred = [Runtime.InteropServices.Marshal]::PtrToStructure($ptr, [type][AGY_CREDENTIAL])
$bytes = New-Object byte[] ([int]$cred.CredentialBlobSize)
[Runtime.InteropServices.Marshal]::Copy($cred.CredentialBlob, $bytes, 0, $bytes.Length)
[AgyCred]::CredFree($ptr)
[Console]::Out.Write([Text.Encoding]::UTF8.GetString($bytes))
`;
}

function emailFromIdToken(value: unknown): string | undefined {
  if (typeof value !== "string") {
    return undefined;
  }
  const parts = value.split(".");
  if (parts.length < 2) {
    return undefined;
  }
  try {
    const normalized = parts[1].replace(/-/gu, "+").replace(/_/gu, "/");
    const payload = Buffer.from(normalized, "base64").toString("utf8");
    const parsed: unknown = JSON.parse(payload);
    return isRecord(parsed) && typeof parsed.email === "string"
      ? parsed.email
      : undefined;
  } catch {
    return undefined;
  }
}
