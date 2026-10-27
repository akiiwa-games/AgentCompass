import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import type { ProviderEvent, ProviderTimelineItem } from "@getpaseo/plugin/server/provider";
import { DEFAULT_MODE, MODES } from "../server/internal/catalog.ts";
import { Session } from "../server/internal/session.ts";
import { ConversationStore } from "../server/internal/store.ts";
import { replayTimelineItem } from "../server/internal/timeline.ts";

const legacy: ProviderTimelineItem = {
  id: "turn-3:auto-compact", type: "notification", level: "info",
  message: "Auto-compacted context (172,979 tokens) 1m before 5m hot cache expiry.",
};

test("legacy idle compaction replays in chronological history, never opening notifications", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agy-replay-order-"));
  try {
    const store = new ConversationStore({ storeDir: dir });
    store.upsert("conv-order", { id: "turn-2:message:1", type: "assistant_message", text: "Morning" }, "2026-10-16T00:30:00Z");
    store.upsert("conv-order", legacy, "2026-10-16T00:38:33Z");
    store.upsert("conv-order", { id: "turn-4:message:1", type: "assistant_message", text: "Afternoon" }, "2026-10-16T07:06:40Z");
    store.flush("conv-order");
    const original = readFileSync(store.snapshotPath("conv-order"), "utf8");
    for (let reopen = 0; reopen < 3; reopen++) {
      const events: ProviderEvent[] = [];
      const session = new Session(`reopen-${reopen}`, "agy", [], {}, {
        cwd: dir, env: {}, mcpServers: {}, settings: {}, persist: true, mode: "full-access",
      }, { version: 1, data: { conversationId: "conv-order" } }, {
        models: [], modes: MODES, defaultMode: DEFAULT_MODE,
      }, (event) => events.push(event), {
        storeDir: dir, spawn: () => { throw new Error("History replay must never start agy or compact"); },
      });
      session.open("open", ["session.persistence"], "replay");
      const timeline = events.filter((event): event is Extract<ProviderEvent, { type: "timeline.item" }> => event.type === "timeline.item");
      assert.deepEqual(timeline.map((e) => e.item.id), ["turn-2:message:1", "turn-3:auto-compact", "turn-4:message:1"]);
      assert.equal(timeline[1].timestamp, "2026-10-16T00:38:33Z");
      assert.deepEqual(timeline[1].item, { id: legacy.id, type: "compaction", status: "completed", trigger: "auto", preTokens: 172979 });
      // Installed Paseo 0.11.2 PluginAgentSession extracts precisely this list
      // and appends it on registration when durable history already exists.
      assert.deepEqual(timeline.filter((e) => e.item.type === "notification"), []);
      assert.equal(events.some((e) => e.type === "session.turn"), false);
      await session.dispose();
    }
    assert.equal(readFileSync(store.snapshotPath("conv-order"), "utf8"), original, "Read-time migration must not rewrite historical data");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("legacy compaction migration is narrow and idempotent", () => {
  const migrated = replayTimelineItem(legacy);
  assert.equal(replayTimelineItem(migrated), migrated);
  for (const other of [
    { ...legacy, id: "permission-warning" },
    { ...legacy, message: "Other warning" },
    { ...legacy, message: "Auto-compacted context (99999999999999999999 tokens) 1m before 5m hot cache expiry." },
  ]) assert.equal(replayTimelineItem(other), other);
});
