import { z } from "zod";
import type { DecisionTrace } from "./telemetry.ts";

export const objectIdSchema = z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9_-]{2,63}$/, "匿名設備IDを指定してください");
const maskedRefSchema = z.string().regex(/^masked:\/\/[a-zA-Z0-9][a-zA-Z0-9._/-]{2,255}$/);
const voiceInputSchema = z.object({
  transcript: z.string().trim().min(2).max(4_000),
  source: z.enum(["browser-speech", "aqua-paste", "manual"]),
  confirmed: z.literal(true),
}).strict();
const screenContextSchema = z.object({
  // These are human-confirmed, privacy-safe labels. Raw screen text is never stored here.
  observedSignals: z.array(z.enum(["anomaly", "forecast", "draft", "history", "normal"])).min(1).max(5),
  capturedAt: z.string().datetime(),
  capturedVia: z.enum(["window", "browser", "file"]),
  confirmed: z.literal(true),
}).strict();
export const missionInputSchema = z.object({
  objectId: objectIdSchema,
  request: z.string().trim().min(2).max(2000),
  source: z.enum(["vault", "demo"]).default("vault"),
  budgetUsd: z.number().finite().min(0).max(1).default(0.1),
  beforeImageRefs: z.array(maskedRefSchema).max(10).default([]),
  afterImageRefs: z.array(maskedRefSchema).max(10).default([]),
  screenImageRefs: z.array(maskedRefSchema).max(1).default([]),
  screenContext: screenContextSchema.optional(),
  voice: voiceInputSchema.optional(),
}).strict().superRefine((value, context) => {
  if (value.screenContext && value.screenImageRefs.length !== 1) {
    context.addIssue({ code: "custom", path: ["screenContext"], message: "画面の意味タグは検査済み画面フレーム1件と組み合わせてください。" });
  }
});
export type MissionInput = z.infer<typeof missionInputSchema>;
export type VoiceAssessment = {
  state: "not_provided" | "confirmed" | "safety_hold";
  source: "browser-speech" | "aqua-paste" | "manual" | null;
  questionDetected: boolean;
  matchedSignals: string[];
  screenRelation: "not_available" | "same_mission_only";
  reason: string;
};
export type SemanticAssessment = {
  state: "not_available" | "screen_only" | "voice_only" | "aligned" | "needs_confirmation" | "conflict" | "safety_hold";
  screenSignals: string[];
  voiceSignals: string[];
  sharedSignals: string[];
  conflicts: string[];
  capturedAt: string | null;
  reason: string;
};
export type Evidence = {
  id: string; path: string; line: number; text: string; digest: string;
  date: string | null; event: string | null; status: string | null; due: string | null;
  quarantined: boolean; safetyRisk: boolean; source: "vault" | "card" | "demo";
};
export type EvidenceSet = { records: Evidence[]; incomplete: boolean; notices: string[]; scannedFiles: number };
export type Alert = { rule: string; severity: "info" | "warning" | "high"; message: string; evidenceIds: string[] };
export type Scenario = {
  id: string; title: string; kind: "conditional-scenario"; premise: string;
  expected: string; risk: string; evidenceIds: string[]; verify: string;
  estimatedCost: null; estimatedDuration: null; probability: null;
};
export type MissionAction = { id: string; kind: "inspection-draft"; title: string; body: string; evidenceIds: string[] };
export type MissionReport = {
  schemaVersion: 1; missionId: string; input: MissionInput; createdAt: string; expiresAt: string;
  status: "ready" | "needs_confirmation" | "read_only";
  route: { intent: "history" | "anomaly" | "forecast" | "draft" | "clarify"; risk: "low" | "medium" | "high";
    reasons: string[]; selectedRoles: string[]; allowedOperations: string[]; recommendedModel: "low-cost" | "high-quality" };
  timeline: Evidence[]; snapshotDigest: string; incomplete: boolean; notices: string[];
  voice: VoiceAssessment;
  semantic: SemanticAssessment;
  alerts: Alert[]; scenarios: Scenario[]; actions: MissionAction[]; questions: string[];
  decisionTrace: DecisionTrace[];
  evidenceQuality: { datedRecords: number; latestDate: string | null; label: "insufficient" | "usable"; explanation: string };
  steps: { id: string; label: string; status: "completed" | "skipped" | "waiting"; detail: string }[];
  cost: { localModelCalls: number; localModelCostUsd: number; outerAgentCostUsd: number | null; savingsPercent: number | null; budgetUsd: number; explanation: string };
};
