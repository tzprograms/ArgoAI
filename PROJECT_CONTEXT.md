# PROJECT CONTEXT: ArgoAI — Multi-Agent RAG Diagnostic System for Argo CD

> This file is the single source of truth for all architectural decisions, design details,
> and domain knowledge for the project. Any LLM or developer starting a new session should
> read this file FIRST before writing any code.

---

## 1. Project Overview

**Title:** ArgoAI

### Problem Statement

ArgoCD automates Kubernetes deployment synchronization via GitOps, but when applications
fail (Degraded, OutOfSync, Missing, Error states), developers must manually inspect pod logs,
K8s events, resource manifests, and configuration diffs to diagnose root causes. This is
time-consuming, error-prone, and requires deep K8s expertise.

### Solution

A two-service system (Go for K8s/API, Python for AI/agents) that sits alongside ArgoCD and provides:

1. **AI-Powered Fault Analysis** — Heuristic-first multi-agent diagnostic system with A2A-style routing
2. **RAG Knowledge Base** — Shared with OpenShift Lightspeed, containing KCS articles and CEE docs
3. **BYOM (Bring Your Own Model)** — Users provide their own LLM API key OR configure via K8s Secret
4. **OpenShift Console Plugin** — ArgoAI UI integrated into the OpenShift console under the GitOps sidebar

### Implemented Scope

- Heuristic A2A routing with 5 specialist agents (Runtime, Config, Network, Storage, RBAC) — **no LLM used for routing**
- All diagnosis goes through real LLM providers (Gemini, OpenAI, Anthropic, Groq, OpenRouter, Ollama)
- Shared RAG knowledge base (pre-built FAISS index from Lightspeed BYOK image)
- Secure API key handling (K8s Secrets + BYOM fallback)
- Semantic caching for diagnosis results (15-min TTL) and route caching (20-min TTL)
- Intelligent log filtering before LLM context
- SSE streaming of all reasoning steps to the UI
- Read-only cluster access (pod logs, events, resource manifests)
- App-scoped signal collection (events and pods filtered to the target ArgoCD app's resources)
- Prometheus metrics on both services (20+ metrics) and health checks
- Kubernetes deployment manifests (Kustomize, separate Go and Python pods in `argocd-agent` namespace)
- OpenShift Console dynamic plugin (React + PatternFly, ArgoAI under GitOps sidebar)
- Tool-call budget enforcement (default 3, hard cap 5) via ADK before/after tool callbacks
- Tool response truncation (default 1200 chars) before results are fed back into the LLM
- LLM diagnosis timeout (configurable, default 90s)
- Gemini JSON response mode only when no tools are attached (incompatible with function calling)
- Anti-hallucination prompts (tool whitelist, evidence requirement, explicit "say you don't know" rules)
- Ollama support with tools stripped (local models have unreliable tool calling)
- `setup-demo.sh` — single-command demo orchestrator for local development

**Out of scope (not implemented):**
- Remediation / applying fixes (read-only POC)
- Hybrid search (BM25 + dense vectors)
- CRDs (HealthAnalysis, AgentPolicy, etc.)
- Alertmanager webhook trigger
- Automated failure detection / watcher

---

## 2. System Architecture

### Two-Service Design

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Kubernetes Cluster                            │
│                                                                       │
│  ┌─────────────────────────────────┐                                 │
│  │  argocd-agent-go Pod (:8080)    │                                 │
│  │                                 │                                 │
│  │  External API:                  │                                 │
│  │    POST /api/v1/diagnose        │                                 │
│  │    GET  /api/v1/providers       │                                 │
│  │    GET  /api/v1/health          │                                 │
│  │    GET  /metrics                │                                 │
│  │    GET  /livez  /readyz         │                                 │
│  │                                 │                                 │
│  │  Internal K8s API:              │                                 │
│  │    POST /internal/k8s/events    │                                 │
│  │    POST /internal/k8s/pods      │                                 │
│  │    POST /internal/k8s/pod-logs  │                                 │
│  │    POST /internal/k8s/resource  │                                 │
│  │    POST /internal/argocd/app    │                                 │
│  │    POST /internal/argocd/diff   │                                 │
│  └────────────┬────────────────────┘                                 │
│               │ HTTP                                                  │
│  ┌────────────▼────────────────────┐                                 │
│  │  argocd-agent-python Pod (:8081)│                                 │
│  │                                 │                                 │
│  │  POST /diagnose                 │                                 │
│  │  GET  /health  /livez  /readyz  │                                 │
│  │  GET  /agents                   │                                 │
│  │  GET  /metrics                  │                                 │
│  │  GET  /cache/stats              │                                 │
│  │  POST /cache/clear              │                                 │
│  │                                 │                                 │
│  │  Heuristic Router → 5 Agents    │                                 │
│  │  FAISS Index (emptyDir volume)  │                                 │
│  └─────────────────────────────────┘                                 │
│                                                                       │
│  ┌─────────────────────────────────┐                                 │
│  │  ArgoAI Console Plugin          │                                 │
│  │  (OpenShift dynamic plugin)     │                                 │
│  └─────────────────────────────────┘                                 │
└───────────────────────────────────────────────────────────────────────┘
```

### Why Two Services (Go + Python)?

| Concern | Language | Reason |
|---------|----------|--------|
| K8s API interactions | Go | `client-go` is the official, fully-typed K8s client |
| AI/Agent orchestration | Python | Google ADK, LiteLLM, sentence-transformers, FAISS |
| Security isolation | Go | Python pod has NO K8s RBAC. Prompt injection can't reach K8s API. |

---

## 3. Agent Architecture (A2A-Based Routing)

### Routing Strategy — Heuristic Only (No LLM Triage)

The router **never calls an LLM for routing decisions**. It uses a pure heuristic strategy:

1. **Pod state matching (highest priority):** Match `stateReason`/`lastTerminatedReason` from pre-loaded pod statuses against each agent's `trigger_event_reasons`. Pod states are app-scoped.
2. **Event reason matching:** Match Kubernetes warning event reasons against trigger lists.
3. **Keyword matching:** Search event messages and pod state reasons for trigger keywords.
4. **Health/sync conditions:** Match ArgoCD health and sync status strings.
5. **Default:** Runtime Analyzer — never fails, never calls LLM.

Priority when multiple agents match: `storage > rbac > network > config > runtime`

Route decisions are cached for **20 minutes** by a fingerprint of (health, sync, warning reasons, pod state reasons).

### Specialist Agents

| Agent ID | Display Name | Triggers |
|----------|-------------|---------|
| `runtime` | Runtime Analyzer | `OOMKilled`, `CrashLoopBackOff`, `ImagePullBackOff`, `BackOff`, `Degraded` |
| `config` | Config Analyzer | `SyncError`, `ComparisonError`, `OutOfSync`, `revision` |
| `network` | Network Analyzer | `Unhealthy`, `connection refused`, `tls`, `NetworkNotReady` |
| `storage` | Storage Analyzer | `FailedMount`, `ProvisioningFailed`, `pvc`, `volume` |
| `rbac` | RBAC Analyzer | `Forbidden`, `cannot get`, `unauthorized`, `RBAC` |

### Agent Tools by Specialist

| Agent | Tools Available |
|-------|----------------|
| Runtime | `get_resource`, `get_pod_logs`, `list_pods`, `get_events`, `rag_search` |
| Config | `get_argocd_app`, `get_argocd_diff`, `get_resource`, `get_events`, `get_pod_logs`, `rag_search` |
| Network | `get_resource`, `list_pods`, `get_events`, `rag_search` |
| Storage | `get_resource`, `list_pods`, `get_events`, `rag_search` |
| RBAC | `get_resource`, `list_pods`, `get_events`, `rag_search` |

Ollama provider: all tools stripped, diagnosis from pre-loaded context only.

### Caching

| Cache | TTL | Max Entries | Purpose |
|-------|-----|------------|---------|
| Route Cache | 20 min | 150 | Avoid repeated routing heuristics |
| Diagnosis Cache | 15 min | 100 | Avoid re-analyzing identical issues |

---

## 4. Signal Pre-Loading

The Go service (`collectSignals`) gathers before any LLM call:

- ArgoCD Application health status, sync status, conditions
- Warning events filtered to resources owned by the target app (`isOwnedByManagedResource`)
- Pod statuses with container state details (phase, readiness, restart count, `stateReason`, `exitCode`, `lastTerminatedReason`) — filtered to app-owned pods
- Pre-loaded logs from the first unhealthy pod (100 tail lines, run through `LogFilter`)
- ArgoCD source info (repoURL, targetRevision)
- Destination namespace

The Python `LogFilter` pre-processes logs to extract:
- Lines matching ERROR/FATAL/PANIC patterns
- Lines matching WARNING patterns (up to 5)
- Patterns: `error`, `fail`, `oom`, `killed`, `crashloop`, `timeout`, `refused`, `denied`, `forbidden`, `not found`, `exit code [1-9]`
- Falls back to last 20 lines if no errors found

---

## 5. Tool System

### Two-Hop Architecture

```
LLM → ADK Agent → Python FunctionTool → HTTP POST → Go Internal API → K8s API
```

### Available Tools

| Tool | Go Endpoint | Description |
|------|-------------|-------------|
| `get_events` | `POST /internal/k8s/events` | List K8s events for a namespace (warnings prioritized) |
| `list_pods` | `POST /internal/k8s/pods` | List pods with status, restarts, container states |
| `get_resource` | `POST /internal/k8s/resource` | Fetch any K8s resource manifest (Secrets blocked) |
| `get_pod_logs` | `POST /internal/k8s/pod-logs` | Get filtered pod logs |
| `get_argocd_app` | `POST /internal/argocd/app` | Get ArgoCD Application health, sync, conditions |
| `get_argocd_diff` | `POST /internal/argocd/diff` | Get desired-vs-live diff |
| `rag_search` | (direct FAISS in Python) | Search the knowledge base |

`get_resource` has a built-in safeguard: `Secret` kind is blocked regardless of what the LLM requests.

`get_resource` returns structured summaries (not raw YAML) for: Deployment, ReplicaSet, StatefulSet, DaemonSet, Pod, Service, Ingress, PVC, PV. Raw YAML for other kinds.

### Budget Enforcement (ADK Callbacks)

- `_before_tool_callback`: Hard-stops tool execution when budget exhausted; returns error dict telling the LLM to finalize.
- `_after_tool_callback`: Truncates tool response to `MAX_TOOL_RESPONSE_CHARS` (default 1200) before feeding to next model round.
- `_before_model_callback`: Records estimated input tokens per LLM round.
- `_after_model_callback`: Records actual provider token usage; sanitizes function call names (strips control tokens).
- `_on_model_error_callback`: Records provider failures; redacts API keys from error text.

---

## 6. LLM Providers

| Provider alias | Default Model | Backend |
|----------------|---------------|---------|
| `gemini` / `google` | `gemini-2.5-flash` | Native ADK `Gemini` class |
| `openai` / `chatgpt` | `gpt-4o-mini` | LiteLLM `openai/gpt-4o-mini` |
| `anthropic` / `claude` | `claude-3-haiku-20240307` | LiteLLM `anthropic/...` |
| `groq` | `llama-3.1-8b-instant` | LiteLLM `groq/...` |
| `openrouter` | `openai/gpt-oss-20b:free` | LiteLLM `openrouter/...` |
| `ollama` | `qwen3:14b` | LiteLLM `ollama/...`, `api_base=http://localhost:11434`, no tools |

**Key lookup order (per request):**
1. `apiKey` in request body → BYOM mode
2. Kubernetes Secret `argocd-agent-llm-keys` in `argocd-agent` namespace
3. `ollama` → no key required
4. Error returned to client

**Secret keys:** `gemini-api-key`, `openai-api-key`, `anthropic-api-key`, `groq-api-key`, `openrouter-api-key`

---

## 7. RAG Architecture

### Pre-Built FAISS Index from Lightspeed BYOK

**Quay Image:** `quay.io/devtools_gitops/argocd_lightspeed_byok:v0.0.4`

| Property | Value |
|----------|-------|
| Format | LlamaIndex + FAISS (`faiss.IndexFlatIP`) |
| Embedding Model | `sentence-transformers/all-mpnet-base-v2` (768 dimensions) |
| Total Documents | 4,801 chunks |
| Relevance Threshold | 0.65 (inline help returned below this) |
| Index files | `default__vector_store.json`, `docstore.json`, `index_store.json` |

RAG is controlled by `ENABLE_RAG=true` (default). When scores are low, curated inline help is returned covering 13 common error types. RAG is disabled for Ollama.

**Extraction (one-time):**
```bash
make extract-rag
# or: docker run --rm -v $(pwd)/rag_data:/out quay.io/devtools_gitops/argocd_lightspeed_byok:v0.0.4 cp -r /rag/vector_db /out/
```

---

## 8. OpenShift Console Plugin

### Architecture

| Property | Value |
|----------|-------|
| Plugin name (CR) | `argocd-agent-plugin` |
| Display name | ArgoAI |
| Framework | React 17, TypeScript, PatternFly 6, Webpack 5 |
| Console SDK | `@openshift-console/dynamic-plugin-sdk` ^4.19.1 |
| Dev server port | 9001 |
| Nav location | Admin perspective, `gitops-navigation-section`, after GitOps items |
| Route | `/argoai` |

### UI Features

- **Application table** — Live watch of `argoproj.io/v1alpha1 Application` CRs via `useK8sWatchResource`. Shows Name, Namespace, Health (color-coded label), Sync (color-coded label), Destination.
- **Provider selector** — Dropdown for Gemini, OpenAI, Groq, Anthropic, OpenRouter, Ollama.
- **API key input** — Password field, hidden for Ollama.
- **Diagnose button** — Disabled until API key is entered (except Ollama). Triggers SSE stream.
- **Live agent log panel** — Real-time SSE event display with auto-scroll toggle. Event types rendered: `triage_start` (blue), `routing` (purple + agent name), `start` (blue), `tool_call` (teal + tool name), `tool_result` (teal + truncated output), `reasoning` (yellow), `warning` (orange), `cache_hit` (green), `usage` (grey), `error` (red).
- **Diagnosis result panel** — Structured display: Error, Root Cause, Fix, Agent, Confidence, Evidence (via `DiagnosisResult` component).

### API URL

```typescript
const GO_SERVICE_URL =
  (window as any).__ARGOAI_API_URL__ || 'http://localhost:8080';
```

Webpack dev server proxies `/api/v1/diagnose`, `/api/v1/health`, `/api/v1/providers` → `localhost:8080`.

### Local Development

```bash
# Terminal 1: ArgoAI plugin (port 9001)
cd console-plugin && yarn start

# Terminal 2: GitOps plugin (port 9002) — provides GitOps sidebar section
cd gitops-console-plugin && PORT=9002 yarn start --port 9002

# Terminal 3: OpenShift console container (port 9000)
cd console-plugin && ENABLE_GITOPS_PLUGIN=true ./start-console.sh

# Terminal 4-5: Backend services
make run-go        # Go on :8080
make run-agent     # Python on :8081

# Or everything in one command:
bash setup-demo.sh
```

Console available at `http://localhost:9000`. Navigate to **GitOps > ArgoAI**.
Direct URL: `http://localhost:9000/argoai`

---

## 9. Observability

### Prometheus Metrics (Python service)

| Metric | Type | Description |
|--------|------|-------------|
| `diagnosis_requests_total` | Counter | By provider, agent, status |
| `diagnosis_duration_seconds` | Histogram | End-to-end latency |
| `active_diagnoses` | Gauge | Currently running |
| `agent_steps_total` | Counter | By agent, step type |
| `tool_calls_total` | Counter | By tool, status |
| `tool_call_duration_seconds` | Histogram | Per tool |
| `rag_searches_total` | Counter | By status |
| `rag_search_duration_seconds` | Histogram | |
| `rag_results_count` | Histogram | Chunks returned |
| `rag_index_size` | Gauge | Total vectors in FAISS |
| `rag_loaded` | Gauge | 1 if loaded |
| `cache_hits_total` | Counter | Diagnosis cache hits |
| `cache_misses_total` | Counter | Diagnosis cache misses |
| `triage_decisions_total` | Counter | By agent, method (pod_state/heuristic/heuristic_priority/cached/default) |
| `triage_duration_seconds` | Histogram | By method |
| `llm_requests_total` | Counter | By provider, status |
| `llm_tokens_used_total` | Counter | By provider, type (input/output/total/input_estimated) |
| `go_service_calls_total` | Counter | By endpoint, status |
| `go_service_call_duration_seconds` | Histogram | By endpoint |

Grafana dashboard: `config/monitoring/`

### Health Checks

| Endpoint | Service | Description |
|----------|---------|-------------|
| `GET /api/v1/health` | Go | JSON `{"status":"ok"}` |
| `GET /livez` | Go | Liveness |
| `GET /readyz` | Go | Readiness |
| `GET /healthz` | Go | Combined |
| `GET /health` | Python | JSON with `rag_loaded`, `go_service_healthy` |
| `GET /livez` | Python | Liveness |
| `GET /readyz` | Python | Readiness |

---

## 10. Security

### API Key Handling

**Mode 1: Kubernetes Secret (org-wide)**
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: argocd-agent-llm-keys
  namespace: argocd-agent
stringData:
  gemini-api-key: "your-key"
  openai-api-key: "your-key"
  anthropic-api-key: "your-key"
  groq-api-key: "your-key"
  openrouter-api-key: "your-key"
```

**Mode 2: BYOM (per-request)**
```json
{"appName": "my-app", "provider": "gemini", "apiKey": "user-key"}
```

### Security Features

- Secret access restricted to `argocd-agent-go` ServiceAccount (Role + RoleBinding scoped to the secret)
- Python pod has **zero** K8s RBAC permissions — it cannot call the K8s API directly
- `Secret` kind explicitly blocked in `get_resource` tool
- API keys never logged; redacted in error messages (`AIza...` and `sk-...` patterns)
- Thread-local storage for API keys (prevents concurrent request credential leakage)

---

## 11. Project Structure

```
ArgoAI/
│
│  ── Go Service (K8s + API) ──
├── cmd/server/main.go              # Go HTTP server, CLI flags, wiring
├── internal/
│   ├── api/handler.go              # External API + signal collection + SSE proxy to Python
│   ├── k8s/handler.go              # Internal K8s data endpoints + resource summarizers
│   ├── health/health.go            # Liveness/readiness checks
│   ├── metrics/metrics.go          # Prometheus middleware + metrics
│   └── secrets/secrets.go          # K8s secret lookup + BYOM fallback
│
│  ── Python Agent Service ──
├── agent/
│   ├── main.py                     # FastAPI server (:8081), lifespan, RAG init
│   ├── engine.py                   # Router, agent builder, diagnosis runner, caches, log filter
│   ├── metrics.py                  # All 20 Prometheus metrics
│   ├── agents/
│   │   ├── base.py                 # AgentCard, AgentSkill dataclasses + heuristic matching
│   │   ├── router.py               # AgentCardRouter (heuristic-only, no LLM triage)
│   │   ├── prompts.py              # Shared prompt utilities
│   │   ├── runtime_agent.py        # Runtime Analyzer AgentCard + prompt
│   │   ├── config_agent.py         # Config Analyzer AgentCard + prompt
│   │   ├── network_agent.py        # Network Analyzer AgentCard + prompt
│   │   ├── storage_agent.py        # Storage Analyzer AgentCard + prompt
│   │   └── rbac_agent.py           # RBAC Analyzer AgentCard + prompt
│   ├── tools/
│   │   ├── k8s_tools.py            # 6 K8s FunctionTools + LogFilter + Go service HTTP client
│   │   └── rag_tools.py            # RAG search FunctionTool
│   └── rag/
│       ├── retriever.py            # FAISS index loader + embedding model + search
│       └── chunker.py              # Document chunker (for building new indexes)
│
│  ── Console Plugin (ArgoAI UI) ──
├── console-plugin/
│   ├── package.json                # Plugin metadata + dependencies
│   ├── console-extensions.json     # Nav item (gitops-navigation-section) + page route
│   ├── plugin-metadata.ts          # Webpack module federation config
│   ├── webpack.config.ts           # Build config + dev server proxy rules
│   ├── start-console.sh            # Local dev console runner (docker/podman)
│   └── src/
│       ├── components/
│       │   ├── ArgoAgentPage.tsx    # Main page: app table + provider selector + diagnosis trigger
│       │   ├── DiagnosisPanel.tsx   # SSE event stream display + auto-scroll
│       │   └── DiagnosisResult.tsx  # Structured diagnosis output
│       └── utils/
│           └── api.ts              # SSE client + health check
│
│  ── Kubernetes Deployment ──
├── config/
│   ├── deploy/
│   │   ├── kustomization.yaml      # Namespace + go-service + python-service + servicemonitor
│   │   ├── namespace.yaml          # argocd-agent namespace
│   │   ├── go-service.yaml         # Deployment + Service + SA + ClusterRole + ServiceMonitor
│   │   ├── python-service.yaml     # Deployment + init container (RAG extraction)
│   │   ├── servicemonitor.yaml     # Prometheus Operator scrape config
│   │   └── llm-keys-secret.yaml    # Secret template (not in kustomization, apply separately)
│   └── monitoring/                 # Grafana dashboard JSON
│
│  ── Demo Scenarios ──
├── demo/
│   ├── oomkilled/deployment.yaml   # 64MB limit, exits with OOMKilled
│   ├── imagepull/deployment.yaml   # Nonexistent image tag
│   ├── crashloop/deployment.yaml   # App exits code 1 (fake DB error)
│   ├── missing-config/deployment.yaml  # References nonexistent ConfigMap
│   ├── network-issue/deployment.yaml   # Liveness probe on wrong port
│   ├── storage-issue/deployment.yaml   # PVC with nonexistent StorageClass
│   ├── rbac-issue/deployment.yaml      # ServiceAccount lacks permissions
│   ├── deploy-all.sh               # Deploy all 7 scenarios
│   └── cleanup.sh                  # Remove all demo resources
│
│  ── Docker ──
├── Dockerfile.server               # Go service image (multi-stage + distroless)
├── Dockerfile.agent                # Python agent image (slim + uv)
├── docker-compose.yml              # Local docker-compose for both services
│
│  ── Tooling ──
├── Makefile                        # run-go, run-agent, extract-rag, docker-build, deploy, demo-apps
├── setup-demo.sh                   # One-command demo orchestrator (all 7 steps)
├── pyproject.toml                  # Python package config, deps, Python ≥ 3.12
├── go.mod / go.sum                 # Go module: github.com/argoai/argocd-agent, Go 1.24
│
│  ── Documentation ──
├── README.md                       # Quick start + demo instructions
├── PROJECT_CONTEXT.md              # This file — architecture source of truth
└── docs/
    └── OBSERVABILITY.md            # Metrics and monitoring guide
```

---

## 12. Running the POC

### Prerequisites

1. OpenShift / ROSA cluster with `oc login`
2. Go 1.24+, Python 3.12+, uv, Node.js 18+, Yarn, Docker

### One-Command Demo

```bash
# One-time: clone GitOps plugin for the sidebar
git clone https://github.com/openshift/gitops-console-plugin.git

# One-time: extract RAG data
make extract-rag

# Run everything
bash setup-demo.sh
```

Open `http://localhost:9000` → **GitOps > ArgoAI**

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GO_SERVICE_URL` | `http://localhost:8080` | Go service URL for Python agent |
| `RAG_INDEX_PATH` | `/rag/vector_db` | Path to FAISS index |
| `ENABLE_RAG` | `true` | Enable/disable RAG tool |
| `DIAGNOSIS_TIMEOUT_SECONDS` | `90` | Max seconds per diagnosis |
| `MAX_TOOL_CALLS` | `3` | Tool call budget (hard cap 5) |
| `MAX_TOOL_RESPONSE_CHARS` | `1200` | Max chars per tool result |
| `LLM_MAX_OUTPUT_TOKENS` | `1024` | Max output tokens per model round |
| `LLM_TEMPERATURE` | `0.2` | Model temperature |
| `GEMINI_THINKING_BUDGET` | `0` | Gemini 2.5 thinking budget (0 = off) |
| `TRANSFORMERS_CACHE` | `/.cache` | HuggingFace model cache |
| `UVICORN_WORKERS` | `1` | Python workers (keep at 1 for ADK sessions) |

### Go CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--addr` | `:8080` | HTTP listen address |
| `--kubeconfig` | `""` | Path to kubeconfig (uses in-cluster config if empty) |
| `--agent-url` | `http://localhost:8081` | Python agent service URL |
| `--secret-namespace` | `""` | Namespace for LLM keys secret (auto-detected from pod SA) |

---

## 13. Test Scenarios

| # | App Name | Root Cause | Expected Agent |
|---|----------|------------|----------------|
| 1 | `demo-oomkilled` | Memory limit 64MB, requests 256MB | Runtime Analyzer |
| 2 | `demo-imagepull` | Nonexistent image tag | Runtime Analyzer |
| 3 | `demo-crashloop` | App exits code 1 (fake DB connection error) | Runtime Analyzer |
| 4 | `demo-missing-config` | References nonexistent ConfigMap | Runtime Analyzer |
| 5 | `demo-network-issue` | Liveness probe on wrong port | Network Analyzer |
| 6 | `demo-storage-issue` | PVC references nonexistent StorageClass | Storage Analyzer |
| 7 | `demo-rbac-issue` | ServiceAccount lacks permissions | RBAC Analyzer |

Scenarios 1-5 are deployed by `setup-demo.sh`. All 7 are deployed by `demo/deploy-all.sh`.

---

## 14. Key Design Decisions

1. **Heuristic-only routing** — The router never calls an LLM. Pod state reasons are the most reliable signal; heuristic priority (storage > rbac > network > config > runtime) resolves multi-matches deterministically. This eliminates a full LLM round-trip on every request.

2. **Two-service split (Go + Python)** — Security isolation and best-of-both-worlds: client-go for K8s, Google ADK + LiteLLM for agents.

3. **A2A-style AgentCards** — Each specialist agent exposes its capabilities as a structured card with trigger lists. The router uses these cards for discovery and matching without coupling routing logic to individual agent implementations.

4. **Shared RAG with Lightspeed** — Same FAISS index as OpenShift Lightspeed BYOK. No duplicate infrastructure.

5. **Semantic caching (two levels)** — Route cache (20 min, fingerprint excludes transient data) and diagnosis cache (15 min, cause-specific fingerprint includes image name, ConfigMap name, PVC name to prevent wrong cache hits).

6. **Log filtering before LLM** — `LogFilter` reduces raw logs to error/warning lines only, capped at 2000 chars, preventing context window bloat.

7. **App-scoped signal collection** — Events and pods are filtered to resources owned by the target ArgoCD app using `isOwnedByManagedResource`. This prevents namespace-level noise from confusing the agent.

8. **Tool-call limits via ADK callbacks** — `before_tool_callback` intercepts at the framework level before any tool executes. `after_tool_callback` truncates responses. This is more reliable than prompt instructions alone.

9. **JSON response mode only for no-tool Gemini runs** — `response_mime_type: application/json` is incompatible with Gemini function calling. Applied only when `tools=[]`.

10. **Anti-hallucination prompts** — Tool whitelist, evidence requirement, explicit "say you don't know" rules, and `Secret` kind blocked at the API layer.

11. **Read-only POC** — No remediation, only diagnosis and actionable fix suggestions. All fixes must go through Git (GitOps).

12. **Ollama context-only mode** — Local models have unreliable tool calling. Tools are stripped and the agent diagnoses purely from pre-loaded signals in the prompt.
