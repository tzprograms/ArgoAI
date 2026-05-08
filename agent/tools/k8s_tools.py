"""K8s tools - Optimized for minimal tokens, maximum diagnostic value.

Design: Each tool returns ~100-200 tokens of highly relevant data.
Log filtering is applied before returning logs to the LLM.
"""

import json
import re
import time
import httpx
from google.adk.tools import FunctionTool

from agent import metrics as prom_metrics

GO_SERVICE_URL = "http://localhost:8080"


def _set_go_service_url(url: str):
    global GO_SERVICE_URL
    GO_SERVICE_URL = url


def _call_go(endpoint: str, payload: dict) -> dict:
    """Call Go service with metrics tracking."""
    start_time = time.time()
    try:
        resp = httpx.post(f"{GO_SERVICE_URL}{endpoint}", json=payload, timeout=30.0)
        resp.raise_for_status()
        prom_metrics.GO_SERVICE_CALLS.labels(endpoint=endpoint, status="success").inc()
        prom_metrics.TOOL_CALLS.labels(tool=endpoint.split("/")[-1], status="success").inc()
        return resp.json()
    except httpx.HTTPStatusError as e:
        prom_metrics.GO_SERVICE_CALLS.labels(endpoint=endpoint, status="http_error").inc()
        prom_metrics.TOOL_CALLS.labels(tool=endpoint.split("/")[-1], status="error").inc()
        return {"error": str(e)}
    except Exception as e:
        prom_metrics.GO_SERVICE_CALLS.labels(endpoint=endpoint, status="connection_error").inc()
        prom_metrics.TOOL_CALLS.labels(tool=endpoint.split("/")[-1], status="error").inc()
        return {"error": str(e)}
    finally:
        prom_metrics.GO_SERVICE_CALL_DURATION.labels(endpoint=endpoint).observe(time.time() - start_time)
        prom_metrics.TOOL_CALL_DURATION.labels(tool=endpoint.split("/")[-1]).observe(time.time() - start_time)


# =============================================================================
# LOG FILTERING - Extract only diagnostic-relevant content
# =============================================================================

class _LogFilter:
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

            # Capture ERROR level lines with context
            if self._level_res['error'].search(line):
                errors.append(line)
                # Include next 2 lines as stack trace context
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
                # Deduplicate by normalizing timestamps and UUIDs
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
            # No explicit errors found - return last N lines as context
            last_lines = '\n'.join(lines[-20:])
            return f"(No explicit errors found. Last 20 lines:)\n{last_lines}"[:self.max_output]

        return result[:self.max_output]


# Global filter instance
_log_filter = _LogFilter()


# =============================================================================
# CORE DIAGNOSTIC TOOLS (Pareto: these 2 solve 80% of cases)
# =============================================================================

def get_events(namespace: str) -> str:
    """Get pod failure events. Shows ImagePullBackOff, OOMKilled, CrashLoop errors."""
    result = _call_go("/internal/k8s/events", {"namespace": namespace})

    if "error" in result:
        return f"Error: {result['error']}"

    events = result.get("events", [])
    if not events:
        return "No events found"

    # Only return Warning events (errors) - most diagnostic value
    warnings = [e for e in events if e.get("type") == "Warning"]
    if not warnings:
        return "No warning events"

    # Format: compact, scannable
    lines = []
    for e in warnings[-5:]:  # Last 5 warnings only
        reason = e.get("reason", "Unknown")
        msg = e.get("message", "")[:120]  # Truncate long messages
        obj = e.get("object", "")
        lines.append(f"[{reason}] {obj}: {msg}")

    return "\n".join(lines)


def get_argocd_app(app_name: str, namespace: str = "") -> str:
    """Get ArgoCD app health/sync status. Shows if app is Healthy, Degraded, OutOfSync.

    Args:
        app_name: Name of the ArgoCD Application
        namespace: ArgoCD namespace (defaults to common namespaces: argocd, openshift-gitops)

    Returns:
        Application health, sync status, and conditions
    """
    import os

    # Try provided namespace first, then common ArgoCD namespaces
    namespaces_to_try = []
    if namespace:
        namespaces_to_try.append(namespace)
    else:
        # Check environment for default, then try common namespaces
        default_ns = os.environ.get("ARGOCD_NAMESPACE", "")
        if default_ns:
            namespaces_to_try.append(default_ns)
        namespaces_to_try.extend(["openshift-gitops", "argocd"])

    # Remove duplicates while preserving order
    seen = set()
    unique_namespaces = []
    for ns in namespaces_to_try:
        if ns and ns not in seen:
            seen.add(ns)
            unique_namespaces.append(ns)

    last_error = None
    for ns in unique_namespaces:
        result = _call_go("/internal/argocd/app", {
            "name": app_name,
            "namespace": ns
        })

        if "error" not in result:
            # Found the app
            health = result.get("health", {})
            sync = result.get("sync", {})
            conditions = result.get("conditions", [])

            lines = [
                f"Namespace: {ns}",
                f"Health: {health.get('status', 'Unknown')}",
                f"Sync: {sync.get('status', 'Unknown')}",
            ]

            # Add first condition message if exists
            if conditions and isinstance(conditions[0], dict):
                msg = conditions[0].get("message", "")[:100]
                if msg:
                    lines.append(f"Condition: {msg}")

            return "\n".join(lines)

        last_error = result.get("error", "Unknown error")

    return f"Error: Application '{app_name}' not found in namespaces: {unique_namespaces}. Last error: {last_error}"


# =============================================================================
# POD LOGS TOOL - With intelligent filtering
# =============================================================================

def get_pod_logs(namespace: str, pod: str, container: str = "", previous: bool = False) -> str:
    """Get filtered pod logs. Extracts only errors and warnings to save tokens.

    Args:
        namespace: Kubernetes namespace
        pod: Pod name
        container: Container name (optional, defaults to first container)
        previous: If True, get logs from previous container instance (for crashed pods)

    Returns:
        Filtered log output with only errors, warnings, and relevant context
    """
    result = _call_go("/internal/k8s/pod-logs", {
        "namespace": namespace,
        "pod": pod,
        "container": container,
        "tail": 200,  # Get last 200 lines for filtering
        "previous": previous,
    })

    if "error" in result:
        return f"Error: {result['error']}"

    raw_logs = result.get("logs", "")
    if not raw_logs:
        return "No logs available"

    # Apply intelligent filtering
    filtered = _log_filter.filter_logs(raw_logs)

    # Add metadata
    prefix = f"[Pod: {pod}"
    if container:
        prefix += f", Container: {container}"
    if previous:
        prefix += ", Previous instance"
    prefix += "]\n"

    return prefix + filtered


# =============================================================================
# SUPPLEMENTARY TOOLS (for deeper investigation if needed)
# =============================================================================

def list_pods(namespace: str) -> str:
    """List pods with status. Quick overview of pod health."""
    result = _call_go("/internal/k8s/pods", {"namespace": namespace})

    if "error" in result:
        return f"Error: {result['error']}"

    pods = result.get("pods", [])
    if not pods:
        return "No pods found"

    # Compact format
    lines = []
    for p in pods[:8]:  # Max 8 pods
        name = p.get("name", "unknown")[-40:]  # Truncate long names
        status = p.get("status", "Unknown")
        restarts = p.get("restarts", 0)
        lines.append(f"{name}: {status} (restarts: {restarts})")

    return "\n".join(lines)


def get_resource(kind: str, name: str, namespace: str = "") -> str:
    """Get K8s resource diagnostic summary.

    Supports: Deployment, StatefulSet, DaemonSet, Pod, Service, Ingress,
    PVC, PV, StorageClass, ServiceAccount, Role, RoleBinding,
    ClusterRole, ClusterRoleBinding, NetworkPolicy, HPA, ConfigMap, Route.

    Args:
        kind: Resource kind (case-insensitive)
        name: Resource name
        namespace: Namespace (optional for cluster-scoped resources)

    Returns:
        Compact diagnostic summary with relevant fields for the resource type
    """
    result = _call_go("/internal/k8s/resource", {
        "kind": kind,
        "namespace": namespace,
        "name": name,
    })

    if "error" in result:
        error_msg = result['error']
        # Provide helpful guidance when resource not found
        if "not found" in error_msg.lower():
            return (
                f"Resource {kind}/{name} not found in namespace '{namespace or 'default'}'. "
                f"USE THE PRE-LOADED DATA in the prompt (podStatuses, warningEvents, preloadedLogs) "
                f"to diagnose. Do NOT retry this tool with different parameters."
            )
        return f"Error: {error_msg}"

    # Go service now returns pre-formatted summaries per resource type
    # Just format as compact JSON
    return json.dumps(result, indent=2, default=str)[:2000]


def get_argocd_diff(app_name: str, namespace: str = "") -> str:
    """Get ArgoCD resource diff showing what is out of sync between Git and cluster.

    Args:
        app_name: Name of the ArgoCD Application
        namespace: ArgoCD namespace (defaults to common namespaces)

    Returns:
        Sync status, resource list with sync state, and condition messages
    """
    import os

    namespaces_to_try = []
    if namespace:
        namespaces_to_try.append(namespace)
    else:
        default_ns = os.environ.get("ARGOCD_NAMESPACE", "")
        if default_ns:
            namespaces_to_try.append(default_ns)
        namespaces_to_try.extend(["openshift-gitops", "argocd"])

    seen = set()
    unique_namespaces = [ns for ns in namespaces_to_try if ns and ns not in seen and not seen.add(ns)]

    last_error = None
    for ns in unique_namespaces:
        result = _call_go("/internal/argocd/diff", {
            "name": app_name,
            "namespace": ns
        })

        if "error" not in result:
            sync_status = result.get("syncStatus", "Unknown")
            resources = result.get("resources", [])
            conditions = result.get("conditions", [])

            lines = [f"Sync: {sync_status}"]

            out_of_sync = [r for r in (resources or []) if isinstance(r, dict) and r.get("status") != "Synced"]
            if out_of_sync:
                lines.append(f"Out-of-sync resources ({len(out_of_sync)}):")
                for r in out_of_sync[:10]:
                    lines.append(f"  - {r.get('kind', '?')}/{r.get('name', '?')}: {r.get('status', '?')}")

            if conditions:
                lines.append("Conditions:")
                for c in (conditions[:5] if isinstance(conditions, list) else []):
                    if isinstance(c, dict):
                        lines.append(f"  - [{c.get('type', '?')}] {c.get('message', '')[:120]}")

            return "\n".join(lines)

        last_error = result.get("error", "Unknown error")

    return f"Error: Application '{app_name}' not found. Last error: {last_error}"


# =============================================================================
# TOOL INSTANCES
# =============================================================================

get_events_tool = FunctionTool(get_events)
get_argocd_app_tool = FunctionTool(get_argocd_app)
get_argocd_diff_tool = FunctionTool(get_argocd_diff)
get_pod_logs_tool = FunctionTool(get_pod_logs)
list_pods_tool = FunctionTool(list_pods)
get_resource_tool = FunctionTool(get_resource)

ALL_K8S_TOOLS = [list_pods_tool, get_events_tool, get_resource_tool, get_pod_logs_tool]
ALL_ARGOCD_TOOLS = [get_argocd_app_tool, get_argocd_diff_tool]
ALL_TOOLS = ALL_K8S_TOOLS + ALL_ARGOCD_TOOLS
