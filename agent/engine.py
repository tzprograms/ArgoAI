# ArgoCD Diagnostic Agent - A2A-Based Routing Architecture
#
# Design Principles:
# 1. A2A ROUTING: Agents expose capabilities via AgentCards
# 2. TIERED TRIAGE: Fast heuristics → LLM fallback for novel issues
# 3. SEMANTIC CACHING: Avoid redundant LLM calls for similar issues
# 4. LOG FILTERING: Pre-process logs to extract only errors/warnings
# 5. ANTI-HALLUCINATION: Only output facts from tool results
#
# Specialist Agents (each with an AgentCard):
# - Runtime Analyzer: Pod crashes, OOM, image pulls, scheduling
# - Config Analyzer: ArgoCD sync, manifests, Git issues
# - Network Analyzer: Service connectivity, DNS, TLS, Ingress
# - Storage Analyzer: PVC, volume mounts, storage class
# - RBAC Analyzer: Permissions, ServiceAccounts, roles

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, AsyncIterator, Optional
from dataclasses import dataclass

from google.adk.agents.llm_agent import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from agent import metrics as prom_metrics

logger = logging.getLogger(__name__)


# =============================================================================
# TOKEN / TOOL BUDGET GUARDS
# =============================================================================

ABSOLUTE_TOOL_CALL_LIMIT = 5
DEFAULT_TOOL_CALL_LIMIT = 3
DEFAULT_TOOL_RESPONSE_CHARS = 1200
DEFAULT_LLM_MAX_OUTPUT_TOKENS = 1024
DEFAULT_GEMINI_THINKING_BUDGET = 0
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-oss-20b:free"
PROVIDER_ERROR_MAX_CHARS = 700


def _int_env(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    """Read a bounded integer environment variable."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _float_env(name: str, default: float) -> float:
    """Read a float environment variable with a safe fallback."""
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _configured_tool_call_limit() -> int:
    """Return the per-diagnosis tool-call limit, capped by the project hard max."""
    return _int_env("MAX_TOOL_CALLS", DEFAULT_TOOL_CALL_LIMIT, minimum=0, maximum=ABSOLUTE_TOOL_CALL_LIMIT)


def _configured_tool_response_chars() -> int:
    """Return max chars allowed back into the model for a single tool result."""
    return _int_env("MAX_TOOL_RESPONSE_CHARS", DEFAULT_TOOL_RESPONSE_CHARS, minimum=300, maximum=4000)


def _configured_llm_output_tokens() -> int:
    """Return max model output tokens for one model round."""
    return _int_env("LLM_MAX_OUTPUT_TOKENS", DEFAULT_LLM_MAX_OUTPUT_TOKENS, minimum=256, maximum=4096)


def _configured_gemini_thinking_budget() -> int:
    """Return Gemini 2.5 thinking budget; default off to preserve visible output tokens."""
    return _int_env("GEMINI_THINKING_BUDGET", DEFAULT_GEMINI_THINKING_BUDGET, minimum=0, maximum=8192)


def _truncate_text(text: str, max_chars: int) -> str:
    """Bound text while making truncation explicit to the model."""
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    marker = f"\n...[truncated {omitted} chars; use available evidence, do not repeat this tool for the same data]"
    keep = max(0, max_chars - len(marker))
    return text[:keep].rstrip() + marker


def _compact_error_text(error: Exception) -> str:
    """Return a short, redacted provider error for UI and logs."""
    text = str(error)
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED_GOOGLE_API_KEY]", text)
    text = re.sub(r"sk-[0-9A-Za-z_-]{20,}", "[REDACTED_API_KEY]", text)
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _is_known_provider_error(error: Exception) -> bool:
    text = str(error).upper()
    return any(
        marker in text
        for marker in (
            "RESOURCE_EXHAUSTED",
            "QUOTA",
            "API KEY NOT VALID",
            "PERMISSION_DENIED",
            "UNAUTHENTICATED",
            "UNAVAILABLE",
            "INSUFFICIENT CREDITS",
            "429",
            "401",
            "402",
            "403",
            "503",
        )
    )


def _format_provider_error(error: Exception) -> str:
    """Map verbose SDK exceptions to actionable user-facing errors."""
    text = _compact_error_text(error)
    upper = text.upper()

    if "RESOURCE_EXHAUSTED" in upper or "QUOTA" in upper or "429" in upper:
        retry = re.search(r"(?:RETRY(?: IN|DELAY)?[^0-9]{0,20})([0-9]+(?:\.[0-9]+)?)S", text, re.IGNORECASE)
        retry_text = ""
        if retry:
            retry_seconds = max(1, round(float(retry.group(1))))
            retry_text = f" Retry after about {retry_seconds} seconds."
        return (
            "LLM provider quota exhausted for this API key/model. "
            "Enable billing, wait for quota reset, reduce usage, or use a different key/provider/model."
            f"{retry_text}"
        )

    if "INSUFFICIENT CREDITS" in upper or "CODE\":402" in upper or " 402" in upper:
        return "LLM provider rejected the request because the OpenRouter account has insufficient credits for this model. Add credits or choose a free model."

    if "API KEY NOT VALID" in upper or "UNAUTHENTICATED" in upper or "401" in upper:
        return "LLM provider rejected the API key. Verify the key is active and belongs to the selected provider."

    if "PERMISSION_DENIED" in upper or "403" in upper:
        return "LLM provider denied access for this API key/model. Check API enablement, billing, and model permissions."

    if "UNAVAILABLE" in upper or "503" in upper:
        return "LLM provider/model is temporarily unavailable or overloaded. Retry later or choose another model/provider."

    return _truncate_text(text, PROVIDER_ERROR_MAX_CHARS)


def _should_force_json_response(provider: str, tools: list[Any]) -> bool:
    """Gemini JSON MIME mode is incompatible with function calling."""
    return provider in ("gemini", "google") and not tools


def _should_set_gemini_thinking_config(provider: str, model_name: str) -> bool:
    """Gemini 2.5 models otherwise spend output budget on hidden reasoning."""
    return provider in ("gemini", "google") and "2.5" in model_name


def _openrouter_model_name(model_name: str = "") -> str:
    """Return a LiteLLM model name for OpenRouter."""
    selected = model_name or DEFAULT_OPENROUTER_MODEL
    if selected.startswith("openrouter/"):
        return selected
    return f"openrouter/{selected}"


def _bounded_tool_response(tool_response: Any, max_chars: int) -> Any:
    """Bound tool responses before ADK feeds them into the next model turn."""
    if isinstance(tool_response, str):
        return _truncate_text(tool_response, max_chars)

    try:
        serialized = json.dumps(tool_response, default=str, ensure_ascii=False)
    except TypeError:
        serialized = str(tool_response)

    if len(serialized) <= max_chars:
        return tool_response

    return {
        "result": _truncate_text(serialized, max_chars),
        "truncated": True,
    }


def _estimate_any_chars(value: Any) -> int:
    """Approximate serialized chars for token observability without provider calls."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        if hasattr(value, "model_dump"):
            return len(json.dumps(value.model_dump(mode="json", exclude_none=True), default=str))
        return len(json.dumps(value, default=str))
    except Exception:
        return len(str(value))


def _estimate_request_tokens(llm_request: Any) -> int:
    """Estimate prompt tokens for requests when provider usage is not yet available."""
    chars = 0
    for content in getattr(llm_request, "contents", []) or []:
        for part in getattr(content, "parts", []) or []:
            chars += _estimate_any_chars(getattr(part, "text", None))
            chars += _estimate_any_chars(getattr(part, "function_call", None))
            chars += _estimate_any_chars(getattr(part, "function_response", None))

    config = getattr(llm_request, "config", None)
    if config is not None:
        chars += _estimate_any_chars(getattr(config, "system_instruction", None))
        chars += _estimate_any_chars(getattr(config, "tools", None))

    tools_dict = getattr(llm_request, "tools_dict", None) or {}
    chars += sum(len(name) for name in tools_dict)

    return max(1, chars // 4)


def _increment_state(context: Any, key: str, amount: int) -> None:
    context.state[key] = int(context.state.get(key, 0) or 0) + int(amount)


def _usage_value(usage: Any, *names: str) -> int:
    for name in names:
        value = getattr(usage, name, None)
        if isinstance(value, int):
            return value
    return 0


def _clean_function_call_name(name: str) -> str:
    """Strip provider-specific control tokens from function call names."""
    if not name:
        return name
    return re.split(r"[^A-Za-z0-9_-]", name, maxsplit=1)[0]


def _sanitize_function_call_names(llm_response: Any) -> int:
    """Normalize tool names before ADK resolves them."""
    changed = 0
    content = getattr(llm_response, "content", None)
    for part in getattr(content, "parts", []) or []:
        function_call = getattr(part, "function_call", None)
        if not function_call:
            continue
        name = getattr(function_call, "name", "")
        clean_name = _clean_function_call_name(name)
        if clean_name and clean_name != name:
            try:
                function_call.name = clean_name
                changed += 1
            except Exception:
                logger.debug("Could not sanitize function call name", exc_info=True)
    return changed


def _before_model_callback(callback_context: Any, llm_request: Any) -> None:
    """Record estimated input tokens before each model round."""
    provider = callback_context.state.get("_provider", "unknown")
    estimated = _estimate_request_tokens(llm_request)
    _increment_state(callback_context, "_llm_input_tokens_estimated", estimated)
    _increment_state(callback_context, "_llm_rounds", 1)
    prom_metrics.LLM_TOKENS_USED.labels(provider=provider, type="input_estimated").inc(estimated)
    return None


def _after_model_callback(callback_context: Any, llm_response: Any) -> None:
    """Record actual provider token usage when ADK exposes it."""
    provider = callback_context.state.get("_provider", "unknown")
    status = "error" if getattr(llm_response, "error_code", None) else "success"
    prom_metrics.LLM_REQUESTS.labels(provider=provider, status=status).inc()
    sanitized = _sanitize_function_call_names(llm_response)
    if sanitized:
        _increment_state(callback_context, "_sanitized_tool_calls", sanitized)

    usage = getattr(llm_response, "usage_metadata", None)
    if usage is None:
        return None

    input_tokens = _usage_value(usage, "prompt_token_count", "input_token_count")
    output_tokens = _usage_value(usage, "candidates_token_count", "output_token_count")
    total_tokens = _usage_value(usage, "total_token_count")

    if input_tokens:
        _increment_state(callback_context, "_llm_input_tokens", input_tokens)
        prom_metrics.LLM_TOKENS_USED.labels(provider=provider, type="input").inc(input_tokens)
    if output_tokens:
        _increment_state(callback_context, "_llm_output_tokens", output_tokens)
        prom_metrics.LLM_TOKENS_USED.labels(provider=provider, type="output").inc(output_tokens)
    if total_tokens:
        _increment_state(callback_context, "_llm_total_tokens", total_tokens)
        prom_metrics.LLM_TOKENS_USED.labels(provider=provider, type="total").inc(total_tokens)

    return None


def _on_model_error_callback(callback_context: Any, llm_request: Any, error: Exception) -> None:
    """Record model failures without masking the real provider error."""
    _ = llm_request
    provider = callback_context.state.get("_provider", "unknown")
    prom_metrics.LLM_REQUESTS.labels(provider=provider, status="error").inc()
    logger.warning(
        "LLM request failed",
        extra={"provider": provider, "error": _format_provider_error(error)},
    )
    return None


def _before_tool_callback(tool: Any, args: dict[str, Any], tool_context: Any) -> Optional[dict]:
    """Hard-stop expensive tool execution before ADK calls the tool."""
    _ = args
    max_calls = int(tool_context.state.get("_max_tool_calls", _configured_tool_call_limit()) or 0)
    calls = int(tool_context.state.get("_tool_calls", 0) or 0)
    tool_name = getattr(tool, "name", "unknown_tool")

    if calls >= max_calls:
        _increment_state(tool_context, "_blocked_tool_calls", 1)
        tool_context.state["_tool_limit_reached"] = True
        return {
            "error": (
                f"Tool budget exhausted after {calls} allowed calls. "
                "Do not call more tools. Produce the final strict JSON diagnosis "
                "from the pre-loaded signals and prior tool results."
            )
        }

    tool_context.state["_tool_calls"] = calls + 1
    tool_names = list(tool_context.state.get("_tool_names", []) or [])
    tool_names.append(tool_name)
    tool_context.state["_tool_names"] = tool_names
    return None


def _after_tool_callback(tool: Any, args: dict[str, Any], tool_context: Any, tool_response: Any) -> Any:
    """Bound tool output so each model round cannot balloon the prompt."""
    _ = args
    max_chars = int(tool_context.state.get("_max_tool_response_chars", _configured_tool_response_chars()) or DEFAULT_TOOL_RESPONSE_CHARS)
    bounded = _bounded_tool_response(tool_response, max_chars)
    if bounded != tool_response:
        _increment_state(tool_context, "_truncated_tool_responses", 1)
        logger.info("Truncated tool response", extra={"tool": getattr(tool, "name", "unknown_tool"), "max_chars": max_chars})
    return bounded


def _build_usage_event(state: dict[str, Any]) -> Optional[dict]:
    """Build an SSE usage event from session state."""
    rounds = int(state.get("_llm_rounds", 0) or 0)
    tool_calls = int(state.get("_tool_calls", 0) or 0)
    blocked_tool_calls = int(state.get("_blocked_tool_calls", 0) or 0)
    estimated_input = int(state.get("_llm_input_tokens_estimated", 0) or 0)
    actual_input = int(state.get("_llm_input_tokens", 0) or 0)
    actual_output = int(state.get("_llm_output_tokens", 0) or 0)
    actual_total = int(state.get("_llm_total_tokens", 0) or 0)
    truncated = int(state.get("_truncated_tool_responses", 0) or 0)

    if not any([rounds, tool_calls, blocked_tool_calls, estimated_input, actual_total, truncated]):
        return None

    display_total = actual_total or (actual_input + actual_output) or estimated_input
    token_label = "actual" if actual_total or actual_input or actual_output else "estimated input"
    content = (
        f"LLM rounds={rounds}, {token_label} tokens={display_total}, "
        f"tool calls={tool_calls}, blocked={blocked_tool_calls}, truncated results={truncated}"
    )
    return {
        "type": "usage",
        "content": content,
        "llm_rounds": rounds,
        "input_tokens": actual_input,
        "output_tokens": actual_output,
        "total_tokens": actual_total,
        "estimated_input_tokens": estimated_input,
        "tool_calls": tool_calls,
        "blocked_tool_calls": blocked_tool_calls,
        "truncated_tool_responses": truncated,
    }


# =============================================================================
# SEMANTIC CACHE - Avoid redundant LLM calls for diagnosis results
# =============================================================================

@dataclass
class CacheEntry:
    diagnosis: dict
    timestamp: float
    hit_count: int = 0


class SemanticCache:
    """Cache diagnosis results by semantic fingerprint.

    QUOTA-SURGICAL: Increased TTL to 15 minutes (900s) to maximize cache hits.

    SECURITY: Fingerprints include cause-specific details to prevent
    wrong diagnoses being reused across apps with different concrete causes.

    Example: Two ImagePullBackOff apps with different bad images should NOT
    share cached diagnoses because the fix is image-specific.

    Fingerprint INCLUDES (for specificity):
    - Health/sync status
    - Warning event reasons
    - Cause-specific details (image name, PVC name, configmap name)

    Fingerprint EXCLUDES (for reusability):
    - App name, pod names (transient identifiers)
    - Timestamps
    - Event counts
    """

    def __init__(self, ttl_seconds: int = 900, max_entries: int = 100):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._cache: dict[str, CacheEntry] = {}

    def _extract_cause_details(self, warnings: list) -> str:
        """Extract cause-shaping details from warning events.

        Includes details that differentiate similar-looking issues:
        - Image name/tag for ImagePull errors
        - Resource limit class for OOM
        - ConfigMap/Secret name for missing config
        - Error reason codes
        """
        if not warnings:
            return ""

        details = []
        for w in warnings[:3]:  # Check first 3 warnings
            reason = w.get('reason', '')
            message = w.get('message', '')

            # Extract image name for ImagePullBackOff
            if reason in ('Failed', 'ErrImagePull', 'ImagePullBackOff'):
                # Match image references like "nginx:bad-tag" or "registry.io/image:v1"
                image_match = re.search(r'image\s+"?([^\s"]+)"?', message, re.IGNORECASE)
                if image_match:
                    details.append(f"image:{image_match.group(1)}")

            # Extract resource info for OOM
            elif reason == 'OOMKilled':
                limit_match = re.search(r'limit[:\s]+(\d+[MGK]i?)', message, re.IGNORECASE)
                if limit_match:
                    details.append(f"limit:{limit_match.group(1)}")

            # Extract configmap/secret name for config errors
            elif reason in ('CreateContainerConfigError', 'FailedMount'):
                cm_match = re.search(r'configmap\s+"?([^\s"]+)"?', message, re.IGNORECASE)
                if cm_match:
                    details.append(f"configmap:{cm_match.group(1)}")
                secret_match = re.search(r'secret\s+"?([^\s"]+)"?', message, re.IGNORECASE)
                if secret_match:
                    details.append(f"secret:{secret_match.group(1)}")

            # Extract PVC name for storage errors
            elif reason in ('FailedScheduling', 'ProvisioningFailed'):
                pvc_match = re.search(r'(?:pvc|persistentvolumeclaim)[:/\s]+"?([^\s"]+)"?', message, re.IGNORECASE)
                if pvc_match:
                    details.append(f"pvc:{pvc_match.group(1)}")

            # Always include the reason
            if reason:
                details.append(f"reason:{reason}")

        return '|'.join(sorted(set(details)))

    def _fingerprint(self, signals: dict) -> str:
        """Create semantic fingerprint from key diagnostic signals.

        QUOTA-SURGICAL: Refined to maximize cache hits while maintaining accuracy.

        Includes:
        - Health/sync status (general app state)
        - Warning reasons (error types)
        - Cause-specific details (image name, configmap name, etc.)
        - Container state reasons from podStatuses (OOMKilled, etc.)

        Excludes:
        - App name (fingerprint should be reusable across similar issues)
        - Pod names (transient identifiers)
        - Timestamps
        - Event counts
        - Specific restart counts
        """
        health = signals.get('healthStatus', '')
        sync = signals.get('syncStatus', '')
        warnings = signals.get('warningEvents') or []

        # Get all unique reasons from warnings (up to 5)
        reasons = sorted(set(w.get('reason', '') for w in warnings[:5] if w.get('reason')))

        # Extract cause-specific details from warnings
        cause_details = self._extract_cause_details(warnings)

        # Extract container state reasons from pre-loaded pod statuses
        pod_statuses = signals.get('podStatuses') or []
        state_reasons = set()
        for ps in pod_statuses[:5]:
            if ps.get('stateReason'):
                state_reasons.add(ps.get('stateReason'))
            if ps.get('lastTerminatedReason'):
                state_reasons.add(ps.get('lastTerminatedReason'))

        # Build fingerprint with all relevant signals
        key_parts = [
            health,
            sync,
            ','.join(reasons),
            cause_details,
            ','.join(sorted(state_reasons))
        ]
        fingerprint_str = '|'.join(key_parts)
        return hashlib.sha256(fingerprint_str.encode()).hexdigest()[:20]

    def get(self, signals: dict) -> Optional[dict]:
        """Get cached diagnosis if available and fresh."""
        fp = self._fingerprint(signals)
        entry = self._cache.get(fp)

        if entry is None:
            return None

        if time.time() - entry.timestamp > self.ttl:
            del self._cache[fp]
            return None

        entry.hit_count += 1
        logger.info(f"Cache hit for fingerprint {fp} (hits: {entry.hit_count})")
        return entry.diagnosis

    def set(self, signals: dict, diagnosis: dict):
        """Cache a diagnosis result."""
        if len(self._cache) >= self.max_entries:
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k].timestamp)
            del self._cache[oldest_key]

        fp = self._fingerprint(signals)
        self._cache[fp] = CacheEntry(diagnosis=diagnosis, timestamp=time.time())
        logger.info(f"Cached diagnosis for fingerprint {fp}")


# Global cache instance - QUOTA-SURGICAL: 15 minute TTL
_diagnosis_cache = SemanticCache(ttl_seconds=900)


# =============================================================================
# LOG FILTERING - Extract only relevant errors/warnings
# =============================================================================

class LogFilter:
    """Pre-process logs to extract only diagnostic-relevant content."""

    ERROR_PATTERNS = [
        r'\b(error|err|fail|failed|failure|exception|panic|fatal)\b',
        r'\b(oom|killed|crashloop|backoff|timeout|refused)\b',
        r'\b(denied|forbidden|unauthorized|permission)\b',
        r'\b(not found|missing|invalid|cannot|unable)\b',
        r'\bexit\s*(code|status)?\s*[1-9]',
    ]

    LEVEL_PATTERNS = {
        'error': r'\b(ERROR|ERR|FATAL|PANIC|CRITICAL)\b',
        'warn': r'\b(WARN|WARNING)\b',
        'info': r'\b(INFO|DEBUG|TRACE)\b',
    }

    def __init__(self, max_output_chars: int = 2000):
        self.max_output = max_output_chars
        self._error_re = re.compile('|'.join(self.ERROR_PATTERNS), re.IGNORECASE)
        self._level_res = {k: re.compile(v) for k, v in self.LEVEL_PATTERNS.items()}

    def filter_logs(self, raw_logs: str) -> str:
        """Filter logs to extract only error-relevant content."""
        if not raw_logs:
            return "No logs available"

        lines = raw_logs.split('\n')
        errors = []
        warnings = []

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue

            if self._level_res['error'].search(line):
                errors.append(line)
                for j in range(i+1, min(i+3, len(lines))):
                    if lines[j].strip():
                        errors.append(lines[j].strip())
            elif self._level_res['warn'].search(line):
                warnings.append(line)
            elif self._error_re.search(line):
                errors.append(line)

        output_lines = []

        if errors:
            output_lines.append("=== ERRORS ===")
            seen = set()
            for e in errors:
                normalized = re.sub(r'\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|[0-9a-f-]{36}', '', e)
                if normalized not in seen:
                    seen.add(normalized)
                    output_lines.append(e[:200])
                    if len(output_lines) > 15:
                        break

        if warnings and len('\n'.join(output_lines)) < self.max_output // 2:
            output_lines.append("\n=== WARNINGS ===")
            for w in warnings[:5]:
                output_lines.append(w[:150])

        result = '\n'.join(output_lines)

        if not result.strip() or result == "No logs available":
            last_lines = '\n'.join(lines[-20:])
            return f"(No explicit errors found. Last 20 lines:)\n{last_lines}"[:self.max_output]

        return result[:self.max_output]

    def filter_events(self, events: list[dict]) -> str:
        """Filter K8s events to prioritize Warnings."""
        if not events:
            return "No events found"

        warnings = [e for e in events if e.get('type') == 'Warning']

        if not warnings:
            return "No warning events (cluster appears healthy)"

        warnings.sort(key=lambda e: e.get('count', 0), reverse=True)

        lines = []
        seen_reasons = set()

        for e in warnings[:8]:
            reason = e.get('reason', 'Unknown')
            if reason in seen_reasons:
                continue
            seen_reasons.add(reason)

            msg = e.get('message', '')[:150]
            obj = e.get('object', '')
            count = e.get('count', 1)

            lines.append(f"[{reason}] ({count}x) {obj}: {msg}")

        return '\n'.join(lines)


# Global filter instance
_log_filter = LogFilter()


# =============================================================================
# MODEL FACTORY
# =============================================================================

_api_key_local = threading.local()


def _get_thread_api_key(provider: str) -> str:
    """Get API key from thread-local storage."""
    keys = getattr(_api_key_local, 'keys', {})
    return keys.get(provider, '')


def _set_thread_api_key(provider: str, api_key: str):
    """Set API key in thread-local storage (thread-safe)."""
    if not hasattr(_api_key_local, 'keys'):
        _api_key_local.keys = {}
    _api_key_local.keys[provider] = api_key


def _clear_thread_api_keys():
    """Clear all API keys from thread-local storage."""
    _api_key_local.keys = {}


def _create_model(provider: str, api_key: str, model_name: str = ""):
    """Create model with thread-safe credential handling.

    SECURITY: API keys are stored in thread-local storage, not os.environ,
    to prevent concurrent requests from overwriting each other's credentials.

    Limitation: Some LLM libraries (ADK Gemini, LiteLLM) read from environment
    variables internally. We set env vars only within the scope of a request
    and document this limitation. For true isolation, consider process-per-request
    or provider SDK instances with explicit credential injection.
    """
    _set_thread_api_key(provider, api_key)

    if provider in ("gemini", "google"):
        from google.adk.models import Gemini
        # ADK Gemini uses google-genai client which can accept api_key directly
        # via environment or client config. We use a context-managed approach.
        # NOTE: Gemini client reads GOOGLE_API_KEY. For thread safety in async,
        # this is a known limitation. Document and mitigate by using async locks
        # in high-concurrency scenarios, or switch to SDK with explicit key injection.
        if api_key:
            os.environ["GOOGLE_API_KEY"] = api_key
        return Gemini(model=model_name or DEFAULT_GEMINI_MODEL)

    elif provider in ("openai", "chatgpt"):
        from google.adk.models import LiteLlm
        # LiteLLM supports api_key parameter for some providers
        model_str = f"openai/{model_name or 'gpt-4o-mini'}"
        if api_key:
            # Try to use LiteLLM's api_key parameter if supported
            try:
                return LiteLlm(model=model_str, api_key=api_key)
            except TypeError:
                # Fall back to env var if api_key param not supported
                os.environ["OPENAI_API_KEY"] = api_key
                return LiteLlm(model=model_str)
        return LiteLlm(model=model_str)

    elif provider in ("anthropic", "claude"):
        from google.adk.models import LiteLlm
        model_str = f"anthropic/{model_name or 'claude-3-haiku-20240307'}"
        if api_key:
            try:
                return LiteLlm(model=model_str, api_key=api_key)
            except TypeError:
                os.environ["ANTHROPIC_API_KEY"] = api_key
                return LiteLlm(model=model_str)
        return LiteLlm(model=model_str)

    elif provider == "groq":
        from google.adk.models import LiteLlm
        model_str = f"groq/{model_name or 'llama-3.1-8b-instant'}"
        if api_key:
            try:
                return LiteLlm(model=model_str, api_key=api_key)
            except TypeError:
                os.environ["GROQ_API_KEY"] = api_key
                return LiteLlm(model=model_str)
        return LiteLlm(model=model_str)

    elif provider == "openrouter":
        from google.adk.models import LiteLlm
        model_str = _openrouter_model_name(model_name)
        if api_key:
            try:
                return LiteLlm(model=model_str, api_key=api_key)
            except TypeError:
                os.environ["OPENROUTER_API_KEY"] = api_key
                return LiteLlm(model=model_str)
        return LiteLlm(model=model_str)

    elif provider == "ollama":
        from google.adk.models import LiteLlm
        # Ollama is local, no API key needed
        return LiteLlm(model=f"ollama/{model_name or 'qwen3:14b'}", api_base="http://localhost:11434")

    raise ValueError(f"Unknown provider: {provider}")


# =============================================================================
# DETERMINISTIC DEMO DIAGNOSIS - No LLM, no tokens, no hallucination
# =============================================================================

# =============================================================================
# A2A-BASED AGENT ROUTING
# =============================================================================

# Import agent cards and router
from agent.agents.base import AgentCard
from agent.agents.runtime_agent import RUNTIME_AGENT_CARD, runtime_agent, RUNTIME_AGENT_PROMPT
from agent.agents.config_agent import CONFIG_AGENT_CARD, config_agent, CONFIG_AGENT_PROMPT
from agent.agents.network_agent import NETWORK_AGENT_CARD, network_agent, NETWORK_AGENT_PROMPT
from agent.agents.storage_agent import STORAGE_AGENT_CARD, storage_agent, STORAGE_AGENT_PROMPT
from agent.agents.rbac_agent import RBAC_AGENT_CARD, rbac_agent, RBAC_AGENT_PROMPT
from agent.agents.router import AgentCardRouter

# All agent cards for the router
# Order matters for heuristic priority: more specific agents first
ALL_AGENT_CARDS = [
    STORAGE_AGENT_CARD,   # Storage issues (PVC, volume) - specific
    RBAC_AGENT_CARD,      # Permission issues - specific
    NETWORK_AGENT_CARD,   # Network issues - specific
    CONFIG_AGENT_CARD,    # ArgoCD sync issues
    RUNTIME_AGENT_CARD,   # General runtime (fallback)
]

# Agent prompts by ID
AGENT_PROMPTS = {
    "runtime": RUNTIME_AGENT_PROMPT,
    "config": CONFIG_AGENT_PROMPT,
    "network": NETWORK_AGENT_PROMPT,
    "storage": STORAGE_AGENT_PROMPT,
    "rbac": RBAC_AGENT_PROMPT,
}

# Global router instance
_router = AgentCardRouter(ALL_AGENT_CARDS)


# =============================================================================
# AGENT BUILDER
# =============================================================================

def build_agent(
    agent_id: str,
    provider: str,
    api_key: str,
    model_name: str = ""
) -> tuple[LlmAgent, Runner]:
    """Build specialist agent based on routing decision."""

    model = _create_model(provider, api_key, model_name)

    # Get prompt for this agent
    prompt = AGENT_PROMPTS.get(agent_id, AGENT_PROMPTS["runtime"])

    # Get agent card to determine tools
    card = _router.get_agent_card(agent_id)

    from agent.tools.k8s_tools import (
        get_events_tool, get_argocd_app_tool, get_argocd_diff_tool,
        list_pods_tool, get_resource_tool, get_pod_logs_tool
    )

    AGENT_TOOLS = {
        "runtime": [get_resource_tool, get_pod_logs_tool, list_pods_tool, get_events_tool],
        "config": [get_argocd_app_tool, get_argocd_diff_tool, get_resource_tool, get_events_tool, get_pod_logs_tool],
        "network": [get_resource_tool, list_pods_tool, get_events_tool],
        "storage": [get_resource_tool, list_pods_tool, get_events_tool],
        "rbac": [get_resource_tool, list_pods_tool, get_events_tool],
    }

    # Local models (Ollama) have unreliable tool-calling support,
    # so we strip tools and rely on the comprehensive pre-loaded data in the prompt.
    if provider == "ollama":
        tools = []
        prompt = "/no_think\n" + prompt
        logger.info(f"Ollama provider: tools stripped, using context-only diagnosis")
    else:
        tools = list(AGENT_TOOLS.get(agent_id, [get_events_tool, list_pods_tool, get_resource_tool]))
        enable_rag = os.environ.get("ENABLE_RAG", "true").lower() == "true"
        if enable_rag:
            try:
                from agent.tools.rag_tools import rag_search_tool
                tools.append(rag_search_tool)
            except Exception as e:
                logger.warning(f"RAG tool not available: {e}")

    selected_model_name = model_name or (DEFAULT_GEMINI_MODEL if provider in ("gemini", "google") else "")
    generate_config = genai_types.GenerateContentConfig(
        temperature=_float_env("LLM_TEMPERATURE", 0.2),
        max_output_tokens=_configured_llm_output_tokens(),
    )
    if _should_set_gemini_thinking_config(provider, selected_model_name):
        generate_config.thinking_config = genai_types.ThinkingConfig(
            thinking_budget=_configured_gemini_thinking_budget(),
        )
    if _should_force_json_response(provider, tools):
        generate_config.response_mime_type = "application/json"

    agent = LlmAgent(
        name=f"{agent_id}_analyzer",
        model=model,
        instruction=prompt,
        tools=tools,
        output_key="diagnosis",
        generate_content_config=generate_config,
        before_model_callback=_before_model_callback,
        after_model_callback=_after_model_callback,
        on_model_error_callback=_on_model_error_callback,
        before_tool_callback=_before_tool_callback,
        after_tool_callback=_after_tool_callback,
    )

    runner = Runner(
        app_name="argocd-agent",
        agent=agent,
        session_service=InMemorySessionService(),
    )

    return agent, runner


# =============================================================================
# DIAGNOSIS RUNNER
# =============================================================================

async def run_diagnosis(
    provider: str,
    api_key: str,
    model_name: str,
    signals: dict,
) -> AsyncIterator[dict]:
    """Run diagnosis with A2A-based routing, caching, and log filtering."""

    # 1. Check semantic cache first
    cached = _diagnosis_cache.get(signals)
    if cached:
        prom_metrics.CACHE_HITS.inc()
        yield {"type": "cache_hit", "content": "Using cached diagnosis for similar issue"}
        yield {"type": "diagnosis", "result": cached}
        yield {"type": "done"}
        return

    prom_metrics.CACHE_MISSES.inc()

    # 2. Route to appropriate agent using A2A-based router
    yield {"type": "triage_start", "content": "Analyzing symptoms to select specialist agent..."}

    agent_id, triage_reason = await _router.route(signals, provider, api_key, model_name)

    # Get agent info for display
    card = _router.get_agent_card(agent_id)
    agent_name = card.name if card else agent_id

    yield {
        "type": "routing",
        "agent": agent_id,
        "agent_name": agent_name,
        "reason": triage_reason,
        "content": f"Selected: {agent_name} - {triage_reason}"
    }

    # 3. Build the specialized agent
    try:
        _, runner = build_agent(agent_id, provider, api_key, model_name)
    except Exception as e:
        yield {"type": "error", "error": str(e)}
        return

    max_calls = _configured_tool_call_limit()
    max_tool_response_chars = _configured_tool_response_chars()
    session = await runner.session_service.create_session(
        app_name="argocd-agent",
        user_id="user",
        state={
            "_provider": provider,
            "_max_tool_calls": max_calls,
            "_max_tool_response_chars": max_tool_response_chars,
        },
    )

    # 4. Prepare context with ALL pre-loaded information
    # QUOTA-SURGICAL: Include comprehensive signals to enable zero-shot diagnosis
    app_name = signals.get('appName', 'unknown')
    app_namespace = signals.get('appNamespace') or signals.get('argocdNamespace') or "unknown"
    destination_namespace = signals.get('destinationNamespace', 'default')
    health = signals.get('healthStatus', 'Unknown')
    sync = signals.get('syncStatus', 'Unknown')

    # Pre-filter warning events
    warnings = signals.get('warningEvents') or []
    filtered_warnings = _log_filter.filter_events(warnings) if warnings else "No events in initial signal"

    # Format pod statuses if pre-loaded
    pod_statuses = signals.get('podStatuses') or []
    pod_status_str = "Not pre-loaded"
    if pod_statuses:
        pod_lines = []
        for ps in pod_statuses[:10]:  # Limit to 10 pods
            line = f"  - {ps.get('name', 'unknown')}: {ps.get('phase', '?')} ({ps.get('ready', '?')}) restarts={ps.get('restarts', 0)}"
            if ps.get('stateReason'):
                line += f" state={ps.get('stateReason')}"
            if ps.get('exitCode') and ps.get('exitCode') != 0:
                line += f" exitCode={ps.get('exitCode')}"
            if ps.get('lastTerminatedReason'):
                line += f" lastTerminated={ps.get('lastTerminatedReason')}"
            pod_lines.append(line)
        pod_status_str = '\n'.join(pod_lines)

    # Format pre-loaded logs if available
    preloaded_logs = signals.get('preloadedLogs', {})
    logs_str = ""
    if preloaded_logs:
        log_pod = preloaded_logs.get('pod', 'unknown')
        log_content = preloaded_logs.get('logs', '')
        if log_content:
            # Filter the pre-loaded logs
            filtered_logs = _log_filter.filter_logs(log_content)
            logs_str = f"\n\nPRE-LOADED LOGS from {log_pod}:\n{filtered_logs}"

    # Include ArgoCD conditions if available
    conditions = signals.get('conditions') or []
    conditions_str = ""
    if conditions:
        conditions_str = f"\n\nArgoCD Conditions:\n" + '\n'.join(f"  - {c}" for c in conditions[:5])

    # Build comprehensive message for zero-shot diagnosis
    message = f"""Diagnose this ArgoCD application issue:

ArgoCD Application: {app_name}
ArgoCD App Namespace: {app_namespace}
Destination Namespace: {destination_namespace}
Health: {health}, Sync: {sync}

PRE-LOADED WARNING EVENTS:
{filtered_warnings}

PRE-LOADED POD STATUSES:
{pod_status_str}{conditions_str}{logs_str}

INSTRUCTIONS:
1. Analyze the pre-loaded data above to identify the root cause
2. ONLY call tools if you need additional details (manifests, more logs)
3. For ArgoCD tools (get_argocd_app/get_argocd_diff), use the ArgoCD App Namespace above.
4. For Kubernetes workload tools (events, pods, logs, resources), use the Destination Namespace above.
5. Tool budget for this run: {max_calls} calls. Do not spend tools on data already shown.
6. Output your diagnosis in JSON format"""

    yield {"type": "start", "content": f"Diagnosing with {provider} ({agent_name})..."}

    tool_calls = 0
    limit_reached = False
    streamed_text_chunks: list[str] = []
    diagnosis_timeout = _int_env("DIAGNOSIS_TIMEOUT_SECONDS", 90, minimum=5, maximum=300)

    try:
        async def _collect_events():
            nonlocal tool_calls, limit_reached
            async for event in runner.run_async(
                user_id="user",
                session_id=session.id,
                new_message=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=message)],
                ),
            ):
                if not hasattr(event, "content") or not event.content:
                    continue

                for part in (event.content.parts or []):
                    if hasattr(part, "function_call") and part.function_call:
                        tool_calls += 1
                        if tool_calls > max_calls + 2:
                            limit_reached = True
                            break
                        yield {"type": "tool_call", "tool": part.function_call.name}
                        if tool_calls == max_calls:
                            yield {"type": "warning", "content": f"Tool budget ({max_calls}) is now exhausted; later tool attempts will be blocked"}
                        elif tool_calls > max_calls:
                            yield {"type": "warning", "content": f"Blocked extra tool attempt {tool_calls - max_calls}; finalizing from available evidence"}

                    elif hasattr(part, "function_response") and part.function_response:
                        result = str(part.function_response.response)
                        yield {"type": "tool_result", "content": result[:500]}

                    elif hasattr(part, "text") and part.text:
                        text_chunk = part.text.strip()
                        if text_chunk:
                            streamed_text_chunks.append(text_chunk)
                            yield {"type": "reasoning", "content": text_chunk[:500]}

                if limit_reached:
                    yield {"type": "warning", "content": f"Tool call limit ({max_calls}) reached, finalizing diagnosis from available data"}
                    break

        timed_out = False
        async with asyncio.timeout(diagnosis_timeout):
            async for evt in _collect_events():
                yield evt

    except asyncio.TimeoutError:
        timed_out = True
        logger.warning(f"Diagnosis timed out after {diagnosis_timeout}s")
        yield {"type": "warning", "content": f"Diagnosis timed out after {diagnosis_timeout}s, returning best available result"}
    except Exception as e:
        user_error = _format_provider_error(e)
        if _is_known_provider_error(e):
            logger.warning("Diagnosis failed due to provider error: %s", user_error)
        else:
            logger.exception("Diagnosis failed")
        yield {"type": "error", "error": user_error}
        return

    # Extract final diagnosis
    final_session = await runner.session_service.get_session(
        app_name="argocd-agent", user_id="user", session_id=session.id
    )

    diagnosis = None
    if final_session and final_session.state:
        raw = final_session.state.get("diagnosis", "")
        if raw:
            diagnosis = _parse_diagnosis(raw)

    if not diagnosis and streamed_text_chunks:
        raw_streamed_text = "\n".join(streamed_text_chunks[-4:])
        if "{" in raw_streamed_text and "error" in raw_streamed_text.lower():
            diagnosis = _parse_diagnosis(raw_streamed_text)

    if final_session and final_session.state:
        usage = _build_usage_event(final_session.state)
        if usage:
            yield usage

    # Fallback to warning events
    if not diagnosis and warnings:
        w = warnings[0]
        diagnosis = {
            "error": w.get('message', 'Unknown error')[:200],
            "cause": w.get('reason', 'Unknown'),
            "fix": "Check pod events and logs for details"
        }

    if diagnosis:
        _diagnosis_cache.set(signals, diagnosis)
        yield {"type": "diagnosis", "result": diagnosis}
    else:
        yield {"type": "error", "error": "Could not determine root cause"}

    yield {"type": "done"}


def _parse_diagnosis(text: str) -> dict:
    """Parse diagnosis JSON from agent output.
    
    Handles multiple formats LLMs might return:
    - Clean JSON: {"error": "...", "cause": "...", "fix": "..."}
    - Markdown wrapped: ```json {...} ```
    - Nested/stringified: "The diagnosis is: {"error": ...}"
    - Prose with embedded JSON
    """
    if not text:
        return {"error": "No diagnosis generated", "cause": "Agent did not produce output", "fix": "Check agent logs"}
    
    clean = text.strip()
    
    # Strategy 1: Extract from markdown code blocks
    if "```" in clean:
        parts = clean.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                try:
                    return json.loads(p)
                except json.JSONDecodeError:
                    pass
    
    # Strategy 2: Find JSON object anywhere in the text
    # Look for {"error": pattern which is our expected format
    json_patterns = [
        r'\{[^{}]*"error"[^{}]*"cause"[^{}]*"fix"[^{}]*\}',  # All three fields
        r'\{[^{}]*"error"[^{}]*\}',  # At least error field
    ]
    
    for pattern in json_patterns:
        matches = re.findall(pattern, clean, re.DOTALL)
        for match in matches:
            try:
                parsed = json.loads(match)
                if "error" in parsed:
                    # Ensure all fields exist
                    return {
                        "error": parsed.get("error", "Unknown error"),
                        "cause": parsed.get("cause", "See error details"),
                        "fix": parsed.get("fix", "Review the error and logs"),
                    }
            except json.JSONDecodeError:
                pass
    
    # Strategy 3: Try to parse the whole thing as JSON
    try:
        parsed = json.loads(clean)
        if isinstance(parsed, dict):
            return {
                "error": parsed.get("error", str(parsed)[:200]),
                "cause": parsed.get("cause", "See error details"),
                "fix": parsed.get("fix", "Review the analysis"),
            }
    except json.JSONDecodeError:
        pass
    
    # Strategy 4: Extract key information from prose
    # Look for patterns like "error: ...", "cause: ...", "fix: ..."
    error_match = re.search(r'error["\s:]+([^,}\n]+)', clean, re.IGNORECASE)
    cause_match = re.search(r'cause["\s:]+([^,}\n]+)', clean, re.IGNORECASE)
    fix_match = re.search(r'fix["\s:]+([^,}\n]+)', clean, re.IGNORECASE)
    
    if error_match:
        return {
            "error": error_match.group(1).strip().strip('"\''),
            "cause": cause_match.group(1).strip().strip('"\'') if cause_match else "See error details",
            "fix": fix_match.group(1).strip().strip('"\'') if fix_match else "Review the analysis",
        }
    
    # Fallback: Return the text as-is but properly formatted
    return {
        "error": clean[:200] if len(clean) > 200 else clean,
        "cause": "Agent provided unstructured response",
        "fix": "Review the analysis above for actionable steps"
    }


# =============================================================================
# PUBLIC API
# =============================================================================

def clear_diagnosis_cache():
    """Clear the semantic cache."""
    global _diagnosis_cache
    _diagnosis_cache = SemanticCache(ttl_seconds=900)
    logger.info("Diagnosis cache cleared")


def get_cache_stats() -> dict:
    """Get cache statistics."""
    return {
        "entries": len(_diagnosis_cache._cache),
        "max_entries": _diagnosis_cache.max_entries,
        "ttl_seconds": _diagnosis_cache.ttl,
    }


def list_available_agents() -> list[dict]:
    """List all available specialist agents."""
    return _router.list_agents()
