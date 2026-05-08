"""Tests for diagnosis engine behavior after demo provider removal."""

import pytest
from types import SimpleNamespace

from agent.engine import (
    SemanticCache,
    _before_tool_callback,
    _bounded_tool_response,
    _build_usage_event,
    _clean_function_call_name,
    _format_provider_error,
    _openrouter_model_name,
    _parse_diagnosis,
    _sanitize_function_call_names,
    _should_force_json_response,
    _should_set_gemini_thinking_config,
)


def test_parse_diagnosis_clean_json():
    """Clean JSON should be parsed directly."""
    text = '{"error": "OOMKilled", "cause": "Memory limit exceeded", "fix": "Increase limits"}'
    result = _parse_diagnosis(text)
    assert result["error"] == "OOMKilled"
    assert result["cause"] == "Memory limit exceeded"
    assert result["fix"] == "Increase limits"


def test_parse_diagnosis_markdown_wrapped():
    """JSON wrapped in markdown fences should be extracted."""
    text = '```json\n{"error": "ImagePullBackOff", "cause": "Bad tag", "fix": "Fix tag"}\n```'
    result = _parse_diagnosis(text)
    assert result["error"] == "ImagePullBackOff"


def test_parse_diagnosis_with_prose():
    """JSON embedded in prose text should be extracted."""
    text = 'The analysis shows: {"error": "CrashLoopBackOff", "cause": "Missing env var", "fix": "Add env var"} as the diagnosis.'
    result = _parse_diagnosis(text)
    assert result["error"] == "CrashLoopBackOff"


def test_parse_diagnosis_empty():
    """Empty input should return a structured fallback."""
    result = _parse_diagnosis("")
    assert "error" in result
    assert "cause" in result
    assert "fix" in result


def test_parse_diagnosis_pure_prose():
    """Pure prose with no JSON should still return structured output."""
    text = "The container is crashing because the memory limit is too low."
    result = _parse_diagnosis(text)
    assert "error" in result


def test_cache_differentiates_providers():
    """Cache should work correctly for LLM-generated diagnoses."""
    cache = SemanticCache(ttl_seconds=300)

    signals = {
        "healthStatus": "Degraded",
        "syncStatus": "Synced",
        "warningEvents": [
            {"reason": "OOMKilled", "message": "Container killed", "type": "Warning"}
        ]
    }

    diagnosis = {"error": "OOMKilled", "cause": "Memory limit exceeded", "fix": "Increase limits"}
    cache.set(signals, diagnosis)

    result = cache.get(signals)
    assert result == diagnosis


def test_no_demo_provider_imports():
    """Verify demo provider functions have been removed from engine."""
    from agent import engine
    assert not hasattr(engine, 'is_demo_provider')
    assert not hasattr(engine, '_run_demo_diagnosis')
    assert not hasattr(engine, '_build_rule_based_diagnosis')
    assert not hasattr(engine, 'DEMO_PROVIDERS')


def test_tool_budget_blocks_before_expensive_call():
    """Tool budget guard should block calls before the tool function runs."""
    context = SimpleNamespace(state={"_max_tool_calls": 1})
    tool = SimpleNamespace(name="get_events")

    assert _before_tool_callback(tool=tool, args={}, tool_context=context) is None
    blocked = _before_tool_callback(tool=tool, args={}, tool_context=context)

    assert blocked is not None
    assert "Tool budget exhausted" in blocked["error"]
    assert context.state["_tool_calls"] == 1
    assert context.state["_blocked_tool_calls"] == 1


def test_bounded_tool_response_truncates_large_payloads():
    """Large tool responses should be bounded before returning to the model."""
    result = _bounded_tool_response("x" * 2000, 500)

    assert len(result) <= 500
    assert "truncated" in result


def test_usage_event_reports_budget_and_token_state():
    """Usage event should expose token and tool-call telemetry."""
    event = _build_usage_event({
        "_llm_rounds": 2,
        "_llm_input_tokens_estimated": 1234,
        "_tool_calls": 3,
        "_blocked_tool_calls": 1,
        "_truncated_tool_responses": 2,
    })

    assert event["type"] == "usage"
    assert event["llm_rounds"] == 2
    assert event["tool_calls"] == 3
    assert event["blocked_tool_calls"] == 1


def test_provider_quota_error_is_actionable_and_redacted():
    """Verbose SDK quota errors should be safe and concise for UI display."""
    error = Exception(
        "429 RESOURCE_EXHAUSTED. API key TEST_API_KEY_SHOULD_NOT_LEAK "
        "Quota exceeded for metric generate_content_free_tier_requests. Please retry in 56s."
    )

    formatted = _format_provider_error(error)

    assert "quota exhausted" in formatted
    assert "Retry after about 56 seconds" in formatted
    assert "TEST_API_KEY_SHOULD_NOT_LEAK" not in formatted


def test_provider_unavailable_error_is_actionable():
    """Transient provider overload should not leak raw SDK traces."""
    formatted = _format_provider_error(Exception("503 UNAVAILABLE. model high demand"))

    assert "temporarily unavailable" in formatted
    assert "503" not in formatted


def test_openrouter_insufficient_credits_error_is_actionable():
    """OpenRouter credit failures should explain the account/model issue."""
    formatted = _format_provider_error(Exception('{"code":402,"message":"Insufficient credits"}'))

    assert "insufficient credits" in formatted
    assert "free model" in formatted


def test_gemini_json_mime_is_disabled_when_tools_are_attached():
    """Gemini does not support response_mime_type JSON together with tools."""
    assert _should_force_json_response("gemini", tools=[]) is True
    assert _should_force_json_response("gemini", tools=[object()]) is False
    assert _should_force_json_response("openai", tools=[]) is False


def test_gemini_thinking_config_only_applies_to_25_models():
    """Gemini 2.5 thinking can consume visible output budget."""
    assert _should_set_gemini_thinking_config("gemini", "gemini-2.5-flash") is True
    assert _should_set_gemini_thinking_config("gemini", "gemini-2.0-flash") is False
    assert _should_set_gemini_thinking_config("openai", "gemini-2.5-flash") is False


def test_openrouter_model_name_defaults_and_normalizes_prefix():
    """OpenRouter models are passed to LiteLLM with the openrouter prefix."""
    assert _openrouter_model_name() == "openrouter/openai/gpt-oss-20b:free"
    assert _openrouter_model_name("meta-llama/llama-3.3-70b-instruct:free") == "openrouter/meta-llama/llama-3.3-70b-instruct:free"
    assert _openrouter_model_name("openrouter/openai/gpt-oss-20b:free") == "openrouter/openai/gpt-oss-20b:free"


def test_provider_control_tokens_are_stripped_from_tool_names():
    """Some OpenAI-compatible providers append control tokens to tool names."""
    assert _clean_function_call_name("get_argocd_app<|channel|>commentary") == "get_argocd_app"
    assert _clean_function_call_name("get_resource") == "get_resource"


def test_sanitize_function_call_names_mutates_llm_response():
    """Tool names should be normalized before ADK resolves them."""
    response = SimpleNamespace(
        content=SimpleNamespace(
            parts=[
                SimpleNamespace(
                    function_call=SimpleNamespace(name="get_argocd_app<|channel|>commentary")
                )
            ]
        )
    )

    assert _sanitize_function_call_names(response) == 1
    assert response.content.parts[0].function_call.name == "get_argocd_app"
