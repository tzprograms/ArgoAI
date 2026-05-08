# RBAC Analyzer Agent
#
# Specializes in Kubernetes RBAC and permission issues:
# - ServiceAccount problems, missing roles
# - Forbidden errors, authorization failures
# - Impersonation issues

from agent.agents.base import AgentCard, AgentSkill

# Agent Card (A2A metadata)
RBAC_AGENT_CARD = AgentCard(
    id="rbac",
    name="RBAC Analyzer",
    description="Diagnoses Kubernetes RBAC and permission issues including ServiceAccount problems, Role/RoleBinding misconfigurations, forbidden errors, and authorization failures.",
    version="1.0.0",
    skills=[
        AgentSkill(
            id="rbac-permissions",
            name="Permission Analysis",
            description="Analyzes RBAC permissions and identifies missing roles or bindings",
            tags=["rbac", "permissions", "authorization"],
            examples=[
                "Forbidden - user lacks required permission",
                "cannot get secrets - ServiceAccount missing role",
                "Unauthorized - invalid credentials or token",
            ]
        ),
        AgentSkill(
            id="rbac-serviceaccount",
            name="ServiceAccount Analysis",
            description="Diagnoses ServiceAccount configuration and token issues",
            tags=["serviceaccount", "tokens"],
            examples=[
                "ServiceAccount not found - SA doesn't exist",
                "Token expired - need to refresh",
                "Impersonation denied - missing impersonate verb",
            ]
        ),
    ],
    # Fast heuristic matching
    trigger_event_reasons=[
        "Forbidden", "Unauthorized", "FailedCreate",
    ],
    trigger_keywords=[
        "forbidden", "unauthorized", "rbac", "permission",
        "serviceaccount", "cannot get", "cannot list",
        "cannot create", "cannot delete", "cannot watch",
        "access denied", "not allowed", "impersonate",
        "role", "rolebinding", "clusterrole",
    ],
    health_conditions=[],
    sync_conditions=[],
)

# Agent prompt - QUOTA-SURGICAL: Context-First, Zero-Shot approach
RBAC_AGENT_PROMPT = """You are a Kubernetes RBAC Analyzer. You diagnose permission and authorization issues using pre-loaded cluster data.

AVAILABLE TOOLS (use ONLY these, do NOT invent tools):
- get_events(namespace): Get warning events from a namespace
- get_resource(kind, name, namespace): Get resource manifest (ServiceAccount, Role, RoleBinding, ClusterRole)
- list_pods(namespace): List pods with status
- rag_search(error_type): Search knowledge base for troubleshooting steps

CONTEXT-FIRST RULE:
The user message contains PRE-LOADED data (warningEvents, conditions, podStatuses).
ANALYZE THIS DATA FIRST. Do NOT call tools to re-fetch data already provided.

TOOL BUDGET: You have a maximum of 3 tool calls.

DIAGNOSTIC DECISION TREE:

Forbidden / "cannot get|list|create|delete":
- Extract the denied verb, resource, and ServiceAccount from the error message.
- The error usually says "User system:serviceaccount:NAMESPACE:SA_NAME cannot VERB RESOURCE".
- Fix: Create a Role with the required verb/resource and bind it to the ServiceAccount.

FailedCreate with Forbidden:
- A controller (Deployment, ReplicaSet) lacks permission to create pods or other resources.
- Check which ServiceAccount the Deployment uses.

ArgoCD sync Forbidden:
- ArgoCD's application-controller ServiceAccount lacks permission in the target namespace.
- Check ArgoCD conditions for the specific permission error.

ServiceAccount not found:
- The Deployment references a non-existent ServiceAccount.
- Call get_resource(kind="ServiceAccount", ...) to verify.

ANTI-HALLUCINATION RULES:
- ONLY report findings supported by actual data or tool results.
- If you cannot determine the root cause, say so explicitly.
- Do NOT invent ServiceAccount names, Role names, or permission details.

OUTPUT FORMAT (strict JSON, no markdown, no code fences, no explanation outside the JSON):
{"error": "<exact error from events/conditions>", "cause": "<specific root cause with evidence>", "fix": "<actionable fix steps>"}"""

# Tools this agent uses
RBAC_AGENT_TOOLS = ["get_events", "get_argocd_app", "get_resource"]


def rbac_agent():
    """Factory function to create the RBAC Analyzer agent."""
    from google.adk.agents.llm_agent import LlmAgent
    from agent.tools.k8s_tools import get_events_tool, get_argocd_app_tool, get_resource_tool
    
    tools = [get_events_tool, get_argocd_app_tool, get_resource_tool]
    
    # Add RAG if available
    try:
        from agent.tools.rag_tools import rag_search_tool
        tools.append(rag_search_tool)
    except Exception:
        pass
    
    return LlmAgent(
        name="rbac_analyzer",
        instruction=RBAC_AGENT_PROMPT,
        tools=tools,
        output_key="diagnosis",
    )
