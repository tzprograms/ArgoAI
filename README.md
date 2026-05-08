# ArgoAI — Multi-Agent RAG Diagnostic System for Argo CD

ArgoAI is an AI-powered fault diagnosis system that sits alongside Argo CD on OpenShift/Kubernetes. When an application breaks — OOMKilled, ImagePullBackOff, CrashLoopBackOff, sync errors, probe failures — ArgoAI automatically routes the problem to the right specialist AI agent, gathers evidence from the cluster, and returns a structured root-cause diagnosis with a recommended fix.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   OpenShift Console                      │
│              ArgoAI Plugin (GitOps > ArgoAI)            │
└────────────────────────┬────────────────────────────────┘
                         │ SSE stream
┌────────────────────────▼────────────────────────────────┐
│              Go Service  :8080                           │
│   /api/v1/diagnose   /api/v1/health   /metrics          │
│   Signal collector · Secret manager · K8s client-go     │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP
┌────────────────────────▼────────────────────────────────┐
│              Python Agent  :8081                         │
│   A2A Router → 5 Specialist Agents                      │
│   Runtime · Config · Network · Storage · RBAC           │
│   Semantic cache · Log filter · FAISS RAG               │
│   Google ADK · LiteLLM · Gemini / OpenAI / Groq / ...  │
└────────────────────────┬────────────────────────────────┘
                         │ Tool calls → HTTP → Go Service → K8s API
┌────────────────────────▼────────────────────────────────┐
│              OpenShift / ROSA Cluster                    │
│   openshift-gitops · argocd-agent · default namespace   │
└─────────────────────────────────────────────────────────┘
```

## Prerequisites

- **OpenShift / ROSA cluster** with `oc` CLI logged in (`oc login`)
- **Go 1.24+** — `go version`
- **Python 3.12+** with **uv** — `pip install uv` or `brew install uv`
- **Docker** (for RAG data extraction and local console)
- **Node.js 18+ and Yarn** — `npm install -g yarn`
- An **LLM API key** — [Gemini (free)](https://aistudio.google.com/app/apikey), OpenAI, Groq, Anthropic, or Ollama (local, no key)

## Quick Start (Full Demo — One Command)

### Step 1 — Clone the GitOps console plugin (one-time)

This enables the GitOps sidebar in the local console so ArgoAI appears under **GitOps > ArgoAI**:

```bash
git clone https://github.com/openshift/gitops-console-plugin.git
```

### Step 2 — Extract the RAG knowledge base (one-time)

```bash
make extract-rag
```

This pulls a pre-built FAISS vector index (4,801 chunks from KCS articles and CEE docs) from `quay.io/devtools_gitops/argocd_lightspeed_byok:v0.0.4`.

### Step 3 — Run everything

```bash
bash setup-demo.sh
```

This single command:
1. Verifies `oc login` to your cluster
2. Installs the OpenShift GitOps operator if not present
3. Deploys 5 broken demo apps to the `default` namespace
4. Creates ArgoCD Application CRs in `openshift-gitops`
5. Skips RAG extraction (already done)
6. Builds and starts the Go service on `:8080`
7. Starts the Python agent on `:8081` (loads embedding model — ~60s first time)
8. Runs `yarn install` and starts the ArgoAI plugin dev server on `:9001`
9. Runs `yarn install` and starts the GitOps plugin on `:9002`
10. Starts the OpenShift Console container on `:9000`

Open **http://localhost:9000** → left sidebar → **GitOps > ArgoAI**

> If the GitOps section doesn't appear, navigate directly to **http://localhost:9000/argoai**

### Step 4 — Diagnose an application

In the ArgoAI UI:
1. Select a broken application from the table
2. Choose your LLM provider (Gemini recommended)
3. Enter your API key
4. Click **Diagnose**
5. Watch the live SSE stream: triage → routing → tool calls → reasoning → structured result

Or via curl:

```bash
curl -s -N -X POST http://localhost:8080/api/v1/diagnose \
  -H "Content-Type: application/json" \
  -d '{"appName":"demo-oomkilled","appNamespace":"openshift-gitops","provider":"gemini","apiKey":"YOUR_GEMINI_KEY"}'
```

---

## Demo Scenarios

Five intentionally broken applications are deployed by `setup-demo.sh`:

| App | Failure Mode | Expected Agent |
|-----|-------------|---------------|
| `demo-oomkilled` | Memory limit 64MB, requests 256MB → OOMKilled | Runtime Analyzer |
| `demo-imagepull` | Nonexistent image tag → ImagePullBackOff | Runtime Analyzer |
| `demo-crashloop` | App exits code 1 (DB connection refused) → CrashLoopBackOff | Runtime Analyzer |
| `demo-missing-config` | References nonexistent ConfigMap → CreateContainerConfigError | Runtime Analyzer |
| `demo-network-issue` | Liveness probe targeting wrong port → probe failures | Network Analyzer |

Check pod states after deployment:

```bash
oc get pods -n default | grep demo-
oc get applications -n openshift-gitops
```

---

## Running Services Individually

If you prefer to run each service in a separate terminal instead of using `setup-demo.sh`:

```bash
# Terminal 1 — Go service
make run-go

# Terminal 2 — Python agent
TRANSFORMERS_CACHE=/tmp/hf_cache \
RAG_INDEX_PATH=./rag_data/vector_db \
uv run python -m agent.main

# Terminal 3 — ArgoAI console plugin
cd console-plugin && yarn install && yarn start

# Terminal 4 — GitOps sidebar plugin
cd gitops-console-plugin && PORT=9002 yarn start --port 9002

# Terminal 5 — OpenShift console container
cd console-plugin && ENABLE_GITOPS_PLUGIN=true ./start-console.sh
```

---

## Deploying Demo Apps Only (No Console)

```bash
bash setup-demo.sh --no-console
```

Then test via curl using the commands in Step 4.

---

## LLM Providers

| Provider | Model | Notes |
|----------|-------|-------|
| `gemini` | `gemini-2.5-flash` | Recommended — best tool calling |
| `openai` | `gpt-4o-mini` | Via LiteLLM |
| `anthropic` | `claude-3-haiku-20240307` | Via LiteLLM |
| `groq` | `llama-3.1-8b-instant` | Free tier, fast |
| `openrouter` | `openai/gpt-oss-20b:free` | Free tier |
| `ollama` | `qwen3:32b` | Local, no API key needed |

Pass your key in the UI or per-request:

```bash
-d '{"appName":"...","appNamespace":"openshift-gitops","provider":"gemini","apiKey":"YOUR_KEY"}'
```

Or configure via Kubernetes secret (for cluster deployment):

```bash
kubectl create secret generic argocd-agent-llm-keys \
  --from-literal=gemini-api-key=YOUR_KEY \
  -n argocd-agent
```

---

## Cluster Deployment (Production)

Deploy both backend services to your cluster using Kustomize:

```bash
# Apply namespace, RBAC, Go service, Python agent, ServiceMonitor
kubectl apply -k config/deploy/

# Create LLM keys secret separately
kubectl apply -f config/deploy/llm-keys-secret.yaml
```

Services are deployed to the `argocd-agent` namespace. Prometheus metrics available at `/metrics` on both services.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GO_SERVICE_URL` | `http://localhost:8080` | Go service URL for Python agent |
| `RAG_INDEX_PATH` | `/rag/vector_db` | Path to FAISS index |
| `ENABLE_RAG` | `true` | Enable/disable RAG tool |
| `DIAGNOSIS_TIMEOUT_SECONDS` | `90` | Max seconds per diagnosis |
| `MAX_TOOL_CALLS` | `3` | Tool call budget per diagnosis (hard cap 5) |
| `MAX_TOOL_RESPONSE_CHARS` | `1200` | Max chars per tool result fed to LLM |
| `LLM_MAX_OUTPUT_TOKENS` | `1024` | Max output tokens per model round |
| `LLM_TEMPERATURE` | `0.2` | Model temperature |
| `TRANSFORMERS_CACHE` | `/.cache` | HuggingFace model cache path |

---

## Project Structure

```
ArgoAI/
├── cmd/server/          # Go HTTP server (K8s API + signal collector)
├── internal/            # Go packages: api, k8s, health, metrics, secrets
├── agent/               # Python FastAPI agent service
│   ├── agents/          # 5 specialist agents + A2A router
│   ├── tools/           # K8s tools + RAG search tool
│   └── rag/             # FAISS retriever + chunker
├── console-plugin/      # OpenShift Console dynamic plugin (React + PatternFly)
├── config/deploy/       # Kustomize manifests for cluster deployment
├── demo/                # 7 broken-app scenarios for testing
├── docs/                # Observability guide
├── Dockerfile.server    # Go service image
├── Dockerfile.agent     # Python agent image
├── Makefile             # Build, run, deploy targets
└── setup-demo.sh        # One-command demo orchestrator
```

---

## How It Works

1. **Signal collection** — Go service gathers ArgoCD app health/sync, Kubernetes warning events (scoped to app-owned resources), pod statuses with container state details, and pre-loads logs from the first unhealthy pod

2. **Routing** — A2A router matches pod state reasons and event reasons against AgentCard trigger lists. Priority: Storage > RBAC > Network > Config > Runtime

3. **Specialist diagnosis** — The selected LLM agent receives all pre-loaded signals and diagnoses using a maximum of 3 tool calls

4. **Semantic caching** — Results are cached for 15 minutes by a fingerprint of health status, event reasons, and cause-specific details (image name, ConfigMap name, etc.)

5. **SSE streaming** — Every step streams to the UI in real time: triage → routing decision → tool calls → tool results → reasoning → structured JSON result

---

## Observability

Both services expose Prometheus metrics. A Grafana dashboard is included at `config/monitoring/`. See [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) for details.

Health checks:
- Go: `GET /api/v1/health`
- Python: `GET /health`
- Liveness: `GET /livez`, `GET /readyz`

---

## Cleanup

```bash
# Stop all demo services (Ctrl+C in the setup-demo.sh terminal)

# Remove demo apps from cluster
oc delete deployment demo-oomkilled demo-imagepull demo-crashloop demo-missing-config demo-network-issue -n default
oc delete application demo-oomkilled demo-imagepull demo-crashloop demo-missing-config demo-network-issue -n openshift-gitops
```
