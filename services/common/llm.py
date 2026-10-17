"""Chat model factory. One env var (LLM_PROVIDER) picks the provider (PLAN.md Section 15.2)."""

from langchain_core.language_models import BaseChatModel
from langchain_ollama import ChatOllama

from services.common.config import ConfigError, Settings

# Large enough for the system prompt, 11 tool schemas and a few bar or scan results.
OLLAMA_CONTEXT_TOKENS = 16384
SEED = 0


def make_chat_model(
    settings: Settings, *, json_mode: bool = False, model: str | None = None
) -> BaseChatModel:
    """Build the chat model named by settings (or `model`), at temperature 0.

    `json_mode` constrains output to JSON (the understand step and the intent judge).
    Raises ConfigError for providers that are not wired up yet.
    """
    if settings.llm_provider == "ollama":
        return ChatOllama(
            model=model or settings.llm_model,
            base_url=settings.ollama_base_url,
            temperature=0,
            seed=SEED,
            num_ctx=OLLAMA_CONTEXT_TOKENS,
            # qwen3 "thinking" adds latency and tokens; the graph has its own plan step.
            reasoning=False,
            format="json" if json_mode else None,
        )
    raise ConfigError(
        f"LLM_PROVIDER={settings.llm_provider} is not wired up yet (planned for F12); "
        "use LLM_PROVIDER=ollama"
    )
