import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import type {
  LanguageModelV4,
  LanguageModelV4CallOptions,
  LanguageModelV4GenerateResult,
  LanguageModelV4StreamResult,
} from "@ai-sdk/provider";
import { WORKFLOW_DESERIALIZE, WORKFLOW_SERIALIZE } from "@ai-sdk/provider-utils";

export const ORCA_ROUTER_BASE_URL = "https://api.orcarouter.ai/v1";
export const ORCA_ROUTER_FREE_MODEL = "orcarouter/free";
// The provider's free-route context limit is not declared in the supplied API material.
// Keep the durable context deliberately conservative until ORCA publishes a guarantee.
export const ORCA_ROUTER_CONTEXT_WINDOW_TOKENS = 8_192;

function configuredApiKey(): string | null {
  const key = process.env.ORCAROUTER_API_KEY?.trim();
  return key && key.length >= 16 ? key : null;
}

/**
 * OpenAI-compatible model adapter for ORCA ROUTER.
 *
 * The key is looked up for each provider call and is deliberately not placed in
 * this model's serializable configuration. Eve can therefore persist a model
 * selection without persisting an Authorization header.
 */
export class OrcaRouterFreeModel implements LanguageModelV4 {
  readonly specificationVersion = "v4" as const;
  readonly provider = "orcarouter.chat";
  readonly supportsStructuredOutputs = false;

  readonly modelId: string;

  constructor(modelId = ORCA_ROUTER_FREE_MODEL) {
    this.modelId = modelId;
  }

  static [WORKFLOW_SERIALIZE](model: OrcaRouterFreeModel) {
    return { modelId: model.modelId };
  }

  static [WORKFLOW_DESERIALIZE](value: { modelId: string }) {
    return new OrcaRouterFreeModel(value.modelId);
  }

  get supportedUrls() {
    return this.delegate().supportedUrls;
  }

  doGenerate(options: LanguageModelV4CallOptions): PromiseLike<LanguageModelV4GenerateResult> {
    return this.delegate().doGenerate(options);
  }

  doStream(options: LanguageModelV4CallOptions): PromiseLike<LanguageModelV4StreamResult> {
    return this.delegate().doStream(options);
  }

  private delegate(): LanguageModelV4 {
    const apiKey = configuredApiKey();
    if (!apiKey) {
      throw new Error("ORCA ROUTERのAPIキーが未設定です。ORCAROUTER_API_KEYを環境変数に設定してください。");
    }
    return createOpenAICompatible({
      baseURL: ORCA_ROUTER_BASE_URL,
      name: "orcarouter",
      apiKey,
      includeUsage: true,
      supportsStructuredOutputs: false,
    }).chatModel(this.modelId);
  }
}

export function orcaRouterFreeModel(): OrcaRouterFreeModel | null {
  return configuredApiKey() ? new OrcaRouterFreeModel() : null;
}
