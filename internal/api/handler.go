package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/argoai/argocd-agent/internal/health"
	"github.com/argoai/argocd-agent/internal/metrics"
	"github.com/argoai/argocd-agent/internal/secrets"

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

// Handler serves the external API that ArgoCD's proxy extension talks to.
// It collects initial signals from K8s, forwards to the Python agent service
// for diagnosis, and proxies the SSE stream back to the frontend.
type Handler struct {
	clientset       kubernetes.Interface
	dynClient       dynamic.Interface
	agentServiceURL string // Python agent service URL (e.g. "http://localhost:8081")
	healthChecker   *health.Checker
	secretsManager  *secrets.Manager
	httpClient      *http.Client
}

func NewHandler(cs kubernetes.Interface, dyn dynamic.Interface, agentServiceURL string, hc *health.Checker, sm *secrets.Manager) *Handler {
	return &Handler{
		clientset:       cs,
		dynClient:       dyn,
		agentServiceURL: agentServiceURL,
		healthChecker:   hc,
		secretsManager:  sm,
		httpClient: &http.Client{
			Timeout: 5 * time.Minute, // Long timeout for LLM responses
		},
	}
}

func (h *Handler) RegisterRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/health", metrics.InstrumentHandler("/api/v1/health", h.handleHealth))
	mux.HandleFunc("GET /api/v1/providers", metrics.InstrumentHandler("/api/v1/providers", h.handleProviders))
	mux.HandleFunc("POST /api/v1/diagnose", metrics.InstrumentHandler("/api/v1/diagnose", h.handleDiagnose))
}

func (h *Handler) handleHealth(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func (h *Handler) handleProviders(w http.ResponseWriter, r *http.Request) {
	// Get provider status from secrets manager
	providers := h.secretsManager.ListProviders(r.Context())

	providerList := make([]map[string]any, 0, len(providers))
	for _, p := range providers {
		providerList = append(providerList, map[string]any{
			"id":          p.ID,
			"name":        p.Name,
			"hasKey":      p.HasKey,   // True if key configured in secret
			"keyField":    p.KeyField, // Secret field name for this provider
			"requiresKey": !p.HasKey,  // True if user must provide key in request
		})
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"providers":        providerList,
		"secretConfigured": h.secretsManager.HasConfiguredProvider(r.Context()),
	})
}

// handleDiagnose collects signals from K8s, sends them to the Python agent service,
// and proxies the SSE stream back to the ArgoCD UI.
func (h *Handler) handleDiagnose(w http.ResponseWriter, r *http.Request) {
	startTime := time.Now()

	var req map[string]any
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "invalid request body", http.StatusBadRequest)
		return
	}

	appName, _ := req["appName"].(string)
	appNamespace, _ := req["appNamespace"].(string)
	provider, _ := req["provider"].(string)
	apiKeyFromRequest, _ := req["apiKey"].(string) // Optional: BYOM mode

	if appName == "" || provider == "" {
		metrics.DiagnosisRequestsTotal.WithLabelValues(provider, "bad_request").Inc()
		http.Error(w, "appName and provider are required", http.StatusBadRequest)
		return
	}

	// Get API key: first try Kubernetes secret, fall back to request body (BYOM)
	apiKey, err := h.secretsManager.GetAPIKey(r.Context(), provider, apiKeyFromRequest)
	if err != nil {
		metrics.DiagnosisRequestsTotal.WithLabelValues(provider, "no_api_key").Inc()
		slog.Warn("no API key available", "provider", provider, "error", err)
		http.Error(w, fmt.Sprintf("No API key configured for provider %q. Either configure the secret %s or provide apiKey in the request body.", provider, secrets.SecretName), http.StatusBadRequest)
		return
	}
	if appNamespace == "" {
		appNamespace = "argocd"
	}

	// Also check ArgoCD proxy headers.
	if argoApp := r.Header.Get("Argocd-Application-Name"); argoApp != "" && appName == "" {
		parts := strings.SplitN(argoApp, ":", 2)
		if len(parts) == 2 {
			appNamespace = parts[0]
			appName = parts[1]
		}
	}

	// Check if agent service is healthy before proceeding
	if !h.healthChecker.IsAgentHealthy() {
		metrics.DiagnosisRequestsTotal.WithLabelValues(provider, "agent_unavailable").Inc()
		slog.Warn("agent service not healthy, proceeding anyway", "app", appName)
	}

	// Collect initial diagnostic signals from K8s.
	signals, err := h.collectSignals(r.Context(), appName, appNamespace)
	if err != nil {
		metrics.DiagnosisRequestsTotal.WithLabelValues(provider, "k8s_error").Inc()
		slog.Error("failed to collect signals", "error", err)
		http.Error(w, fmt.Sprintf("failed to collect cluster data: %v", err), http.StatusInternalServerError)
		return
	}

	// Build the request to the Python agent service.
	agentReq := map[string]any{
		"appName":      appName,
		"appNamespace": appNamespace,
		"provider":     provider,
		"apiKey":       apiKey,
		"model":        req["model"],
		"signals":      signals,
	}

	agentBody, _ := json.Marshal(agentReq)

	// Forward to the Python agent service.
	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()

	agentHTTPReq, err := http.NewRequestWithContext(ctx, http.MethodPost, h.agentServiceURL+"/diagnose", strings.NewReader(string(agentBody)))
	if err != nil {
		metrics.DiagnosisRequestsTotal.WithLabelValues(provider, "internal_error").Inc()
		http.Error(w, "failed to create agent request", http.StatusInternalServerError)
		return
	}
	agentHTTPReq.Header.Set("Content-Type", "application/json")

	agentResp, err := h.httpClient.Do(agentHTTPReq)
	if err != nil {
		metrics.DiagnosisRequestsTotal.WithLabelValues(provider, "agent_error").Inc()
		slog.Error("failed to call agent service", "error", err)
		http.Error(w, fmt.Sprintf("agent service unavailable: %v", err), http.StatusServiceUnavailable)
		return
	}
	defer agentResp.Body.Close()

	// Proxy the SSE stream from Python back to the frontend.
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("Access-Control-Allow-Origin", "*")

	flusher, ok := w.(http.Flusher)
	if !ok {
		metrics.DiagnosisRequestsTotal.WithLabelValues(provider, "internal_error").Inc()
		http.Error(w, "streaming not supported", http.StatusInternalServerError)
		return
	}

	buf := make([]byte, 4096)
	for {
		n, err := agentResp.Body.Read(buf)
		if n > 0 {
			w.Write(buf[:n])
			flusher.Flush()
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			slog.Error("error reading agent stream", "error", err)
			break
		}
	}

	// Record successful diagnosis metrics
	metrics.DiagnosisRequestsTotal.WithLabelValues(provider, "success").Inc()
	metrics.DiagnosisDuration.WithLabelValues(provider).Observe(time.Since(startTime).Seconds())
}

func (h *Handler) collectSignals(ctx context.Context, appName, appNamespace string) (map[string]any, error) {
	signals := map[string]any{
		"appName":      appName,
		"appNamespace": appNamespace,
	}

	app, err := h.dynClient.Resource(applicationGVR).Namespace(appNamespace).Get(ctx, appName, metav1.GetOptions{})
	if err != nil {
		return nil, fmt.Errorf("fetching application %s: %w", appName, err)
	}

	healthStatus, _, _ := unstructured.NestedString(app.Object, "status", "health", "status")
	syncStatus, _, _ := unstructured.NestedString(app.Object, "status", "sync", "status")
	signals["healthStatus"] = healthStatus
	signals["syncStatus"] = syncStatus

	// Extract sync policy -- agents need to know if auto-sync is active
	// to determine whether fixes should go through Git or runtime patches.
	syncPolicy, _, _ := unstructured.NestedMap(app.Object, "spec", "syncPolicy")
	if syncPolicy != nil {
		autoSync, hasAutoSync, _ := unstructured.NestedMap(app.Object, "spec", "syncPolicy", "automated")
		if hasAutoSync && autoSync != nil {
			selfHeal, _, _ := unstructured.NestedBool(app.Object, "spec", "syncPolicy", "automated", "selfHeal")
			signals["autoSyncEnabled"] = true
			signals["selfHealEnabled"] = selfHeal
		} else {
			signals["autoSyncEnabled"] = false
			signals["selfHealEnabled"] = false
		}
	} else {
		signals["autoSyncEnabled"] = false
		signals["selfHealEnabled"] = false
	}

	conditions, _, _ := unstructured.NestedSlice(app.Object, "status", "conditions")
	var condMsgs []string
	for _, c := range conditions {
		if cond, ok := c.(map[string]any); ok {
			if msg, ok := cond["message"].(string); ok {
				condMsgs = append(condMsgs, msg)
			}
		}
	}
	signals["conditions"] = condMsgs

	resources, _, _ := unstructured.NestedSlice(app.Object, "status", "resources")
	signals["resources"] = resources

	destNS, _, _ := unstructured.NestedString(app.Object, "spec", "destination", "namespace")
	if destNS == "" {
		destNS = "default"
	}
	signals["destinationNamespace"] = destNS

	// Extract source info for context.
	repoURL, _, _ := unstructured.NestedString(app.Object, "spec", "source", "repoURL")
	targetRevision, _, _ := unstructured.NestedString(app.Object, "spec", "source", "targetRevision")
	if repoURL != "" {
		signals["repoURL"] = repoURL
	}
	if targetRevision != "" {
		signals["targetRevision"] = targetRevision
	}

	// Build set of resource names managed by this ArgoCD app for scoped filtering
	managedNames := map[string]bool{}
	for _, r := range resources {
		if rm, ok := r.(map[string]any); ok {
			if name, ok := rm["name"].(string); ok {
				managedNames[name] = true
			}
		}
	}
	// Fallback: if ArgoCD has no synced resources, use the app name itself
	// to scope filtering (common convention: app name == deployment name)
	if len(managedNames) == 0 {
		managedNames[appName] = true
	}

	events, err := h.clientset.CoreV1().Events(destNS).List(ctx, metav1.ListOptions{})
	if err == nil {
		var warningEvents []map[string]string
		for _, e := range events.Items {
			if e.Type != "Warning" {
				continue
			}
			// Only include events for resources managed by this ArgoCD app
			if len(managedNames) > 0 && !isOwnedByManagedResource(e.InvolvedObject.Name, managedNames) {
				continue
			}
			warningEvents = append(warningEvents, map[string]string{
				"reason":  e.Reason,
				"message": e.Message,
				"object":  fmt.Sprintf("%s/%s", e.InvolvedObject.Kind, e.InvolvedObject.Name),
				"type":    e.Type,
			})
		}
		if len(warningEvents) > 20 {
			warningEvents = warningEvents[len(warningEvents)-20:]
		}
		signals["warningEvents"] = warningEvents
	}

	// SIGNAL PRE-LOADING: Fetch pod statuses scoped to this app's managed resources
	podStatuses, firstUnhealthyPod := h.collectPodStatuses(ctx, destNS, managedNames)
	if len(podStatuses) > 0 {
		signals["podStatuses"] = podStatuses
	}

	// If app is Degraded/Unhealthy and there's a non-running pod, fetch its logs
	if (healthStatus == "Degraded" || healthStatus == "Missing" || healthStatus == "Unknown") && firstUnhealthyPod != "" {
		logs := h.fetchPodLogs(ctx, destNS, firstUnhealthyPod, 100)
		if logs != "" {
			signals["preloadedLogs"] = map[string]string{
				"pod":  firstUnhealthyPod,
				"logs": logs,
			}
		}
	}

	return signals, nil
}

// collectPodStatuses fetches pod statuses scoped to managed resources and returns the first unhealthy pod name
func (h *Handler) collectPodStatuses(ctx context.Context, namespace string, managedNames map[string]bool) ([]map[string]any, string) {
	pods, err := h.clientset.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		slog.Warn("failed to list pods for signal preloading", "namespace", namespace, "error", err)
		return nil, ""
	}

	var statuses []map[string]any
	var firstUnhealthyPod string

	for _, pod := range pods.Items {
		if len(managedNames) > 0 {
			owned := isOwnedByManagedResource(pod.Name, managedNames)
			if !owned {
				for _, ref := range pod.OwnerReferences {
					if isOwnedByManagedResource(ref.Name, managedNames) {
						owned = true
						break
					}
				}
			}
			if !owned {
				continue
			}
		}

		ready := 0
		total := len(pod.Status.ContainerStatuses)
		var restarts int32
		var containerState string
		var stateReason string
		var exitCode int32
		var lastTerminatedReason string

		for _, cs := range pod.Status.ContainerStatuses {
			if cs.Ready {
				ready++
			}
			restarts += cs.RestartCount

			// Extract container state details
			if cs.State.Waiting != nil {
				containerState = "Waiting"
				stateReason = cs.State.Waiting.Reason
			} else if cs.State.Terminated != nil {
				containerState = "Terminated"
				stateReason = cs.State.Terminated.Reason
				exitCode = cs.State.Terminated.ExitCode
			} else if cs.State.Running != nil {
				containerState = "Running"
			}

			// Check last terminated state for crash info
			if cs.LastTerminationState.Terminated != nil {
				lastTerminatedReason = cs.LastTerminationState.Terminated.Reason
				if exitCode == 0 && cs.LastTerminationState.Terminated.ExitCode != 0 {
					exitCode = cs.LastTerminationState.Terminated.ExitCode
				}
			}
		}

		podStatus := map[string]any{
			"name":     pod.Name,
			"phase":    string(pod.Status.Phase),
			"ready":    fmt.Sprintf("%d/%d", ready, total),
			"restarts": restarts,
		}

		if containerState != "" {
			podStatus["containerState"] = containerState
		}
		if stateReason != "" {
			podStatus["stateReason"] = stateReason
		}
		if exitCode != 0 {
			podStatus["exitCode"] = exitCode
		}
		if lastTerminatedReason != "" {
			podStatus["lastTerminatedReason"] = lastTerminatedReason
		}

		// Identify first unhealthy pod for log fetching
		isUnhealthy := pod.Status.Phase != "Running" || ready < total ||
			stateReason == "CrashLoopBackOff" || stateReason == "ImagePullBackOff" ||
			stateReason == "OOMKilled" || stateReason == "Error" ||
			lastTerminatedReason == "OOMKilled"

		if isUnhealthy && firstUnhealthyPod == "" {
			firstUnhealthyPod = pod.Name
		}

		statuses = append(statuses, podStatus)
	}

	// Limit to 15 pods to avoid huge payloads
	if len(statuses) > 15 {
		statuses = statuses[:15]
	}

	return statuses, firstUnhealthyPod
}

// fetchPodLogs fetches recent logs from a pod, filtering for errors
func (h *Handler) fetchPodLogs(ctx context.Context, namespace, podName string, tailLines int64) string {
	opts := &corev1.PodLogOptions{
		TailLines: &tailLines,
	}

	stream, err := h.clientset.CoreV1().Pods(namespace).GetLogs(podName, opts).Stream(ctx)
	if err != nil {
		slog.Debug("failed to fetch pod logs for preloading", "pod", podName, "error", err)
		return ""
	}
	defer stream.Close()

	logBytes, err := io.ReadAll(io.LimitReader(stream, 8*1024)) // Limit to 8KB
	if err != nil {
		return ""
	}

	return string(logBytes)
}

// isOwnedByManagedResource checks if a resource name belongs to one of the managed resources.
// Handles K8s naming conventions: pods and replicasets have the deployment name as a prefix.
// e.g., managed name "demo-oomkilled" matches "demo-oomkilled-6f7985f4f4-5hchn"
func isOwnedByManagedResource(name string, managedNames map[string]bool) bool {
	if managedNames[name] {
		return true
	}
	for managed := range managedNames {
		if strings.HasPrefix(name, managed+"-") {
			return true
		}
	}
	return false
}
