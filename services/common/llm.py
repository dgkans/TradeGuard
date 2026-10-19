"""Chat model factory. One env var (LLM_PROVIDER) picks the provider (PLAN.md Section 15.2)."""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama

from services.common.config import ConfigError, Settings

# Large enough for the system prompt, 11 tool schemas and a few bar or scan results.
OLLAMA_CONTEXT_TOKENS = 16384
SEED = 0
# Groq free plan (F12): limits are per model; gpt-oss reasons, so keep it short and hidden.
GROQ_REASONING_EFFORT = "low"
GROQ_TIMEOUT_S = 60
# The free plan allows 8K tokens a minute per model: wait out a 429 (the SDK honours
# retry-after) rather than fail the turn.
GROQ_RETRIES = 5


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
    if settings.llm_provider == "groq":
        if settings.groq_api_key is None:
            raise ConfigError("LLM_PROVIDER=groq needs GROQ_API_KEY")
        extra: dict[str, Any] = {"include_reasoning": False}
        if json_mode:
            extra["response_format"] = {"type": "json_object"}
        return ChatGroq(
            model_name=model or settings.llm_model,
            api_key=settings.groq_api_key,
            temperature=0,
            reasoning_effort=GROQ_REASONING_EFFORT,
            model_kwargs=extra,
            timeout=GROQ_TIMEOUT_S,
            max_retries=GROQ_RETRIES,
        )
    raise ConfigError(
        f"LLM_PROVIDER={settings.llm_provider} is not wired up yet; use ollama or groq"
    )
