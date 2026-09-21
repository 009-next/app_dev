import { evaluateBusinessCases, summarizeBusinessEvaluation } from "../evals/business/runner.ts";

const rows = await evaluateBusinessCases();
console.log(JSON.stringify({ ...summarizeBusinessEvaluation(rows), rows }, null, 2));
