import { defineState } from "eve/context";
import type { MissionReport } from "./types.ts";

export const missionState = defineState("miru.mission.v1", () => ({ report: null as MissionReport | null, locked: false }));
export const usageState = defineState("miru.usage.v1", () => ({
  orcaRouterFreeActive: false,
  inFlight: [] as { key: string; model: string; startedAt: number }[],
  entries: [] as { eventId: string; inputTokens: number | null; outputTokens: number | null; costUsd: number | null;
    costSource: "reported" | "unknown"; model: string; latencyMs: number | null; artifactId: string | null }[],
}));
