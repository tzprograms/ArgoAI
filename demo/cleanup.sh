#!/bin/bash
# Clean up all demo apps

NAMESPACE="${DEMO_NAMESPACE:-default}"

echo "Cleaning up demo apps from namespace: $NAMESPACE"
echo ""

oc delete deployment demo-oomkilled -n $NAMESPACE --ignore-not-found
oc delete deployment demo-imagepull -n $NAMESPACE --ignore-not-found
oc delete deployment demo-missing-config -n $NAMESPACE --ignore-not-found
oc delete deployment demo-crashloop -n $NAMESPACE --ignore-not-found
oc delete deployment demo-network-issue -n $NAMESPACE --ignore-not-found
oc delete deployment demo-storage-issue -n $NAMESPACE --ignore-not-found
oc delete deployment demo-rbac-issue -n $NAMESPACE --ignore-not-found
oc delete pvc demo-nonexistent-pvc -n $NAMESPACE --ignore-not-found
oc delete sa demo-restricted-sa -n $NAMESPACE --ignore-not-found

echo ""
echo "Cleanup complete!"
