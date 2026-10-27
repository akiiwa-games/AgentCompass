import { defineRpc } from "@getpaseo/plugin";
import { z } from "zod";

export const fastModeStateSchema = z.object({
  enabled: z.boolean(),
  serviceTier: z.enum(["priority", "default"]),
  updatedAt: z.string().optional(),
});

export type FastModeState = z.output<typeof fastModeStateSchema>;

export const getFastModeRpc = defineRpc({
  name: "fast.get",
  input: z.object({}),
  output: fastModeStateSchema,
});

export const setFastModeRpc = defineRpc({
  name: "fast.set",
  input: z.object({
    enabled: z.boolean().optional(),
  }),
  output: fastModeStateSchema,
});
