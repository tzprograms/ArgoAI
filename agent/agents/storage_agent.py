# Storage Analyzer Agent
#
# Specializes in Kubernetes storage issues:
# - PVC binding, StorageClass problems
# - Volume mount failures, permission errors
# - CSI driver issues

from agent.agents.base import AgentCard, AgentSkill

# Agent Card (A2A metadata)
STORAGE_AGENT_CARD = AgentCard(
    id="storage",
    name="Storage Analyzer",
    description="Diagnoses Kubernetes storage and volume issues including PVC binding, mount failures, storage class problems, and permission errors on volumes.",
    version="1.0.0",
    skills=[
        AgentSkill(
            id="storage-pvc",
            name="PVC Analysis",
            description="Analyzes PersistentVolumeClaim binding issues and storage provisioning",
            tags=["storage", "pvc", "pv"],
            examples=[
                "PVC Pending - no matching PV or StorageClass issue",
                "ProvisioningFailed - storage class not found",
                "WaitForFirstConsumer - waiting for pod to schedule",
            ]
        ),
        AgentSkill(
            id="storage-mount",
            name="Volume Mount Analysis",
            description="Diagnoses volume mount failures and attachment issues",
            tags=["volumes", "mount"],
            examples=[
                "FailedMount - volume doesn't exist or node can't access",
                "FailedAttachVolume - volume already attached elsewhere",
                "Permission denied - fsGroup or securityContext issue",
            ]
        ),
    ],
    # Fast heuristic matching - ProvisioningFailed is a STORAGE issue
    trigger_event_reasons=[
        "ProvisioningFailed", "FailedMount", "FailedAttachVolume",
        "VolumeNotFound", "FailedBinding", "FailedScheduling",  # When due to PVC
    ],
    trigger_keywords=[
        "mount", "volume", "pvc", "pv ", "storage",
        "persistentvolume", "unbound", "attach",
        "storageclass", "provisioner", "csi",
        "fsgroup", "readonly", "permission denied",
    ],
    health_conditions=[],
    sync_conditions=[],
)

# Agent prompt - QUOTA-SURGICAL: Context-First, Zero-Shot approach
STORAGE_AGENT_PROMPT = """You are a Kubernetes Storage Analyzer. You diagnose PVC, volume mount, and storage class issues using pre-loaded cluster data.

AVAILABLE TOOLS (use ONLY these, do NOT invent tools):
- get_events(namespace): Get warning events from a namespace
- get_resource(kind, name, namespace): Get resource manifest (PVC, PV, StorageClass, Deployment)
- list_pods(namespace): List pods with status
- rag_search(error_type): Search knowledge base for troubleshooting steps

CONTEXT-FIRST RULE:
The user message contains PRE-LOADED data (warningEvents, podStatuses).
ANALYZE THIS DATA FIRST. Do NOT call tools to re-fetch data already provided.

TOOL BUDGET: You have a maximum of 3 tool calls.

DIAGNOSTIC DECISION TREE:

ProvisioningFailed:
- StorageClass not found or provisioner error.
- Extract the StorageClass name from the error message.
- Call get_resource(kind="PersistentVolumeClaim", ...) to check the PVC spec if needed.

FailedMount:
- Volume doesn't exist, node can't access it, or wrong mount path.
- Extract the PVC/volume name from the event message.

FailedAttachVolume:
- Volume already attached to another node (RWO access mode conflict).
- Check if another pod on a different node holds the volume.

FailedScheduling with PVC:
- PVC not bound. Check if StorageClass exists and has available capacity.
- If message mentions "persistentvolumeclaim", this is the root cause.

Permission denied on volume:
- fsGroup or securityContext misconfigured.
- Check Deployment spec for security context settings.

ANTI-HALLUCINATION RULES:
- ONLY report findings supported by actual data or tool results.
- If you cannot determine the root cause, say so explicitly.
- Do NOT invent PVC names, StorageClass names, or capacity values.

OUTPUT FORMAT (strict JSON, no markdown, no code fences, no explanation outside the JSON):
{"error": "<exact error from events>", "cause": "<specific root cause with evidence>", "fix": "<actionable fix steps>"}"""

# Tools this agent uses
STORAGE_AGENT_TOOLS = ["get_events", "get_resource", "list_pods"]


def storage_agent():
    """Factory function to create the Storage Analyzer agent."""
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
        name="storage_analyzer",
        instruction=STORAGE_AGENT_PROMPT,
        tools=tools,
        output_key="diagnosis",
    )
