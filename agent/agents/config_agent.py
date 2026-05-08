# Config Analyzer Agent
#
# Specializes in ArgoCD sync and configuration issues:
# - OutOfSync, SyncError, ComparisonError
# - Git/Helm/Kustomize problems
# - Manifest validation errors

from agent.agents.base import AgentCard, AgentSkill

# Agent Card (A2A metadata)
CONFIG_AGENT_CARD = AgentCard(
    id="config",
    name="Config Analyzer",
    description="Diagnoses ArgoCD application sync and configuration issues including OutOfSync states, Git problems, Helm/Kustomize errors, and manifest validation failures.",
    version="1.0.0",
    skills=[
        AgentSkill(
            id="config-sync",
            name="Sync Status Analysis",
            description="Analyzes ArgoCD application sync status and identifies drift between Git and cluster state",
            tags=["argocd", "sync", "gitops"],
            examples=[
                "OutOfSync - cluster state differs from Git",
                "SyncError - failed to apply manifests",
                "ComparisonError - cannot compare resources",
            ]
        ),
        AgentSkill(
            id="config-manifest",
            name="Manifest Validation",
            description="Validates Kubernetes manifests and Helm/Kustomize configurations",
            tags=["manifests", "helm", "kustomize", "yaml"],
            examples=[
                "InvalidSpecError - malformed YAML",
                "Helm render error - values file issue",
                "Kustomize build error - missing base",
            ]
        ),
    ],
    # Fast heuristic matching
    trigger_event_reasons=[
        "SyncError", "ComparisonError", "InvalidSpecError",
        "OperationError", "ResourceNotFound", "HookError",
    ],
    trigger_keywords=[
        "sync", "git", "helm", "kustomize", "manifest",
        "render", "template", "values", "chart", "base",
        "overlay", "patch", "drift", "desired state",
    ],
    health_conditions=[],
    sync_conditions=["OutOfSync", "Unknown"],
)

# Agent prompt - QUOTA-SURGICAL: Context-First, Zero-Shot approach
CONFIG_AGENT_PROMPT = """You are an ArgoCD Config Analyzer. You diagnose sync and configuration issues using pre-loaded cluster data.

AVAILABLE TOOLS (use ONLY these, do NOT invent tools):
- get_argocd_app(app_name, namespace): Get ArgoCD Application health/sync status
- get_events(namespace): Get warning events from a namespace
- get_resource(kind, name, namespace): Get resource manifest summary
- get_pod_logs(namespace, pod, container, previous): Get filtered pod logs
- rag_search(error_type): Search knowledge base for troubleshooting steps

CONTEXT-FIRST RULE:
The user message contains PRE-LOADED data (healthStatus, syncStatus, conditions, warningEvents, resources).
ANALYZE THIS DATA FIRST. Do NOT call tools to re-fetch data already provided.

TOOL BUDGET: You have a maximum of 3 tool calls. Use them only when pre-loaded data is insufficient.
Do NOT call get_argocd_app if health/sync/conditions are already in the prompt.
Do NOT call get_events if warningEvents are already in the prompt.

DIAGNOSTIC DECISION TREE:

OutOfSync with no error conditions:
- Likely a manual cluster change or Git webhook issue.
- Check if auto-sync is enabled (autoSyncEnabled in signals).
- Fix: Review the resource diff and either update Git or revert the cluster change.

SyncError:
- Check ArgoCD conditions for the specific error message.
- Common causes: invalid YAML, Helm values error, resource conflict, namespace doesn't exist.
- Fix: Correct the manifest in Git based on the condition message.

ComparisonError:
- Resource comparison failed, often due to CRD schema issues.
- Fix: Check if CRDs are installed and up to date.

Degraded but Synced:
- App deployed successfully but pods are unhealthy.
- This is actually a runtime issue -- note this in your diagnosis.

CreateContainerConfigError (with sync OK):
- Missing ConfigMap or Secret referenced in the deployment.
- Extract the exact resource name from the event message.

ANTI-HALLUCINATION RULES:
- ONLY report findings that are directly supported by the data provided or tool results.
- If you cannot determine the root cause, say "Unable to determine root cause from available signals" in the cause field.
- Do NOT invent error messages, resource names, or Git details.
- Include the actual evidence (condition message, sync status) in your diagnosis.

OUTPUT FORMAT (strict JSON, no markdown, no code fences, no explanation outside the JSON):
{"error": "<exact error from conditions/events>", "cause": "<specific root cause with evidence>", "fix": "<actionable fix steps>"}"""

# Tools this agent uses
CONFIG_AGENT_TOOLS = ["get_argocd_app", "get_events", "get_resource"]


def config_agent():
    """Factory function to create the Config Analyzer agent."""
    from google.adk.agents.llm_agent import LlmAgent
    from agent.tools.k8s_tools import get_argocd_app_tool, get_events_tool, get_resource_tool
    
    tools = [get_argocd_app_tool, get_events_tool, get_resource_tool]
    
    # Add RAG if available
    try:
        from agent.tools.rag_tools import rag_search_tool
        tools.append(rag_search_tool)
    except Exception:
        pass
    
    return LlmAgent(
        name="config_analyzer",
        instruction=CONFIG_AGENT_PROMPT,
        tools=tools,
        output_key="diagnosis",
    )
