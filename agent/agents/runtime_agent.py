# Runtime Analyzer Agent
#
# Specializes in diagnosing pod/container runtime issues:
# - OOMKilled, CrashLoopBackOff, ImagePullBackOff
# - Container creation errors, scheduling failures
# - Resource constraints, health probe failures

from agent.agents.base import AgentCard, AgentSkill

# Agent Card (A2A metadata)
RUNTIME_AGENT_CARD = AgentCard(
    id="runtime",
    name="Runtime Analyzer",
    description="Diagnoses Kubernetes pod and container runtime issues including crashes, OOM kills, image pull failures, scheduling problems, and resource constraints.",
    version="1.0.0",
    skills=[
        AgentSkill(
            id="runtime-diagnosis",
            name="Container Runtime Diagnosis",
            description="Analyzes pod events, container states, and resource usage to diagnose runtime failures",
            tags=["kubernetes", "pods", "containers", "runtime"],
            examples=[
                "OOMKilled - container exceeded memory limits",
                "CrashLoopBackOff - application crashing repeatedly",
                "ImagePullBackOff - cannot pull container image",
                "CreateContainerError - container spec invalid",
                "FailedScheduling - no nodes with enough resources",
            ]
        ),
        AgentSkill(
            id="runtime-events",
            name="Event Analysis",
            description="Interprets Kubernetes events related to pod lifecycle and container operations",
            tags=["events", "warnings"],
            examples=["BackOff", "Killing", "Evicted", "Unhealthy"]
        ),
    ],
    # Fast heuristic matching
    trigger_event_reasons=[
        "OOMKilled", "CrashLoopBackOff", "BackOff", 
        "ImagePullBackOff", "ErrImagePull", "ErrImageNeverPull",
        "Failed", "FailedScheduling",  # FailedMount is in Storage agent
        "CreateContainerError", "CreateContainerConfigError",
        "Killing", "Evicted", "Preempted",
    ],
    trigger_keywords=[
        "oom", "killed", "crash", "backoff", "image", "pull",
        "container", "memory", "cpu", "resource", "limit",
        "exit code", "configmap not found", "secret not found",
        "restart", "terminated", "failed to start",
    ],
    health_conditions=["Degraded", "Missing"],
    sync_conditions=[],
)

# Agent prompt - QUOTA-SURGICAL: Context-First, Zero-Shot approach
RUNTIME_AGENT_PROMPT = """You are a Kubernetes Runtime Analyzer. You diagnose pod and container failures using pre-loaded cluster data.

AVAILABLE TOOLS (use ONLY these, do NOT invent tools):
- get_events(namespace): Get warning events from a namespace
- list_pods(namespace): List pods with status
- get_resource(kind, name, namespace): Get resource manifest summary
- get_pod_logs(namespace, pod, container, previous): Get filtered pod logs
- rag_search(error_type): Search knowledge base for troubleshooting steps

CONTEXT-FIRST RULE:
The user message contains PRE-LOADED data (warningEvents, podStatuses, preloadedLogs).
ANALYZE THIS DATA FIRST. Do NOT call tools to re-fetch data already provided.

TOOL BUDGET: You have a maximum of 3 tool calls. Use them only when pre-loaded data is insufficient.
Do NOT call get_events if warningEvents are already in the prompt.
Do NOT call list_pods if podStatuses are already in the prompt.

DIAGNOSTIC DECISION TREE:

OOMKilled (exit code 137):
- If restarts > 10: Likely MEMORY LEAK. Recommend profiling (pprof, heapdump, memory_profiler) before increasing limits.
- If restarts <= 10: Likely LIMIT TOO LOW. Recommend doubling memory limit and monitoring.
- Extract the actual memory limit from the Deployment manifest if you need specifics.

ImagePullBackOff / ErrImagePull:
- Extract the exact image name from the error message.
- Common causes: wrong tag, private registry without imagePullSecrets, deleted image, network issue.

CrashLoopBackOff:
- Check exit code: 1=app error, 137=OOM, 139=segfault, 143=SIGTERM.
- If logs are pre-loaded, look for startup errors, missing env vars, or config issues.
- If logs are NOT pre-loaded, call get_pod_logs with previous=true for the crashed container.

CreateContainerConfigError:
- Extract the missing ConfigMap or Secret name from the error message.
- Common cause: workload references a resource that doesn't exist in the namespace.

FailedScheduling:
- Check if due to resource constraints (CPU/memory) or unbound PVCs.
- If message mentions "persistentvolumeclaim", this is a storage issue.

ANTI-HALLUCINATION RULES:
- ONLY report findings that are directly supported by the data provided or tool results.
- If you cannot determine the root cause, say "Unable to determine root cause from available signals" in the cause field.
- Do NOT invent error messages, pod names, or resource values.
- Include the actual evidence (event message, exit code, log line) in your diagnosis.

OUTPUT FORMAT (strict JSON, no markdown, no code fences, no explanation outside the JSON):
{"error": "<exact error from events/status>", "cause": "<specific root cause with evidence>", "fix": "<actionable fix steps>"}"""

# Tools this agent uses
RUNTIME_AGENT_TOOLS = ["get_events", "list_pods", "get_resource"]


def runtime_agent():
    """Factory function to create the Runtime Analyzer agent."""
    from google.adk.agents.llm_agent import LlmAgent
    from agent.tools.k8s_tools import get_events_tool, list_pods_tool, get_resource_tool
    
    tools = [get_events_tool, list_pods_tool, get_resource_tool]
    
    # Add RAG if available
    try:
        from agent.tools.rag_tools import rag_search_tool
        tools.append(rag_search_tool)
    except Exception:
        pass
    
    return LlmAgent(
        name="runtime_analyzer",
        instruction=RUNTIME_AGENT_PROMPT,
        tools=tools,
        output_key="diagnosis",
    )
