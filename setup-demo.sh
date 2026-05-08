#!/usr/bin/env bash
set -euo pipefail

# ArgoAI Demo Setup Script
# Automates: cluster verification, GitOps operator check, demo app deployment,
# backend services startup, and console plugin startup.
#
# Usage: ./setup-demo.sh [--no-console]
#   --no-console  Skip console plugin startup (just backend + demo apps)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

NO_CONSOLE=false
[[ "${1:-}" == "--no-console" ]] && NO_CONSOLE=true

GO_PID=""
PYTHON_PID=""
PLUGIN_PID=""
GITOPS_PLUGIN_PID=""
CONSOLE_PID=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${GREEN}[ArgoAI]${NC} $1"; }
warn() { echo -e "${YELLOW}[ArgoAI]${NC} $1"; }
err()  { echo -e "${RED}[ArgoAI]${NC} $1"; }
info() { echo -e "${CYAN}[ArgoAI]${NC} $1"; }

cleanup() {
    log "Shutting down services..."
    for pid in "$GO_PID" "$PYTHON_PID" "$PLUGIN_PID" "$GITOPS_PLUGIN_PID" "$CONSOLE_PID"; do
        if [ -n "${pid:-}" ]; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    wait 2>/dev/null || true
    log "Done."
}
trap cleanup EXIT

# ============================================================
# Step 1: Verify cluster connection
# ============================================================
log "Step 1: Verifying cluster connection..."
if ! oc whoami &>/dev/null; then
    err "Not logged into an OpenShift cluster. Run 'oc login' first."
    exit 1
fi

CLUSTER_USER=$(oc whoami)
CLUSTER_URL=$(oc whoami --show-server)
log "Connected as ${CYAN}${CLUSTER_USER}${NC} to ${CYAN}${CLUSTER_URL}${NC}"

# ============================================================
# Step 2: Check OpenShift GitOps operator
# ============================================================
log "Step 2: Checking OpenShift GitOps operator..."
if ! oc get pods -n openshift-gitops --no-headers 2>/dev/null | grep -q "Running"; then
    warn "GitOps operator not found or not ready. Installing..."
    oc apply -f - <<'GITOPS_SUB'
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: openshift-gitops-operator
  namespace: openshift-operators
spec:
  channel: latest
  installPlanApproval: Automatic
  name: openshift-gitops-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
GITOPS_SUB
    log "Waiting for GitOps pods to start (up to 3 minutes)..."
    for i in $(seq 1 36); do
        if oc get pods -n openshift-gitops --no-headers 2>/dev/null | grep -q "Running"; then
            break
        fi
        sleep 5
        printf "."
    done
    echo ""
fi
log "GitOps operator is running."

# ============================================================
# Step 3: Deploy demo failure scenarios
# ============================================================
log "Step 3: Deploying demo failure scenarios..."
oc apply -f demo/oomkilled/deployment.yaml \
         -f demo/imagepull/deployment.yaml \
         -f demo/crashloop/deployment.yaml \
         -f demo/missing-config/deployment.yaml \
         -f demo/network-issue/deployment.yaml 2>&1 | sed 's/^/  /'

# Create ArgoCD Application CRs
log "Creating ArgoCD Application CRs..."
for app in demo-oomkilled demo-imagepull demo-crashloop demo-missing-config demo-network-issue; do
    path="${app#demo-}"
    cat <<APPEOF | oc apply -f - 2>/dev/null
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ${app}
  namespace: openshift-gitops
spec:
  project: default
  destination:
    server: https://kubernetes.default.svc
    namespace: default
  source:
    repoURL: https://github.com/tzprograms/ArgoAI---Demo
    path: ${path}
    targetRevision: HEAD
APPEOF
    echo "  application.argoproj.io/${app} created"
done

log "Waiting 30s for failures to manifest..."
sleep 30
info "Pod statuses:"
oc get pods -n default --no-headers 2>&1 | grep demo- | sed 's/^/  /'

# ============================================================
# Step 4: Extract RAG data (if not present)
# ============================================================
if [ ! -d "rag_data/vector_db" ]; then
    log "Step 4: Extracting RAG data..."
    mkdir -p rag_data
    if command -v docker >/dev/null 2>&1; then
        CONTAINER_RUNTIME=${CONTAINER_RUNTIME:-docker}
    elif command -v podman >/dev/null 2>&1; then
        CONTAINER_RUNTIME=${CONTAINER_RUNTIME:-podman}
    else
        err "Neither docker nor podman is available for RAG extraction."
        exit 1
    fi
    "${CONTAINER_RUNTIME}" run --rm -v "${SCRIPT_DIR}/rag_data:/out" "${RAG_IMAGE:-quay.io/devtools_gitops/argocd_lightspeed_byok:v0.0.4}" cp -r /rag/vector_db /out/
else
    log "Step 4: RAG data already present, skipping extraction."
fi

# ============================================================
# Step 5: Build and start Go service
# ============================================================
log "Step 5: Building Go service..."
go build -o bin/go-service ./cmd/server 2>&1 | sed 's/^/  /'

log "Starting Go service on :8080..."
./bin/go-service --addr=:8080 --agent-url=http://localhost:8081 &
GO_PID=$!
sleep 2

if curl -s http://localhost:8080/api/v1/health | grep -q "ok"; then
    log "Go service is healthy."
else
    err "Go service failed to start."
    exit 1
fi

# ============================================================
# Step 6: Start Python agent service
# ============================================================
log "Step 6: Starting Python agent service on :8081..."
TRANSFORMERS_CACHE=/tmp/hf_cache \
HF_HOME=/tmp/hf_cache \
SENTENCE_TRANSFORMERS_HOME=/tmp/hf_cache \
RAG_INDEX_PATH=./rag_data/vector_db \
uv run python -m agent.main &
PYTHON_PID=$!

log "Waiting for Python service to start (loading embedding model)..."
for i in $(seq 1 60); do
    if curl -s http://localhost:8081/health 2>/dev/null | grep -q "ok"; then
        break
    fi
    sleep 2
    printf "."
done
echo ""

if curl -s http://localhost:8081/health | grep -q "ok"; then
    PYTHON_JSON_BIN="${PYTHON_JSON_BIN:-python3}"
    if ! command -v "$PYTHON_JSON_BIN" >/dev/null 2>&1; then
        PYTHON_JSON_BIN="python"
    fi
    RAG_STATUS=$(curl -s http://localhost:8081/health | "$PYTHON_JSON_BIN" -c "import sys,json; print(json.load(sys.stdin).get('rag_loaded', False))" 2>/dev/null || echo "unknown")
    log "Python service is healthy. RAG loaded: ${RAG_STATUS}"
else
    err "Python service failed to start."
    exit 1
fi

# ============================================================
# Step 7: Console plugin (optional)
# ============================================================
if [ "$NO_CONSOLE" = false ]; then
    log "Step 7: Starting console plugins..."

    # Install deps if needed
    if [ ! -d "console-plugin/node_modules" ]; then
        (cd console-plugin && yarn install 2>&1 | tail -1)
    fi
    if [ -d "gitops-console-plugin" ] && [ ! -d "gitops-console-plugin/node_modules" ]; then
        (cd gitops-console-plugin && yarn install 2>&1 | tail -1)
    fi

    # Start ArgoAI plugin on 9001
    (cd console-plugin && yarn start 2>/dev/null) &
    PLUGIN_PID=$!

    if [ -d "gitops-console-plugin" ]; then
        # Start GitOps plugin on 9002 when the optional plugin checkout is present.
        (cd gitops-console-plugin && PORT=9002 yarn start --port 9002 2>/dev/null) &
        GITOPS_PLUGIN_PID=$!
    else
        warn "gitops-console-plugin directory not found; starting only the ArgoAI console plugin."
    fi

    log "Waiting for plugin servers to compile..."
    sleep 15

    # Start console container
    log "Starting OpenShift Console container on :9000..."
    (cd console-plugin && ./start-console.sh 2>&1) &
    CONSOLE_PID=$!

    sleep 5
    info "Console UI: ${CYAN}http://localhost:9000${NC}"
else
    log "Step 7: Skipping console (--no-console flag)."
fi

# ============================================================
# Summary
# ============================================================
echo ""
echo "============================================"
log "ArgoAI Demo Environment Ready!"
echo "============================================"
echo ""
info "Backend API:     http://localhost:8080/api/v1/health"
info "Agent Service:   http://localhost:8081/health"
info "Agent List:      http://localhost:8081/agents"
if [ "$NO_CONSOLE" = false ]; then
    info "Console UI:      http://localhost:9000"
fi
echo ""
info "Test diagnosis:"
echo '  curl -s -N -X POST http://localhost:8080/api/v1/diagnose \'
echo '    -H "Content-Type: application/json" \'
echo '    -d '"'"'{"appName":"demo-oomkilled","appNamespace":"openshift-gitops","provider":"groq","apiKey":"YOUR_GROQ_KEY"}'"'"''
echo ""
log "Press Ctrl+C to stop all services."

# Wait for all background processes
wait
