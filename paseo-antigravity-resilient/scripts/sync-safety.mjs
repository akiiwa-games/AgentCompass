import { fileURLToPath } from "node:url";

export function normalizeGitPath(rawPath) {
  let cleaned = String(rawPath ?? "").trim();
  if (cleaned.startsWith('"') && cleaned.endsWith('"') && cleaned.length >= 2) {
    cleaned = cleaned.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, "\\");
  }
  cleaned = cleaned.replaceAll("\\", "/").replace(/^\.\//, "").replace(/^\/+|\/+$/g, "");
  return cleaned;
}

export function pathsCollide(pathA, pathB) {
  const a = normalizeGitPath(pathA);
  const b = normalizeGitPath(pathB);
  if (!a || !b) {
    return false;
  }
  return a === b || a.startsWith(`${b}/`) || b.startsWith(`${a}/`);
}

export function parsePorcelainStatus(porcelainText) {
  const trackedDirty = [];
  const untracked = [];

  for (const rawLine of String(porcelainText ?? "").split(/\r?\n/u)) {
    if (!rawLine || rawLine.length < 4) {
      continue;
    }
    const status = rawLine.slice(0, 2);
    const rawRest = rawLine.slice(3).trim();
    if (!rawRest || status === "!!") {
      continue;
    }
    if (status === "??") {
      const normalized = normalizeGitPath(rawRest);
      if (normalized) {
        untracked.push(normalized);
      }
      continue;
    }
    if (status !== "  ") {
      const parts = rawRest.includes(" -> ") ? rawRest.split(" -> ") : [rawRest];
      for (const part of parts) {
        const normalized = normalizeGitPath(part);
        if (normalized) {
          trackedDirty.push({
            status: status.trim() || status,
            path: normalized,
          });
        }
      }
    }
  }

  return { trackedDirty, untracked };
}

export function evaluateRemoteSyncSafety(input) {
  const expectedBranch = String(input?.expectedBranch ?? "main").trim() || "main";
  const branch = String(input?.branch ?? "").trim();
  const localHead = String(input?.localHead ?? "").trim();
  const remoteHead = String(input?.remoteHead ?? "").trim();
  const isAncestor = Boolean(input?.isAncestor) || (Boolean(localHead) && localHead === remoteHead);

  const { trackedDirty, untracked } = parsePorcelainStatus(input?.porcelainStatus ?? "");
  const incomingChangedFiles = Array.from(
    new Set(
      (Array.isArray(input?.incomingChangedFiles) ? input.incomingChangedFiles : [])
        .map((entry) => normalizeGitPath(entry))
        .filter(Boolean),
    ),
  );

  const collidingUntracked = [];
  const nonCollidingUntracked = [];
  for (const untrackedPath of untracked) {
    const collides = incomingChangedFiles.some((changedPath) =>
      pathsCollide(untrackedPath, changedPath),
    );
    if (collides) {
      collidingUntracked.push(untrackedPath);
    } else {
      nonCollidingUntracked.push(untrackedPath);
    }
  }

  const reasons = [];
  if (!localHead || !remoteHead) {
    reasons.push(
      `Missing commit SHA (remoteHead="${remoteHead}", localHead="${localHead}")`,
    );
  }
  if (branch !== expectedBranch) {
    reasons.push(
      `Remote checkout is on branch "${branch || "(detached HEAD)"}", expected "${expectedBranch}"`,
    );
  }
  if (localHead && remoteHead && remoteHead !== localHead && !isAncestor) {
    reasons.push(
      `Remote HEAD (${remoteHead}) has diverged from or is ahead of target HEAD (${localHead}); refusing non-fast-forward update`,
    );
  }
  if (trackedDirty.length > 0) {
    reasons.push(
      `Remote checkout has ${trackedDirty.length} uncommitted tracked change(s): ${trackedDirty
        .map((entry) => `${entry.status}:${entry.path}`)
        .join(", ")}`,
    );
  }
  if (collidingUntracked.length > 0) {
    reasons.push(
      `Remote checkout has ${collidingUntracked.length} untracked file(s) that would be overwritten by incoming changes: ${collidingUntracked.join(", ")}`,
    );
  }

  const safe = reasons.length === 0;
  const action = !safe
    ? "refuse"
    : remoteHead === localHead
      ? "noop"
      : "fast-forward";

  return {
    safe,
    action,
    reasons,
    branch,
    expectedBranch,
    localHead,
    remoteHead,
    isAncestor,
    trackedDirty,
    collidingUntracked,
    nonCollidingUntracked,
    incomingChangedFiles,
  };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const rawArg = process.argv[2];
  if (!rawArg) {
    console.error(
      "Usage: node scripts/sync-safety.mjs '<json-inspection-payload>'",
    );
    process.exit(2);
  }
  const parsed = JSON.parse(rawArg);
  const decision = evaluateRemoteSyncSafety(parsed);
  console.log(JSON.stringify(decision, null, 2));
  process.exit(decision.safe ? 0 : 1);
}
