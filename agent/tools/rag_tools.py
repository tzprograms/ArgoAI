"""RAG tool - Search knowledge base for troubleshooting guidance.

Design: RAG provides remediation steps from KCS articles, CEE docs, and FAQs.
"""

import time
from google.adk.tools import FunctionTool
from agent.rag.retriever import retriever_instance
from agent import metrics as prom_metrics


QUERY_MAPPINGS = {
    "oomkilled": "container memory limit exceeded OOMKilled exit code 137 increase resources",
    "oom": "container memory limit exceeded OOMKilled exit code 137 increase resources",
    "imagepullbackoff": "image pull failed ImagePullBackOff registry authentication tag",
    "errimagepull": "image pull failed ImagePullBackOff registry authentication tag",
    "crashloopbackoff": "container crash restart CrashLoopBackOff application error startup",
    "createcontainerconfigerror": "container config error ConfigMap Secret missing reference",
    "failedscheduling": "pod scheduling failed node resources taints tolerations",
    "failedmount": "volume mount failed PVC PersistentVolumeClaim storage",
    "forbidden": "RBAC permission denied ServiceAccount Role RoleBinding",
    "unauthorized": "authentication failed ServiceAccount token credentials",
}


EXPECTED_TERMS = {
    "oomkilled": ("oomkilled", "exit code 137", "memory", "out of memory", "memory limit"),
    "oom": ("oomkilled", "exit code 137", "memory", "out of memory", "memory limit"),
    "imagepullbackoff": ("imagepullbackoff", "image pull", "pull image", "registry", "tag"),
    "errimagepull": ("errimagepull", "image pull", "pull image", "registry", "tag"),
    "crashloopbackoff": ("crashloopbackoff", "back-off", "restart", "failed container", "exit code"),
    "createcontainerconfigerror": ("createcontainerconfigerror", "configmap", "secret", "not found"),
    "failedscheduling": ("failedscheduling", "insufficient", "taint", "toleration", "node"),
    "failedmount": ("failedmount", "mount", "persistentvolumeclaim", "pvc", "volume"),
    "forbidden": ("forbidden", "rbac", "permission", "rolebinding", "serviceaccount"),
    "unauthorized": ("unauthorized", "authentication", "token", "credentials"),
}


INLINE_HELP = {
    "oomkilled": "OOMKilled (exit code 137): Container exceeded memory limit. If restarts > 10, likely a memory leak -- profile with pprof/heapdump. If restarts are low, increase memory limits in deployment manifest.",
    "oom": "OOMKilled (exit code 137): Container exceeded memory limit. If restarts > 10, likely a memory leak -- profile with pprof/heapdump. If restarts are low, increase memory limits in deployment manifest.",
    "imagepullbackoff": "ImagePullBackOff: Verify image repository/tag exists, check registry auth (imagePullSecrets), confirm network connectivity to registry.",
    "errimagepull": "ErrImagePull: Image cannot be pulled. Check exact image name/tag, registry credentials, and network access.",
    "crashloopbackoff": "CrashLoopBackOff: Container starts but exits repeatedly. Check exit code (1=app error, 137=OOM, 139=segfault). Inspect logs for startup errors, missing env vars, or config.",
    "createcontainerconfigerror": "CreateContainerConfigError: Container spec references a missing ConfigMap or Secret. Verify the referenced resource exists in the namespace.",
    "failedmount": "FailedMount: Volume cannot be mounted. Check PVC exists and is bound, verify StorageClass, check node access to storage backend.",
    "provisioningfailed": "ProvisioningFailed: StorageClass provisioner failed. Verify StorageClass exists, check provisioner health, confirm storage backend capacity.",
    "failedscheduling": "FailedScheduling: No node matches pod requirements. Check resource requests/limits, node taints/tolerations, PVC binding, and affinity rules.",
    "forbidden": "Forbidden: RBAC permission denied. Identify the denied verb/resource from the error, add the minimal Role/RoleBinding for the ServiceAccount.",
    "unauthorized": "Unauthorized: Authentication failed. Check ServiceAccount token, API server credentials, or certificate expiration.",
    "unhealthy": "Unhealthy: Health probe failed. Check container port matches probe config, verify startup time, check probe path and application readiness.",
    "outofsync": "OutOfSync: Cluster state differs from Git. Check ArgoCD Application conditions, review resource diff, update Git or revert cluster changes.",
    "syncerror": "SyncError: ArgoCD sync failed. Check for invalid YAML, Helm values errors, resource conflicts, or RBAC issues during apply.",
}


def _normalize_error_type(error_type: str) -> str:
    return "".join(ch for ch in error_type.lower() if ch.isalnum())


def _canonical_error_key(error_type: str) -> str:
    normalized = _normalize_error_type(error_type)
    for key in QUERY_MAPPINGS:
        if key in normalized:
            return key
    return error_type.lower().strip()


def _has_expected_terms(result: dict, expected_terms: tuple[str, ...]) -> bool:
    if not expected_terms:
        return True
    text = f"{result.get('source', '')}\n{result.get('title', '')}\n{result.get('content', '')}".lower()
    return any(term in text for term in expected_terms)


def _relevant_snippet(content: str, expected_terms: tuple[str, ...], max_chars: int = 300) -> str:
    if len(content) <= max_chars:
        return content

    lower_content = content.lower()
    positions = [lower_content.find(term) for term in expected_terms if lower_content.find(term) >= 0]
    if not positions:
        return content[:max_chars]

    start = max(0, min(positions) - 80)
    end = min(len(content), start + max_chars)
    snippet = content[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet = snippet + "..."
    return snippet


def rag_search(error_type: str) -> str:
    """Search knowledge base for troubleshooting steps.

    Args:
        error_type: Error to search for (ImagePullBackOff, OOMKilled, CrashLoopBackOff, etc.)

    Returns:
        Relevant troubleshooting documentation with fix steps.
    """
    start_time = time.time()

    if retriever_instance is None:
        prom_metrics.RAG_SEARCHES.labels(status="not_configured").inc()
        prom_metrics.TOOL_CALLS.labels(tool="rag_search", status="error").inc()
        # QUOTA-SURGICAL: Explicit instruction to prevent LLM retry loop
        return (
            "Knowledge base is currently unavailable. "
            "DO NOT retry RAG search. "
            "Proceed with diagnosis using the pre-loaded cluster signals only. "
            "Focus on warningEvents, podStatuses, and preloadedLogs in the prompt."
        )

    try:
        # Build a more specific query based on error type.
        canonical_error = _canonical_error_key(error_type)
        query = QUERY_MAPPINGS.get(canonical_error)
        if not query:
            # Fallback to error type with context
            query = f"{error_type} Kubernetes troubleshoot fix pod container error"
        
        results = retriever_instance.search(query, top_k=3)

        prom_metrics.RAG_SEARCH_DURATION.observe(time.time() - start_time)
        prom_metrics.RAG_RESULTS_COUNT.observe(len(results))

        if not results:
            prom_metrics.RAG_SEARCHES.labels(status="no_results").inc()
            prom_metrics.TOOL_CALLS.labels(tool="rag_search", status="success").inc()
            return f"No documentation found for '{error_type}'. Proceed with diagnosis using cluster signals."

        # Format results concisely
        output_lines = []
        expected_terms = EXPECTED_TERMS.get(canonical_error, ())
        for i, r in enumerate(results, 1):
            source = r["source"].split("/")[-1]  # Just filename
            score = r.get("score", 0)

            if score > 0.65 and _has_expected_terms(r, expected_terms):
                content = _relevant_snippet(r["content"], expected_terms)
                output_lines.append(f"[{source}] (relevance: {score:.2f})")
                output_lines.append(content)
                output_lines.append("")

        if not output_lines:
            prom_metrics.RAG_SEARCHES.labels(status="low_relevance").inc()
            prom_metrics.TOOL_CALLS.labels(tool="rag_search", status="success").inc()
            # Provide inline help instead of useless low-relevance or off-topic docs.
            return INLINE_HELP.get(canonical_error, f"No highly relevant docs for '{error_type}'. Use cluster signals for diagnosis.")

        prom_metrics.RAG_SEARCHES.labels(status="success").inc()
        prom_metrics.TOOL_CALLS.labels(tool="rag_search", status="success").inc()
        return "\n".join(output_lines)

    except Exception as e:
        prom_metrics.RAG_SEARCHES.labels(status="error").inc()
        prom_metrics.TOOL_CALLS.labels(tool="rag_search", status="error").inc()
        return f"RAG search error: {str(e)}"


rag_search_tool = FunctionTool(rag_search)
