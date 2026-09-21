import { defineHook } from "eve/hooks";
import { missionState, usageState } from "../lib/mission/state.ts";
import { checkBudget, normalizeUsageCost } from "../lib/mission/budget.ts";

export default defineHook({
  events: {
    "step.started"(event) {
      const report = missionState.get().report;
      if (report) checkBudget(usageState.get().entries, report.input.budgetUsd);
      const key = `${event.data.turnId}:${event.data.stepIndex}`;
      usageState.update(state => ({ ...state, inFlight: [...state.inFlight.filter(item => item.key !== key),
        { key, model: event.data.modelId, startedAt: Date.now() }].slice(-20) }));
    },
    "step.completed"(event) {
      const usage = event.data.usage;
      const cost = normalizeUsageCost(usage?.costUsd);
      const key = `${event.data.turnId}:${event.data.stepIndex}`;
      usageState.update(s => {
        if (s.entries.some(e => e.eventId === event.meta.id)) return s;
        const started = s.inFlight.find(item => item.key === key);
        return { ...s, inFlight: s.inFlight.filter(item => item.key !== key), entries: [...s.entries, {
          eventId: event.meta.id, inputTokens: usage?.inputTokens ?? null, outputTokens: usage?.outputTokens ?? null, ...cost,
          model: started?.model ?? "unknown", latencyMs: started ? Math.max(0, Date.now() - started.startedAt) : null,
          artifactId: missionState.get().report?.missionId ?? null,
        }].slice(-500) };
      });
    },
  },
});
