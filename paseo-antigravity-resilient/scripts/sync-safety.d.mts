export type TrackedDirtyEntry = {
  path: string;
  status: string;
};

export type RemoteSyncInspection = {
  branch: string;
  expectedBranch?: string;
  incomingChangedFiles?: string[];
  isAncestor: boolean;
  localHead: string;
  porcelainStatus: string;
  remoteHead: string;
};

export type RemoteSyncDecision = {
  action: "fast-forward" | "noop" | "refuse";
  branch: string;
  collidingUntracked: string[];
  expectedBranch: string;
  incomingChangedFiles: string[];
  isAncestor: boolean;
  localHead: string;
  nonCollidingUntracked: string[];
  reasons: string[];
  remoteHead: string;
  safe: boolean;
  trackedDirty: TrackedDirtyEntry[];
};

export function normalizeGitPath(rawPath: string): string;
export function pathsCollide(pathA: string, pathB: string): boolean;
export function parsePorcelainStatus(porcelainText: string): {
  trackedDirty: TrackedDirtyEntry[];
  untracked: string[];
};
export function evaluateRemoteSyncSafety(
  input: RemoteSyncInspection,
): RemoteSyncDecision;
