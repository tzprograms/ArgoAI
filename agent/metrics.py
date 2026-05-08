"""Prometheus metrics for the Python agent service."""

from prometheus_client import Counter, Histogram, Gauge, Info

# Service info
SERVICE_INFO = Info(
    "argocd_agent_python_service",
    "Information about the Python agent service"
)

# Diagnosis metrics
DIAGNOSIS_REQUESTS = Counter(
    "argocd_agent_diagnosis_requests_total",
    "Total number of diagnosis requests",
    ["provider", "status"]
)

DIAGNOSIS_DURATION = Histogram(
    "argocd_agent_diagnosis_duration_seconds",
    "Diagnosis request duration in seconds",
    ["provider"],
    buckets=[1, 5, 10, 30, 60, 120, 300]
)

ACTIVE_DIAGNOSES = Gauge(
    "argocd_agent_active_diagnoses",
    "Number of active diagnosis sessions"
)

# Agent pipeline metrics
AGENT_STEPS = Counter(
    "argocd_agent_pipeline_steps_total",
    "Total number of agent pipeline steps",
    ["agent", "step_type"]
)

TOOL_CALLS = Counter(
    "argocd_agent_tool_calls_total",
    "Total number of tool calls",
    ["tool", "status"]
)

TOOL_CALL_DURATION = Histogram(
    "argocd_agent_tool_call_duration_seconds",
    "Tool call duration in seconds",
    ["tool"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10]
)

# RAG metrics
RAG_SEARCHES = Counter(
    "argocd_agent_rag_searches_total",
    "Total number of RAG searches",
    ["status"]
)

RAG_SEARCH_DURATION = Histogram(
    "argocd_agent_rag_search_duration_seconds",
    "RAG search duration in seconds",
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2]
)

RAG_RESULTS_COUNT = Histogram(
    "argocd_agent_rag_results_count",
    "Number of RAG results returned",
    buckets=[0, 1, 2, 3, 5, 10]
)

RAG_INDEX_SIZE = Gauge(
    "argocd_agent_rag_index_size",
    "Number of vectors in the RAG index"
)

RAG_LOADED = Gauge(
    "argocd_agent_rag_loaded",
    "Whether the RAG index is loaded (1 = loaded, 0 = not loaded)"
)

# Semantic cache metrics
CACHE_HITS = Counter(
    "argocd_agent_cache_hits_total",
    "Total number of semantic cache hits"
)

CACHE_MISSES = Counter(
    "argocd_agent_cache_misses_total",
    "Total number of semantic cache misses"
)

# Intelligent triage metrics
TRIAGE_DECISIONS = Counter(
    "argocd_agent_triage_decisions_total",
    "Total number of triage routing decisions",
    ["agent", "method"]  # method: intelligent (LLM), heuristic (fast), fallback (LLM failed), cached
)

TRIAGE_DURATION = Histogram(
    "argocd_agent_triage_duration_seconds",
    "Triage decision duration in seconds",
    ["method"],
    buckets=[0.1, 0.5, 1, 2, 5, 10]
)

# LLM metrics
LLM_REQUESTS = Counter(
    "argocd_agent_llm_requests_total",
    "Total number of LLM API requests",
    ["provider", "status"]
)

LLM_TOKENS_USED = Counter(
    "argocd_agent_llm_tokens_total",
    "Total number of LLM tokens used (estimated)",
    ["provider", "type"]
)

# Go service connection metrics
GO_SERVICE_CALLS = Counter(
    "argocd_agent_go_service_calls_total",
    "Total number of calls to the Go service",
    ["endpoint", "status"]
)

GO_SERVICE_CALL_DURATION = Histogram(
    "argocd_agent_go_service_call_duration_seconds",
    "Go service call duration in seconds",
    ["endpoint"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 2, 5]
)


def init_service_info(version: str = "0.1.0"):
    """Initialize service info metric."""
    SERVICE_INFO.info({
        "version": version,
        "framework": "google-adk",
    })
