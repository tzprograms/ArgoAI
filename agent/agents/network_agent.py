# Network Analyzer Agent
#
# Specializes in Kubernetes networking issues:
# - Service connectivity, DNS resolution
# - Ingress/Route problems, TLS/certificate errors
# - Network policies, health probe failures

from agent.agents.base import AgentCard, AgentSkill

# Agent Card (A2A metadata)
NETWORK_AGENT_CARD = AgentCard(
    id="network",
    name="Network Analyzer",
    description="Diagnoses Kubernetes networking issues including service connectivity, DNS resolution, ingress problems, TLS/certificate errors, and network policy blocks.",
    version="1.0.0",
    skills=[
        AgentSkill(
            id="network-connectivity",
            name="Service Connectivity Analysis",
            description="Analyzes service-to-service connectivity, DNS resolution, and network reachability",
            tags=["networking", "services", "dns"],
            examples=[
                "Connection refused - service not listening or selector mismatch",
                "DNS lookup failed - service doesn't exist or CoreDNS issue",
                "No route to host - network policy or node issue",
            ]
        ),
        AgentSkill(
            id="network-ingress",
            name="Ingress/TLS Analysis",
            description="Diagnoses ingress controller issues and TLS certificate problems",
            tags=["ingress", "tls", "certificates"],
            examples=[
                "503 Service Unavailable - backend not ready",
                "TLS handshake error - certificate mismatch",
                "SSL certificate expired",
            ]
        ),
        AgentSkill(
            id="network-probes",
            name="Health Probe Analysis",
            description="Analyzes liveness and readiness probe failures",
            tags=["probes", "health"],
            examples=[
                "Readiness probe failed - app not ready",
                "Liveness probe failed - app unresponsive",
            ]
        ),
    ],
    # Fast heuristic matching
    trigger_event_reasons=[
        "Unhealthy",  # Probe failures
    ],
    trigger_keywords=[
        "dns", "connection refused", "connection timed out",
        "tls", "certificate", "ssl", "ingress", "route",
        "no route to host", "network unreachable", "dial tcp",
        "probe failed", "service unavailable", "503", "502",
        "network policy", "endpoint", "port",
    ],
    health_conditions=[],
    sync_conditions=[],
)

# Agent prompt - QUOTA-SURGICAL: Context-First, Zero-Shot approach
NETWORK_AGENT_PROMPT = """You are a Kubernetes Network Analyzer. You diagnose connectivity, DNS, TLS, and probe issues using pre-loaded cluster data.

AVAILABLE TOOLS (use ONLY these, do NOT invent tools):
- get_events(namespace): Get warning events from a namespace
- get_resource(kind, name, namespace): Get resource manifest (Service, Ingress, NetworkPolicy)
- list_pods(namespace): List pods with status
- rag_search(error_type): Search knowledge base for troubleshooting steps

CONTEXT-FIRST RULE:
The user message contains PRE-LOADED data (warningEvents, podStatuses, preloadedLogs).
ANALYZE THIS DATA FIRST. Do NOT call tools to re-fetch data already provided.

TOOL BUDGET: You have a maximum of 3 tool calls.

DIAGNOSTIC DECISION TREE:

Unhealthy (probe failure):
- Readiness/liveness probe failed. Check if the container port matches the probe config.
- Common causes: app not listening on expected port, slow startup, wrong probe path.
- Call get_resource(kind="Service", ...) to check selector/port alignment if needed.

Connection refused:
- Service selector mismatch (no matching pods) or pod not listening on target port.
- Call get_resource(kind="Service", ...) to verify selector matches pod labels.

DNS lookup failed:
- Service doesn't exist in the namespace, or CoreDNS is down.
- Check the service name in the error message.

TLS/certificate errors:
- Certificate mismatch, expiration, or chain issue.
- Call get_resource(kind="Ingress", ...) to check TLS config.

503 from Ingress:
- Backend pods not ready. Check podStatuses for readiness.

ANTI-HALLUCINATION RULES:
- ONLY report findings supported by actual data or tool results.
- If you cannot determine the root cause, say so explicitly.
- Do NOT invent IP addresses, port numbers, or service names.

OUTPUT FORMAT (strict JSON, no markdown, no code fences, no explanation outside the JSON):
{"error": "<exact error from events/logs>", "cause": "<specific root cause with evidence>", "fix": "<actionable fix steps>"}"""

# Tools this agent uses
NETWORK_AGENT_TOOLS = ["get_events", "get_resource", "list_pods"]


def network_agent():
    """Factory function to create the Network Analyzer agent."""
    from google.adk.agents.llm_agent import LlmAgent
    from agent.tools.k8s_tools import get_events_tool, get_resource_tool, list_pods_tool
    
    tools = [get_events_tool, get_resource_tool, list_pods_tool]
    
    # Add RAG if available
    try:
        from agent.tools.rag_tools import rag_search_tool
        tools.append(rag_search_tool)
    except Exception:
        pass
    
    return LlmAgent(
        name="network_analyzer",
        instruction=NETWORK_AGENT_PROMPT,
        tools=tools,
        output_key="diagnosis",
    )
