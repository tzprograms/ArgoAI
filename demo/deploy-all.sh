#!/bin/bash
# Deploy all broken demo apps to test ArgoAgent's 5 specialist agents

set -e

echo "=========================================="
echo "Deploying Demo Apps for ArgoAgent Testing"
echo "=========================================="
echo ""

# Check if connected to cluster
if ! oc whoami &>/dev/null; then
    echo "ERROR: Not logged into OpenShift cluster. Run 'oc login' first."
    exit 1
fi

echo "Cluster: $(oc whoami --show-server)"
echo "User: $(oc whoami)"
echo ""

# Ensure we're in default namespace (or create a demo namespace)
NAMESPACE="${DEMO_NAMESPACE:-default}"
echo "Deploying to namespace: $NAMESPACE"
echo ""

# Deploy each broken app
echo "1. Deploying OOMKilled demo (triggers Runtime Analyzer)..."
oc apply -f oomkilled/deployment.yaml -n $NAMESPACE
echo "   -> Container requests 256MB but limited to 64MB - will be OOMKilled"
echo ""

echo "2. Deploying ImagePullBackOff demo (triggers Runtime Analyzer)..."
oc apply -f imagepull/deployment.yaml -n $NAMESPACE
echo "   -> Uses nonexistent image tag - will fail to pull"
echo ""

echo "3. Deploying Missing ConfigMap demo (triggers Runtime Analyzer)..."
oc apply -f missing-config/deployment.yaml -n $NAMESPACE
echo "   -> References nonexistent ConfigMap - CreateContainerConfigError"
echo ""

echo "4. Deploying CrashLoop demo (triggers Runtime Analyzer)..."
oc apply -f crashloop/deployment.yaml -n $NAMESPACE
echo "   -> App exits with error - CrashLoopBackOff"
echo ""

echo "5. Deploying Network Issue demo (triggers Network Analyzer)..."
oc apply -f network-issue/deployment.yaml -n $NAMESPACE
echo "   -> Tries to connect to nonexistent service, health probes fail"
echo ""

echo "6. Deploying Storage Issue demo (triggers Storage Analyzer)..."
oc apply -f storage-issue/deployment.yaml -n $NAMESPACE
echo "   -> PVC references nonexistent StorageClass - stays Pending"
echo ""

echo "7. Deploying RBAC Issue demo (triggers RBAC Analyzer)..."
oc apply -f rbac-issue/deployment.yaml -n $NAMESPACE
echo "   -> ServiceAccount lacks permissions - Forbidden errors"
echo ""

echo "=========================================="
echo "All demo apps deployed!"
echo "=========================================="
echo ""
echo "Wait 30-60 seconds for failures to manifest, then check status:"
echo ""
echo "  oc get pods -n $NAMESPACE"
echo "  oc get events -n $NAMESPACE --sort-by='.lastTimestamp' | tail -30"
echo ""
echo "Expected states:"
echo "  - demo-oomkilled:       OOMKilled / CrashLoopBackOff"
echo "  - demo-imagepull:       ImagePullBackOff"
echo "  - demo-missing-config:  CreateContainerConfigError"
echo "  - demo-crashloop:       CrashLoopBackOff"
echo "  - demo-network-issue:   Running but Unhealthy (probe failures)"
echo "  - demo-storage-issue:   Pending (PVC not bound)"
echo "  - demo-rbac-issue:      Running (but logs show Forbidden errors)"
echo ""
