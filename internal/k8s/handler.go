package k8s

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
)

var applicationGVR = schema.GroupVersionResource{
	Group: "argoproj.io", Version: "v1alpha1", Resource: "applications",
}

// kindToGVR maps resource kinds to their GroupVersionResource.
// SECURITY: Secret is explicitly excluded to prevent leaking cluster secrets.
// For secret-related diagnostics, rely on events which expose safe metadata.
var kindToGVR = map[string]schema.GroupVersionResource{
	// Core workload resources
	"pod":         {Version: "v1", Resource: "pods"},
	"deployment":  {Group: "apps", Version: "v1", Resource: "deployments"},
	"statefulset": {Group: "apps", Version: "v1", Resource: "statefulsets"},
	"replicaset":  {Group: "apps", Version: "v1", Resource: "replicasets"},
	"daemonset":   {Group: "apps", Version: "v1", Resource: "daemonsets"},

	// Config resources (ConfigMap only - NOT secrets)
	"configmap": {Version: "v1", Resource: "configmaps"},

	// Network resources
	"service":       {Version: "v1", Resource: "services"},
	"ingress":       {Group: "networking.k8s.io", Version: "v1", Resource: "ingresses"},
	"networkpolicy": {Group: "networking.k8s.io", Version: "v1", Resource: "networkpolicies"},

	// Storage resources
	"persistentvolumeclaim": {Version: "v1", Resource: "persistentvolumeclaims"},
	"persistentvolume":      {Version: "v1", Resource: "persistentvolumes"},
	"storageclass":          {Group: "storage.k8s.io", Version: "v1", Resource: "storageclasses"},

	// RBAC resources
	"serviceaccount":     {Version: "v1", Resource: "serviceaccounts"},
	"role":               {Group: "rbac.authorization.k8s.io", Version: "v1", Resource: "roles"},
	"rolebinding":        {Group: "rbac.authorization.k8s.io", Version: "v1", Resource: "rolebindings"},
	"clusterrole":        {Group: "rbac.authorization.k8s.io", Version: "v1", Resource: "clusterroles"},
	"clusterrolebinding": {Group: "rbac.authorization.k8s.io", Version: "v1", Resource: "clusterrolebindings"},

	// Scaling resources
	"horizontalpodautoscaler": {Group: "autoscaling", Version: "v2", Resource: "horizontalpodautoscalers"},
	"resourcequota":           {Version: "v1", Resource: "resourcequotas"},

	// OpenShift Route (if available)
	"route": {Group: "route.openshift.io", Version: "v1", Resource: "routes"},
}

// blockedKinds are explicitly rejected for security reasons
var blockedKinds = map[string]bool{
	"secret": true,
}

// Handler exposes internal K8s data endpoints for the Python agent service.
type Handler struct {
	clientset kubernetes.Interface
	dynClient dynamic.Interface
}

func NewHandler(cs kubernetes.Interface, dyn dynamic.Interface) *Handler {
	return &Handler{clientset: cs, dynClient: dyn}
}

// RegisterRoutes registers internal K8s API routes on the given mux.
func (h *Handler) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /internal/k8s/pod-logs", h.handlePodLogs)
	mux.HandleFunc("POST /internal/k8s/events", h.handleEvents)
	mux.HandleFunc("POST /internal/k8s/resource", h.handleResource)
	mux.HandleFunc("POST /internal/k8s/pods", h.handleListPods)
	mux.HandleFunc("POST /internal/argocd/app", h.handleArgoCDApp)
	mux.HandleFunc("POST /internal/argocd/diff", h.handleArgoCDDiff)
}

// --- Pod Logs ---

func (h *Handler) handlePodLogs(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Namespace string `json:"namespace"`
		Pod       string `json:"pod"`
		Container string `json:"container"`
		Tail      int64  `json:"tail"`
		Previous  bool   `json:"previous"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	if req.Tail <= 0 {
		req.Tail = 100
	}

	opts := &corev1.PodLogOptions{TailLines: &req.Tail, Previous: req.Previous}
	if req.Container != "" {
		opts.Container = req.Container
	}

	stream, err := h.clientset.CoreV1().Pods(req.Namespace).GetLogs(req.Pod, opts).Stream(r.Context())
	if err != nil {
		jsonResp(w, map[string]string{"logs": fmt.Sprintf("Error: %v", err)})
		return
	}
	defer stream.Close()

	logBytes, _ := io.ReadAll(io.LimitReader(stream, 32*1024))
	jsonResp(w, map[string]string{"logs": string(logBytes)})
}

// --- Events ---

func (h *Handler) handleEvents(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Namespace    string `json:"namespace"`
		ResourceKind string `json:"resourceKind"`
		ResourceName string `json:"resourceName"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	events, err := h.clientset.CoreV1().Events(req.Namespace).List(r.Context(), metav1.ListOptions{})
	if err != nil {
		jsonResp(w, map[string]string{"events": fmt.Sprintf("Error: %v", err)})
		return
	}

	var filtered []map[string]any
	for _, e := range events.Items {
		if req.ResourceKind != "" && !strings.EqualFold(e.InvolvedObject.Kind, req.ResourceKind) {
			continue
		}
		if req.ResourceName != "" && e.InvolvedObject.Name != req.ResourceName {
			continue
		}
		filtered = append(filtered, map[string]any{
			"type":    e.Type,
			"reason":  e.Reason,
			"message": e.Message,
			"object":  fmt.Sprintf("%s/%s", e.InvolvedObject.Kind, e.InvolvedObject.Name),
			"count":   e.Count,
		})
	}
	if len(filtered) > 30 {
		filtered = filtered[len(filtered)-30:]
	}
	jsonResp(w, map[string]any{"events": filtered})
}

// --- Get Resource ---

func (h *Handler) handleResource(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Kind      string `json:"kind"`
		Namespace string `json:"namespace"`
		Name      string `json:"name"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	kindLower := strings.ToLower(req.Kind)

	// SECURITY: Block access to secrets
	if blockedKinds[kindLower] {
		w.WriteHeader(http.StatusForbidden)
		jsonResp(w, map[string]string{"error": fmt.Sprintf("Access to %s is not allowed for security reasons", req.Kind)})
		return
	}

	gvr, ok := kindToGVR[kindLower]
	if !ok {
		kinds := make([]string, 0, len(kindToGVR))
		for k := range kindToGVR {
			kinds = append(kinds, k)
		}
		jsonResp(w, map[string]string{"error": fmt.Sprintf("Unknown kind: %s. Supported: %s", req.Kind, strings.Join(kinds, ", "))})
		return
	}

	// Handle cluster-scoped resources
	var obj *unstructured.Unstructured
	var err error
	if isClusterScoped(kindLower) {
		obj, err = h.dynClient.Resource(gvr).Get(r.Context(), req.Name, metav1.GetOptions{})
	} else {
		obj, err = h.dynClient.Resource(gvr).Namespace(req.Namespace).Get(r.Context(), req.Name, metav1.GetOptions{})
	}
	if err != nil {
		jsonResp(w, map[string]string{"error": fmt.Sprintf("Error: %v", err)})
		return
	}

	// Return diagnostic summary based on resource kind
	summary := buildResourceSummary(kindLower, obj)
	jsonResp(w, summary)
}

// isClusterScoped returns true for cluster-scoped resources
func isClusterScoped(kind string) bool {
	clusterScoped := map[string]bool{
		"persistentvolume":      true,
		"storageclass":          true,
		"clusterrole":           true,
		"clusterrolebinding":    true,
	}
	return clusterScoped[kind]
}

// buildResourceSummary returns a compact diagnostic summary for a resource
func buildResourceSummary(kind string, obj *unstructured.Unstructured) map[string]any {
	// Remove noisy fields
	unstructured.RemoveNestedField(obj.Object, "metadata", "managedFields")
	unstructured.RemoveNestedField(obj.Object, "metadata", "annotations", "kubectl.kubernetes.io/last-applied-configuration")

	summary := map[string]any{
		"kind":      obj.GetKind(),
		"name":      obj.GetName(),
		"namespace": obj.GetNamespace(),
	}

	switch kind {
	case "deployment", "statefulset", "daemonset", "replicaset":
		summary = buildWorkloadSummary(obj, summary)
	case "pod":
		summary = buildPodSummary(obj, summary)
	case "service":
		summary = buildServiceSummary(obj, summary)
	case "ingress":
		summary = buildIngressSummary(obj, summary)
	case "persistentvolumeclaim":
		summary = buildPVCSummary(obj, summary)
	case "persistentvolume":
		summary = buildPVSummary(obj, summary)
	case "storageclass":
		summary = buildStorageClassSummary(obj, summary)
	case "serviceaccount":
		summary = buildServiceAccountSummary(obj, summary)
	case "role", "clusterrole":
		summary = buildRoleSummary(obj, summary)
	case "rolebinding", "clusterrolebinding":
		summary = buildRoleBindingSummary(obj, summary)
	case "networkpolicy":
		summary = buildNetworkPolicySummary(obj, summary)
	case "horizontalpodautoscaler":
		summary = buildHPASummary(obj, summary)
	case "configmap":
		summary = buildConfigMapSummary(obj, summary)
	case "route":
		summary = buildRouteSummary(obj, summary)
	default:
		// For unknown kinds, return spec/status if available
		if spec, ok, _ := unstructured.NestedMap(obj.Object, "spec"); ok {
			summary["spec"] = spec
		}
		if status, ok, _ := unstructured.NestedMap(obj.Object, "status"); ok {
			summary["status"] = status
		}
	}

	return summary
}

// buildWorkloadSummary extracts diagnostic info from Deployment/StatefulSet/etc
func buildWorkloadSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	// Get replicas info
	replicas, _, _ := unstructured.NestedInt64(obj.Object, "spec", "replicas")
	readyReplicas, _, _ := unstructured.NestedInt64(obj.Object, "status", "readyReplicas")
	summary["replicas"] = map[string]int64{"desired": replicas, "ready": readyReplicas}

	// Get container info
	containers, _, _ := unstructured.NestedSlice(obj.Object, "spec", "template", "spec", "containers")
	containerSummaries := []map[string]any{}
	for _, c := range containers {
		cm, ok := c.(map[string]any)
		if !ok {
			continue
		}
		cs := map[string]any{
			"name":  cm["name"],
			"image": cm["image"],
		}
		if res, ok := cm["resources"].(map[string]any); ok {
			cs["resources"] = res
		}
		if envFrom, ok := cm["envFrom"].([]any); ok {
			cs["envFrom_count"] = len(envFrom)
		}
		if env, ok := cm["env"].([]any); ok {
			// Count env vars that reference secrets/configmaps
			secretRefs := 0
			configMapRefs := 0
			for _, e := range env {
				em, ok := e.(map[string]any)
				if !ok {
					continue
				}
				if vf, ok := em["valueFrom"].(map[string]any); ok {
					if _, ok := vf["secretKeyRef"]; ok {
						secretRefs++
					}
					if _, ok := vf["configMapKeyRef"]; ok {
						configMapRefs++
					}
				}
			}
			cs["env_count"] = len(env)
			cs["env_secret_refs"] = secretRefs
			cs["env_configmap_refs"] = configMapRefs
		}
		if vms, ok := cm["volumeMounts"].([]any); ok {
			cs["volumeMounts_count"] = len(vms)
		}
		if lp, ok := cm["livenessProbe"].(map[string]any); ok {
			cs["has_liveness_probe"] = true
			_ = lp
		}
		if rp, ok := cm["readinessProbe"].(map[string]any); ok {
			cs["has_readiness_probe"] = true
			_ = rp
		}
		containerSummaries = append(containerSummaries, cs)
	}
	summary["containers"] = containerSummaries

	// Get serviceAccountName
	saName, _, _ := unstructured.NestedString(obj.Object, "spec", "template", "spec", "serviceAccountName")
	if saName != "" {
		summary["serviceAccountName"] = saName
	}

	// Get volumes (names and types only)
	volumes, _, _ := unstructured.NestedSlice(obj.Object, "spec", "template", "spec", "volumes")
	volSummaries := []map[string]any{}
	for _, v := range volumes {
		vm, ok := v.(map[string]any)
		if !ok {
			continue
		}
		vs := map[string]any{"name": vm["name"]}
		// Identify volume type
		for _, vtype := range []string{"configMap", "secret", "persistentVolumeClaim", "emptyDir", "hostPath"} {
			if vt, ok := vm[vtype].(map[string]any); ok {
				vs["type"] = vtype
				if name, ok := vt["name"].(string); ok {
					vs["ref"] = name
				}
				if claimName, ok := vt["claimName"].(string); ok {
					vs["ref"] = claimName
				}
				break
			}
		}
		volSummaries = append(volSummaries, vs)
	}
	if len(volSummaries) > 0 {
		summary["volumes"] = volSummaries
	}

	// Get conditions
	conditions, _, _ := unstructured.NestedSlice(obj.Object, "status", "conditions")
	condSummaries := []map[string]any{}
	for _, c := range conditions {
		cm, ok := c.(map[string]any)
		if !ok {
			continue
		}
		condSummaries = append(condSummaries, map[string]any{
			"type":    cm["type"],
			"status":  cm["status"],
			"reason":  cm["reason"],
			"message": cm["message"],
		})
	}
	if len(condSummaries) > 0 {
		summary["conditions"] = condSummaries
	}

	return summary
}

// buildPodSummary extracts diagnostic info from Pod
func buildPodSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	phase, _, _ := unstructured.NestedString(obj.Object, "status", "phase")
	summary["phase"] = phase

	// Container statuses
	containerStatuses, _, _ := unstructured.NestedSlice(obj.Object, "status", "containerStatuses")
	csSummaries := []map[string]any{}
	for _, cs := range containerStatuses {
		csm, ok := cs.(map[string]any)
		if !ok {
			continue
		}
		csSummary := map[string]any{
			"name":         csm["name"],
			"ready":        csm["ready"],
			"restartCount": csm["restartCount"],
		}
		if state, ok := csm["state"].(map[string]any); ok {
			for stateType, stateData := range state {
				csSummary["state"] = stateType
				if sd, ok := stateData.(map[string]any); ok {
					if reason, ok := sd["reason"]; ok {
						csSummary["reason"] = reason
					}
					if msg, ok := sd["message"]; ok {
						msgStr := fmt.Sprintf("%v", msg)
						if len(msgStr) > 200 {
							msgStr = msgStr[:200]
						}
						csSummary["message"] = msgStr
					}
					if exitCode, ok := sd["exitCode"]; ok {
						csSummary["exitCode"] = exitCode
					}
				}
				break
			}
		}
		if lastState, ok := csm["lastState"].(map[string]any); ok {
			for stateType, stateData := range lastState {
				csSummary["lastState"] = stateType
				if sd, ok := stateData.(map[string]any); ok {
					if reason, ok := sd["reason"]; ok {
						csSummary["lastReason"] = reason
					}
					if exitCode, ok := sd["exitCode"]; ok {
						csSummary["lastExitCode"] = exitCode
					}
				}
				break
			}
		}
		csSummaries = append(csSummaries, csSummary)
	}
	summary["containerStatuses"] = csSummaries

	// Conditions
	conditions, _, _ := unstructured.NestedSlice(obj.Object, "status", "conditions")
	condSummaries := []map[string]any{}
	for _, c := range conditions {
		cm, ok := c.(map[string]any)
		if !ok {
			continue
		}
		condSummaries = append(condSummaries, map[string]any{
			"type":    cm["type"],
			"status":  cm["status"],
			"reason":  cm["reason"],
			"message": cm["message"],
		})
	}
	summary["conditions"] = condSummaries

	return summary
}

// buildServiceSummary extracts diagnostic info from Service
func buildServiceSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	stype, _, _ := unstructured.NestedString(obj.Object, "spec", "type")
	summary["type"] = stype

	clusterIP, _, _ := unstructured.NestedString(obj.Object, "spec", "clusterIP")
	summary["clusterIP"] = clusterIP

	ports, _, _ := unstructured.NestedSlice(obj.Object, "spec", "ports")
	portSummaries := []map[string]any{}
	for _, p := range ports {
		pm, ok := p.(map[string]any)
		if !ok {
			continue
		}
		portSummaries = append(portSummaries, map[string]any{
			"name":       pm["name"],
			"port":       pm["port"],
			"targetPort": pm["targetPort"],
			"protocol":   pm["protocol"],
		})
	}
	summary["ports"] = portSummaries

	selector, _, _ := unstructured.NestedStringMap(obj.Object, "spec", "selector")
	summary["selector"] = selector

	return summary
}

// buildIngressSummary extracts diagnostic info from Ingress
func buildIngressSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	ingressClassName, _, _ := unstructured.NestedString(obj.Object, "spec", "ingressClassName")
	summary["ingressClassName"] = ingressClassName

	rules, _, _ := unstructured.NestedSlice(obj.Object, "spec", "rules")
	ruleSummaries := []map[string]any{}
	for _, r := range rules {
		rm, ok := r.(map[string]any)
		if !ok {
			continue
		}
		host, _ := rm["host"].(string)
		rs := map[string]any{"host": host}

		if http, ok := rm["http"].(map[string]any); ok {
			if paths, ok := http["paths"].([]any); ok {
				pathStrs := []string{}
				for _, p := range paths {
					if pm, ok := p.(map[string]any); ok {
						if path, ok := pm["path"].(string); ok {
							pathStrs = append(pathStrs, path)
						}
					}
				}
				rs["paths"] = pathStrs
			}
		}
		ruleSummaries = append(ruleSummaries, rs)
	}
	summary["rules"] = ruleSummaries

	tls, _, _ := unstructured.NestedSlice(obj.Object, "spec", "tls")
	if len(tls) > 0 {
		summary["tls_configured"] = true
	}

	return summary
}

// buildPVCSummary extracts diagnostic info from PersistentVolumeClaim
func buildPVCSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	phase, _, _ := unstructured.NestedString(obj.Object, "status", "phase")
	summary["phase"] = phase

	storageClassName, _, _ := unstructured.NestedString(obj.Object, "spec", "storageClassName")
	summary["storageClassName"] = storageClassName

	accessModes, _, _ := unstructured.NestedStringSlice(obj.Object, "spec", "accessModes")
	summary["accessModes"] = accessModes

	storage, _, _ := unstructured.NestedString(obj.Object, "spec", "resources", "requests", "storage")
	summary["requestedStorage"] = storage

	volumeName, _, _ := unstructured.NestedString(obj.Object, "spec", "volumeName")
	summary["volumeName"] = volumeName

	return summary
}

// buildPVSummary extracts diagnostic info from PersistentVolume
func buildPVSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	phase, _, _ := unstructured.NestedString(obj.Object, "status", "phase")
	summary["phase"] = phase

	storageClassName, _, _ := unstructured.NestedString(obj.Object, "spec", "storageClassName")
	summary["storageClassName"] = storageClassName

	capacity, _, _ := unstructured.NestedString(obj.Object, "spec", "capacity", "storage")
	summary["capacity"] = capacity

	accessModes, _, _ := unstructured.NestedStringSlice(obj.Object, "spec", "accessModes")
	summary["accessModes"] = accessModes

	reclaimPolicy, _, _ := unstructured.NestedString(obj.Object, "spec", "persistentVolumeReclaimPolicy")
	summary["reclaimPolicy"] = reclaimPolicy

	return summary
}

// buildStorageClassSummary extracts diagnostic info from StorageClass
func buildStorageClassSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	provisioner, _, _ := unstructured.NestedString(obj.Object, "provisioner")
	summary["provisioner"] = provisioner

	reclaimPolicy, _, _ := unstructured.NestedString(obj.Object, "reclaimPolicy")
	summary["reclaimPolicy"] = reclaimPolicy

	volumeBindingMode, _, _ := unstructured.NestedString(obj.Object, "volumeBindingMode")
	summary["volumeBindingMode"] = volumeBindingMode

	allowVolumeExpansion, _, _ := unstructured.NestedBool(obj.Object, "allowVolumeExpansion")
	summary["allowVolumeExpansion"] = allowVolumeExpansion

	return summary
}

// buildServiceAccountSummary extracts diagnostic info from ServiceAccount
func buildServiceAccountSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	secrets, _, _ := unstructured.NestedSlice(obj.Object, "secrets")
	summary["secrets_count"] = len(secrets)

	imagePullSecrets, _, _ := unstructured.NestedSlice(obj.Object, "imagePullSecrets")
	summary["imagePullSecrets_count"] = len(imagePullSecrets)

	automountToken, ok, _ := unstructured.NestedBool(obj.Object, "automountServiceAccountToken")
	if ok {
		summary["automountServiceAccountToken"] = automountToken
	}

	return summary
}

// buildRoleSummary extracts diagnostic info from Role/ClusterRole
func buildRoleSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	rules, _, _ := unstructured.NestedSlice(obj.Object, "rules")
	ruleSummaries := []map[string]any{}
	for _, r := range rules {
		rm, ok := r.(map[string]any)
		if !ok {
			continue
		}
		ruleSummaries = append(ruleSummaries, map[string]any{
			"apiGroups":     rm["apiGroups"],
			"resources":     rm["resources"],
			"verbs":         rm["verbs"],
			"resourceNames": rm["resourceNames"],
		})
	}
	summary["rules"] = ruleSummaries
	summary["rules_count"] = len(rules)

	return summary
}

// buildRoleBindingSummary extracts diagnostic info from RoleBinding/ClusterRoleBinding
func buildRoleBindingSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	roleRef, _, _ := unstructured.NestedMap(obj.Object, "roleRef")
	if roleRef != nil {
		summary["roleRef"] = map[string]any{
			"kind": roleRef["kind"],
			"name": roleRef["name"],
		}
	}

	subjects, _, _ := unstructured.NestedSlice(obj.Object, "subjects")
	subjectSummaries := []map[string]any{}
	for _, s := range subjects {
		sm, ok := s.(map[string]any)
		if !ok {
			continue
		}
		subjectSummaries = append(subjectSummaries, map[string]any{
			"kind":      sm["kind"],
			"name":      sm["name"],
			"namespace": sm["namespace"],
		})
	}
	summary["subjects"] = subjectSummaries

	return summary
}

// buildNetworkPolicySummary extracts diagnostic info from NetworkPolicy
func buildNetworkPolicySummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	policyTypes, _, _ := unstructured.NestedStringSlice(obj.Object, "spec", "policyTypes")
	summary["policyTypes"] = policyTypes

	podSelector, _, _ := unstructured.NestedMap(obj.Object, "spec", "podSelector")
	if matchLabels, ok := podSelector["matchLabels"].(map[string]any); ok {
		summary["podSelector"] = matchLabels
	}

	ingress, _, _ := unstructured.NestedSlice(obj.Object, "spec", "ingress")
	summary["ingress_rules_count"] = len(ingress)

	egress, _, _ := unstructured.NestedSlice(obj.Object, "spec", "egress")
	summary["egress_rules_count"] = len(egress)

	return summary
}

// buildHPASummary extracts diagnostic info from HorizontalPodAutoscaler
func buildHPASummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	minReplicas, _, _ := unstructured.NestedInt64(obj.Object, "spec", "minReplicas")
	maxReplicas, _, _ := unstructured.NestedInt64(obj.Object, "spec", "maxReplicas")
	currentReplicas, _, _ := unstructured.NestedInt64(obj.Object, "status", "currentReplicas")
	desiredReplicas, _, _ := unstructured.NestedInt64(obj.Object, "status", "desiredReplicas")

	summary["minReplicas"] = minReplicas
	summary["maxReplicas"] = maxReplicas
	summary["currentReplicas"] = currentReplicas
	summary["desiredReplicas"] = desiredReplicas

	scaleTargetRef, _, _ := unstructured.NestedMap(obj.Object, "spec", "scaleTargetRef")
	if scaleTargetRef != nil {
		summary["scaleTargetRef"] = map[string]any{
			"kind": scaleTargetRef["kind"],
			"name": scaleTargetRef["name"],
		}
	}

	conditions, _, _ := unstructured.NestedSlice(obj.Object, "status", "conditions")
	condSummaries := []map[string]any{}
	for _, c := range conditions {
		cm, ok := c.(map[string]any)
		if !ok {
			continue
		}
		condSummaries = append(condSummaries, map[string]any{
			"type":   cm["type"],
			"status": cm["status"],
			"reason": cm["reason"],
		})
	}
	summary["conditions"] = condSummaries

	return summary
}

// buildConfigMapSummary extracts diagnostic info from ConfigMap (keys only, not values)
func buildConfigMapSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	data, _, _ := unstructured.NestedStringMap(obj.Object, "data")
	keys := make([]string, 0, len(data))
	for k := range data {
		keys = append(keys, k)
	}
	summary["keys"] = keys
	summary["keys_count"] = len(keys)

	return summary
}

// buildRouteSummary extracts diagnostic info from OpenShift Route
func buildRouteSummary(obj *unstructured.Unstructured, summary map[string]any) map[string]any {
	host, _, _ := unstructured.NestedString(obj.Object, "spec", "host")
	summary["host"] = host

	path, _, _ := unstructured.NestedString(obj.Object, "spec", "path")
	summary["path"] = path

	toService, _, _ := unstructured.NestedString(obj.Object, "spec", "to", "name")
	summary["targetService"] = toService

	tls, _, _ := unstructured.NestedMap(obj.Object, "spec", "tls")
	if tls != nil {
		summary["tls_termination"] = tls["termination"]
	}

	// Ingress status
	ingress, _, _ := unstructured.NestedSlice(obj.Object, "status", "ingress")
	if len(ingress) > 0 {
		if ing, ok := ingress[0].(map[string]any); ok {
			conditions, _, _ := unstructured.NestedSlice(ing, "conditions")
			condSummaries := []map[string]any{}
			for _, c := range conditions {
				cm, ok := c.(map[string]any)
				if !ok {
					continue
				}
				condSummaries = append(condSummaries, map[string]any{
					"type":   cm["type"],
					"status": cm["status"],
				})
			}
			summary["ingress_conditions"] = condSummaries
		}
	}

	return summary
}

// --- List Pods ---

func (h *Handler) handleListPods(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Namespace     string `json:"namespace"`
		LabelSelector string `json:"labelSelector"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	pods, err := h.clientset.CoreV1().Pods(req.Namespace).List(r.Context(), metav1.ListOptions{
		LabelSelector: req.LabelSelector,
	})
	if err != nil {
		jsonResp(w, map[string]string{"error": fmt.Sprintf("Error: %v", err)})
		return
	}

	var result []map[string]any
	for _, pod := range pods.Items {
		ready := 0
		total := len(pod.Status.ContainerStatuses)
		var restarts int32
		for _, cs := range pod.Status.ContainerStatuses {
			if cs.Ready {
				ready++
			}
			restarts += cs.RestartCount
		}
		result = append(result, map[string]any{
			"name":     pod.Name,
			"status":   string(pod.Status.Phase),
			"ready":    fmt.Sprintf("%d/%d", ready, total),
			"restarts": restarts,
		})
	}
	jsonResp(w, map[string]any{"pods": result})
}

// --- ArgoCD App ---

func (h *Handler) handleArgoCDApp(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name      string `json:"name"`
		Namespace string `json:"namespace"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	app, err := h.dynClient.Resource(applicationGVR).Namespace(req.Namespace).Get(r.Context(), req.Name, metav1.GetOptions{})
	if err != nil {
		jsonResp(w, map[string]string{"error": fmt.Sprintf("Error: %v", err)})
		return
	}

	status, _, _ := unstructured.NestedMap(app.Object, "status")
	spec, _, _ := unstructured.NestedMap(app.Object, "spec")

	summary := map[string]any{"name": req.Name, "namespace": req.Namespace}
	if spec != nil {
		for _, key := range []string{"source", "destination"} {
			if v, ok := spec[key]; ok {
				summary[key] = v
			}
		}
	}
	if status != nil {
		for _, key := range []string{"health", "sync", "conditions", "operationState", "resources"} {
			if v, ok := status[key]; ok {
				summary[key] = v
			}
		}
	}

	jsonResp(w, summary)
}

// --- ArgoCD Diff ---

func (h *Handler) handleArgoCDDiff(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Name      string `json:"name"`
		Namespace string `json:"namespace"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	app, err := h.dynClient.Resource(applicationGVR).Namespace(req.Namespace).Get(r.Context(), req.Name, metav1.GetOptions{})
	if err != nil {
		jsonResp(w, map[string]string{"error": fmt.Sprintf("Error: %v", err)})
		return
	}

	resources, _, _ := unstructured.NestedSlice(app.Object, "status", "resources")
	syncStatus, _, _ := unstructured.NestedString(app.Object, "status", "sync", "status")
	conditions, _, _ := unstructured.NestedSlice(app.Object, "status", "conditions")

	jsonResp(w, map[string]any{
		"syncStatus": syncStatus,
		"resources":  resources,
		"conditions": conditions,
	})
}

func jsonResp(w http.ResponseWriter, data any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(data)
}
