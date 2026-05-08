# Observability Guide

This document covers the metrics, health checks, and monitoring setup for the ArgoCD Agent.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Kubernetes Cluster                            │
│                                                                      │
│  ┌──────────────────────┐          ┌──────────────────────┐         │
│  │  argocd-agent-go     │          │  argocd-agent-python │         │
│  │  (Deployment)        │          │  (Deployment)        │         │
│  │                      │          │                      │         │
│  │  :8080               │   HTTP   │  :8081               │         │
│  │  ├── /metrics ◄──────┼──────────┼── /metrics ◄────────┼────┐    │
│  │  ├── /livez          │          │  ├── /livez          │    │    │
│  │  ├── /readyz         │          │  ├── /readyz         │    │    │
│  │  └── /healthz        │          │  └── /health         │    │    │
│  └──────────────────────┘          └──────────────────────┘    │    │
│           │                                  │                  │    │
│           └──────────────┬───────────────────┘                  │    │
│                          │                                      │    │
│  ┌───────────────────────▼──────────────────────────────────────┴┐   │
│  │                    ServiceMonitor                              │   │
│  │   (scrapes both /metrics endpoints every 30s)                  │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                          │                                            │
│                          ▼                                            │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │                    Prometheus                                   │   │
│  │   (stores metrics, evaluates alerts)                            │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                          │                                            │
│                          ▼                                            │
│  ┌────────────────────────────────────────────────────────────────┐   │
│  │                    Grafana                                      │   │
│  │   (dashboards, visualization)                                   │   │
│  └────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## Health Check Endpoints

### Go Service (`:8080`)

| Endpoint | Purpose | K8s Probe |
|----------|---------|-----------|
| `/livez` | Liveness probe - is the process alive? | `livenessProbe` |
| `/readyz` | Readiness probe - is the service ready for traffic? | `readinessProbe` |
| `/healthz` | Combined health check (backwards compatible) | - |

**Liveness (`/livez`):**
- Returns `200 OK` if the process can respond
- Kubernetes restarts the pod if this fails

**Readiness (`/readyz`):**
- Checks K8s API connectivity
- Checks Python agent service connectivity (optional, degraded mode OK)
- Kubernetes removes pod from service if this fails

Example response:
```json
{
  "status": "ready",
  "checks": {
    "kubernetes": {"healthy": true, "lastCheck": "2024-01-15T10:30:00Z"},
    "agentService": {"healthy": true, "lastCheck": "2024-01-15T10:30:00Z"}
  }
}
```

### Python Service (`:8081`)

| Endpoint | Purpose | K8s Probe |
|----------|---------|-----------|
| `/livez` | Liveness probe | `livenessProbe` |
| `/readyz` | Readiness probe | `readinessProbe` |
| `/health` | Simple health check | `startupProbe` |

**Readiness (`/readyz`):**
- Checks RAG index loaded status
- Checks Go service connectivity (optional)

## Prometheus Metrics

### Go Service Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `argocd_agent_http_requests_total` | Counter | `method`, `endpoint`, `status` | Total HTTP requests |
| `argocd_agent_http_request_duration_seconds` | Histogram | `method`, `endpoint` | Request latency |
| `argocd_agent_http_active_requests` | Gauge | - | Currently active requests |
| `argocd_agent_diagnosis_requests_total` | Counter | `provider`, `status` | Diagnosis requests by provider |
| `argocd_agent_diagnosis_duration_seconds` | Histogram | `provider` | Diagnosis latency |
| `argocd_agent_k8s_api_calls_total` | Counter | `endpoint`, `status` | K8s API calls |
| `argocd_agent_k8s_api_call_duration_seconds` | Histogram | `endpoint` | K8s API latency |
| `argocd_agent_python_service_up` | Gauge | - | Python service health (1=up, 0=down) |

### Python Service Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `argocd_agent_diagnosis_requests_total` | Counter | `provider`, `status` | Diagnosis requests |
| `argocd_agent_diagnosis_duration_seconds` | Histogram | `provider` | Diagnosis latency |
| `argocd_agent_active_diagnoses` | Gauge | - | Currently running diagnoses |
| `argocd_agent_tool_calls_total` | Counter | `tool`, `status` | Tool invocations |
| `argocd_agent_tool_call_duration_seconds` | Histogram | `tool` | Tool call latency |
| `argocd_agent_rag_searches_total` | Counter | `status` | RAG search count |
| `argocd_agent_rag_search_duration_seconds` | Histogram | - | RAG search latency |
| `argocd_agent_rag_results_count` | Histogram | - | Number of RAG results returned |
| `argocd_agent_rag_index_size` | Gauge | - | Number of vectors in RAG index |
| `argocd_agent_rag_loaded` | Gauge | - | RAG index loaded (1=yes, 0=no) |
| `argocd_agent_go_service_calls_total` | Counter | `endpoint`, `status` | Calls to Go service |
| `argocd_agent_go_service_call_duration_seconds` | Histogram | `endpoint` | Go service call latency |

## Deployment

### Prerequisites

- Kubernetes cluster with Prometheus Operator installed
- `monitoring.coreos.com/v1` ServiceMonitor CRD available

### Deploy with Kustomize

```bash
# Deploy all components
kubectl apply -k config/deploy/

# Check status
make status
```

### Verify Metrics

```bash
# Port-forward to Go service
kubectl -n argocd-agent port-forward svc/argocd-agent-go 8080:8080

# Check metrics
curl http://localhost:8080/metrics | grep argocd_agent

# Port-forward to Python service
kubectl -n argocd-agent port-forward svc/argocd-agent-python 8081:8081

# Check metrics
curl http://localhost:8081/metrics | grep argocd_agent
```

### Verify ServiceMonitor is Picked Up

```bash
# Check ServiceMonitor exists
kubectl -n argocd-agent get servicemonitor

# Check Prometheus targets (if you have access to Prometheus UI)
# Navigate to Status -> Targets and look for argocd-agent-*
```

## Grafana Dashboard

A pre-built Grafana dashboard is available at:
```
config/monitoring/grafana-dashboard.json
```

### Import Dashboard

1. Open Grafana
2. Go to Dashboards → Import
3. Upload or paste the JSON from `config/monitoring/grafana-dashboard.json`
4. Select your Prometheus data source
5. Click Import

### Dashboard Panels

- **Overview Row:**
  - Diagnoses (24h) - total successful diagnoses
  - Diagnosis P95 Latency - 95th percentile response time
  - Active Diagnoses - currently running
  - RAG Status - index loaded indicator

- **Diagnosis Requests Row:**
  - Request rate by provider and status
  - Duration percentiles (p50, p95, p99) by provider

- **Tool Calls & RAG Row:**
  - Tool call distribution
  - Tool and RAG latency

- **Go Service HTTP Row:**
  - HTTP request rate by endpoint
  - Active HTTP requests

## Alerting Examples

Example Prometheus alerting rules (add to your PrometheusRule):

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: argocd-agent-alerts
  namespace: argocd-agent
spec:
  groups:
    - name: argocd-agent
      rules:
        - alert: ArgoCDAgentHighErrorRate
          expr: |
            sum(rate(argocd_agent_diagnosis_requests_total{status!="success"}[5m])) /
            sum(rate(argocd_agent_diagnosis_requests_total[5m])) > 0.1
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "ArgoCD Agent error rate > 10%"
            
        - alert: ArgoCDAgentRAGDown
          expr: argocd_agent_rag_loaded == 0
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "ArgoCD Agent RAG index not loaded"
            
        - alert: ArgoCDAgentHighLatency
          expr: |
            histogram_quantile(0.95, sum(rate(argocd_agent_diagnosis_duration_seconds_bucket[5m])) by (le)) > 120
          for: 5m
          labels:
            severity: warning
          annotations:
            summary: "ArgoCD Agent P95 latency > 2 minutes"
```

## Local Development

When running locally, metrics are available at:
- Go service: `http://localhost:8080/metrics`
- Python service: `http://localhost:8081/metrics`

You can use tools like `promtool` or run a local Prometheus to scrape these endpoints.
