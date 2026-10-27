import assert from "node:assert/strict";
import { test } from "node:test";
import type { UsageScope } from "@getpaseo/plugin/server/usage";
import contribute from "../index.server.ts";
import {
  parseCredentialBlob,
  type CredentialResult,
} from "../server/credentials.ts";
import {
  discover,
  fetchUsage,
  reportFromQuotaResponse,
  USAGE_SOURCE_ID,
  USAGE_SOURCE_LABEL,
  windowsFromQuotaResponse,
} from "../server/usage.ts";
import { DEFAULT_KEYRING_TARGET, inputSchema } from "../shared/input.ts";

const QUOTA_RESPONSE = {
  groups: [
    {
      buckets: [
        {
          bucketId: "gemini-weekly",
          displayName: "Weekly Limit Remaining",
          window: "weekly",
          resetTime: "2026-10-22T16:06:07Z",
          description:
            "You have used some of your weekly limit, it will fully refresh in 6 days, 13 hours.",
          remainingFraction: 0.9470819,
        },
        {
          bucketId: "gemini-5h",
          displayName: "Five Hour Limit Remaining",
          window: "5h",
          resetTime: "2026-10-16T03:00:03Z",
          remainingFraction: 0.9391971,
        },
      ],
      displayName: "Gemini Models",
      description: "Models within this group: Gemini Flash, Gemini Pro",
    },
    {
      buckets: [
        {
          bucketId: "3p-weekly",
          displayName: "Weekly Limit Remaining",
          window: "weekly",
          resetTime: "2026-10-22T16:06:23Z",
          remainingFraction: 1,
        },
        {
          bucketId: "3p-5h",
          displayName: "Five Hour Limit Remaining",
          window: "5h",
          resetTime: "2026-10-16T07:06:32Z",
          remainingFraction: 1,
        },
      ],
      displayName: "Claude and GPT models",
      description: "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
    },
  ],
  description: "Within each group, models share a weekly limit and a 5-hour limit.",
};

function base64url(value: unknown): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}

function credentialBlob(overrides: {
  accessToken?: string;
  expiry?: string;
  idToken?: string;
}): string {
  return JSON.stringify({
    auth_method: "consumer",
    token: {
      access_token: overrides.accessToken ?? "ya29.antigravity-access-token",
      token_type: "Bearer",
      refresh_token: "1//refresh-token-should-never-be-used",
      expiry: overrides.expiry ?? "2026-10-16T10:13:38.9538207+08:00",
    },
    id_token:
      overrides.idToken ??
      `${base64url({ alg: "RS256" })}.${base64url({ email: "user@example.com" })}.sig`,
  });
}

function okCredential(blob = credentialBlob({})): CredentialResult {
  const credential = parseCredentialBlob(blob);
  assert.ok(credential, "test blob should parse");
  return { ok: true, credential };
}

function stubFetch(response: {
  status?: number;
  body?: unknown;
  throwError?: Error;
}): typeof fetch {
  return (async () => {
    if (response.throwError) {
      throw response.throwError;
    }
    const status = response.status ?? 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => null },
      json: async () => response.body,
      text: async () => JSON.stringify(response.body ?? null),
    };
  }) as unknown as typeof fetch;
}

test("parseCredentialBlob reads the access token, expiry and id_token email", () => {
  const credential = parseCredentialBlob(credentialBlob({}));
  assert.ok(credential);
  assert.equal(credential.accessToken, "ya29.antigravity-access-token");
  assert.equal(credential.email, "user@example.com");
  assert.equal(
    credential.expiresAt,
    Date.parse("2026-10-16T10:13:38.9538207+08:00"),
  );

  assert.equal(parseCredentialBlob("not json"), null);
  assert.equal(parseCredentialBlob(JSON.stringify({ token: {} })), null);
  assert.equal(
    parseCredentialBlob(JSON.stringify({ token: { access_token: "   " } })),
    null,
  );

  const noIdToken = parseCredentialBlob(
    credentialBlob({ idToken: "opaque-not-a-jwt" }),
  );
  assert.ok(noIdToken);
  assert.equal(noIdToken.email, undefined);
});

test("windowsFromQuotaResponse maps each group/bucket to a labelled window", () => {
  const windows = windowsFromQuotaResponse(QUOTA_RESPONSE);
  assert.equal(windows.length, 4);

  const byId = new Map(windows.map((window) => [window.id, window]));
  const weekly = byId.get("gemini-weekly");
  assert.ok(weekly);
  assert.equal(weekly.label, "Gemini Models · Weekly");
  assert.equal(weekly.shortLabel, "Gemini wk");
  assert.equal(weekly.resetsAt, "2026-10-22T16:06:07Z");
  assert.equal(weekly.tone, "ok");
  assert.ok(Math.abs((weekly.usedPct ?? 0) - 5.29181) < 1e-6);

  const fiveHour = byId.get("gemini-5h");
  assert.ok(fiveHour);
  assert.equal(fiveHour.label, "Gemini Models · 5-hour");
  assert.equal(fiveHour.shortLabel, "Gemini 5h");
  assert.equal(fiveHour.summary, true);

  const thirdParty = byId.get("3p-weekly");
  assert.ok(thirdParty);
  assert.equal(thirdParty.label, "Claude and GPT models · Weekly");
  assert.equal(thirdParty.shortLabel, "Claude+GPT wk");
  assert.equal(thirdParty.usedPct, 0);
  assert.equal(thirdParty.summary, undefined);

  assert.equal(byId.get("3p-5h")?.shortLabel, "Claude+GPT 5h");

  assert.deepEqual(windowsFromQuotaResponse({}), []);
  assert.deepEqual(
    windowsFromQuotaResponse({ groups: [{ buckets: [{ bucketId: "x" }] }] }),
    [],
  );
});

test("reportFromQuotaResponse surfaces per-group detail rows", () => {
  const report = reportFromQuotaResponse(QUOTA_RESPONSE);
  assert.equal(report.status, "available");
  if (report.status !== "available") {
    return;
  }
  assert.equal(report.windows.length, 4);
  assert.deepEqual(report.details, [
    {
      id: "group:0",
      label: "Gemini Models",
      value: "Gemini Flash, Gemini Pro",
    },
    {
      id: "group:1",
      label: "Claude and GPT models",
      value: "Claude Opus, Claude Sonnet, GPT-OSS",
    },
  ]);
});

test("fetchUsage returns an available report with real quota windows", async () => {
  let requestedUrl = "";
  let authorization = "";
  let userAgent = "";
  const fetchApi = (async (
    url: string,
    init: { headers?: Record<string, string> },
  ) => {
    requestedUrl = url;
    authorization = init.headers?.Authorization ?? "";
    userAgent = init.headers?.["User-Agent"] ?? "";
    return {
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => QUOTA_RESPONSE,
      text: async () => "",
    };
  }) as unknown as typeof fetch;

  const report = await fetchUsage(
    { target: DEFAULT_KEYRING_TARGET },
    { readCredential: async () => okCredential(), fetchApi, now: () => Date.parse("2026-10-15T00:00:00Z") },
  );

  assert.equal(report.status, "available");
  if (report.status === "available") {
    assert.equal(report.windows.length, 4);
  }
  assert.equal(
    requestedUrl,
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
  );
  // The endpoint gates on both the bearer token and an Antigravity user agent.
  assert.equal(authorization, "Bearer ya29.antigravity-access-token");
  assert.equal(userAgent, "antigravity/1.0");
});

test("fetchUsage reports missing, expired and rejected logins honestly", async () => {
  const missing = await fetchUsage(
    { target: DEFAULT_KEYRING_TARGET },
    { readCredential: async () => ({ ok: false, problem: { kind: "missing" } }) },
  );
  assert.equal(missing.status, "unavailable");
  if (missing.status === "unavailable" && missing.problem.kind === "no_quota") {
    assert.match(missing.problem.detail, /Run `agy` and sign in/);
  } else {
    assert.fail(`expected no_quota, got ${JSON.stringify(missing)}`);
  }

  const unsupported = await fetchUsage(
    { target: DEFAULT_KEYRING_TARGET },
    {
      readCredential: async () => ({
        ok: false,
        problem: { kind: "unsupported", platform: "linux" },
      }),
    },
  );
  assert.equal(unsupported.status, "unavailable");

  const expired = await fetchUsage(
    { target: DEFAULT_KEYRING_TARGET },
    {
      readCredential: async () =>
        okCredential(credentialBlob({ expiry: "2020-01-01T00:00:00Z" })),
      fetchApi: stubFetch({ status: 200, body: QUOTA_RESPONSE }),
      now: () => Date.parse("2026-10-15T00:00:00Z"),
    },
  );
  assert.equal(expired.status, "unavailable");
  if (expired.status === "unavailable" && expired.problem.kind === "expired") {
    assert.equal(expired.problem.refreshedBy, "agy");
  } else {
    assert.fail(`expected expired, got ${JSON.stringify(expired)}`);
  }

  const rejected = await fetchUsage(
    { target: DEFAULT_KEYRING_TARGET },
    {
      readCredential: async () => okCredential(),
      fetchApi: stubFetch({ status: 403 }),
      now: () => Date.parse("2026-10-15T00:00:00Z"),
    },
  );
  assert.equal(rejected.status, "unavailable");
  if (rejected.status === "unavailable" && rejected.problem.kind === "rejected") {
    assert.equal(rejected.problem.status, 403);
  } else {
    assert.fail(`expected rejected, got ${JSON.stringify(rejected)}`);
  }

  const failed = await fetchUsage(
    { target: DEFAULT_KEYRING_TARGET },
    {
      readCredential: async () => okCredential(),
      fetchApi: stubFetch({ throwError: new Error("socket hang up") }),
      now: () => Date.parse("2026-10-15T00:00:00Z"),
    },
  );
  assert.equal(failed.status, "error");
  if (failed.status === "error") {
    assert.match(failed.error, /socket hang up/);
  }
});

test("discover lists the machine login for global and Antigravity sessions only", async () => {
  const hostKeyPattern = /^[A-Za-z0-9._-]{1,128}$/;

  const globalScope: UsageScope = { kind: "global" };
  const accounts = await discover(globalScope, {
    readCredential: async () => okCredential(),
  });
  assert.equal(accounts.length, 1);
  const account = accounts[0];
  assert.ok(account);
  assert.match(account.key, hostKeyPattern);
  assert.equal(account.label, "user@example.com");
  assert.equal(account.harness, USAGE_SOURCE_LABEL);
  assert.deepEqual(account.input, { target: DEFAULT_KEYRING_TARGET });

  const sessionScope = (provider: string): UsageScope => ({
    kind: "session",
    provider,
    env: {},
  });
  const ours = await discover(sessionScope("antigravity-resilient"), {
    readCredential: async () => okCredential(),
  });
  assert.equal(ours.length, 1);
  const bundled = await discover(sessionScope("antigravity"), {
    readCredential: async () => okCredential(),
  });
  assert.equal(bundled.length, 1);
  const other = await discover(sessionScope("claude"), {
    readCredential: async () => okCredential(),
  });
  assert.deepEqual(other, []);

  const signedOut = await discover(globalScope, {
    readCredential: async () => ({ ok: false, problem: { kind: "missing" } }),
  });
  assert.deepEqual(signedOut, []);
});

test("inputSchema defaults and trims the keyring target", async () => {
  assert.deepEqual(await inputSchema.parseAsync({ target: "  custom:key  " }), {
    target: "custom:key",
  });
  assert.deepEqual(await inputSchema.parseAsync({}), {
    target: DEFAULT_KEYRING_TARGET,
  });
  assert.deepEqual(await inputSchema.parseAsync(null), {
    target: DEFAULT_KEYRING_TARGET,
  });
});

test("contribute registers the provider and the Antigravity usage source", () => {
  const registered: Array<{
    id: string;
    label: string;
    hasInput: boolean;
    hasDiscover: boolean;
    hasFetch: boolean;
  }> = [];
  let providerId = "";
  const cleanup = contribute({
    registerProvider(definition) {
      providerId = definition.id;
    },
    registerUsageSource(source) {
      registered.push({
        id: source.id,
        label: source.label,
        hasInput: typeof source.input?.parseAsync === "function",
        hasDiscover: typeof source.discover === "function",
        hasFetch: typeof source.fetch === "function",
      });
    },
  });

  assert.equal(providerId, "antigravity-resilient");
  assert.deepEqual(registered, [
    {
      id: USAGE_SOURCE_ID,
      label: USAGE_SOURCE_LABEL,
      hasInput: true,
      hasDiscover: true,
      hasFetch: true,
    },
  ]);
  assert.equal(typeof cleanup, "function");
});

test("fetchUsage prefers agy CLI /usage JSON payload when available", async () => {
  const cliPayload = {
    status: "SUCCESS",
    command: {
      name: "usage",
      data: {
        description: "Within each group, models share a weekly limit and a 5-hour limit.",
        groups: [
          {
            name: "Gemini Models",
            description: "Models within this group: Gemini Flash, Gemini Pro",
            buckets: [
              {
                id: "gemini-weekly",
                name: "Weekly Limit Remaining",
                window: "weekly",
                remaining_fraction: 0.69,
                reset_time: "2026-10-23T23:29:12Z",
              },
              {
                id: "gemini-5h",
                name: "Five Hour Limit Remaining",
                window: "5h",
                remaining_fraction: 0.85,
                reset_time: "2026-10-20T08:21:06Z",
              },
            ],
          },
        ],
      },
    },
  };

  let httpCalled = false;
  const report = await fetchUsage(
    { target: DEFAULT_KEYRING_TARGET },
    {
      readCredential: async () => okCredential(),
      readCliQuota: async () => cliPayload,
      fetchApi: (async () => {
        httpCalled = true;
        throw new Error("should not call HTTP fallback when CLI succeeds");
      }) as unknown as typeof fetch,
    },
  );

  assert.equal(httpCalled, false);
  assert.equal(report.status, "available");
  if (report.status === "available") {
    assert.equal(report.windows.length, 2);
    const weekly = report.windows.find((w) => w.id === "gemini-weekly");
    const fiveHour = report.windows.find((w) => w.id === "gemini-5h");
    assert.ok(weekly);
    assert.ok(fiveHour);
    assert.ok(Math.abs((weekly.usedPct ?? 0) - 31) < 1e-6);
    assert.ok(Math.abs((fiveHour.usedPct ?? 0) - 15) < 1e-6);
  }
});
