package health

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"k8s.io/client-go/kubernetes"
)

// Checker performs health checks for readiness and liveness probes
type Checker struct {
	clientset       kubernetes.Interface
	agentServiceURL string

	mu              sync.RWMutex
	k8sHealthy      bool
	agentHealthy    bool
	lastK8sCheck    time.Time
	lastAgentCheck  time.Time
}

func NewChecker(cs kubernetes.Interface, agentServiceURL string) *Checker {
	c := &Checker{
		clientset:       cs,
		agentServiceURL: agentServiceURL,
	}
	go c.startBackgroundChecks()
	return c
}

func (c *Checker) startBackgroundChecks() {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()

	// Initial check
	c.checkK8s()
	c.checkAgent()

	for range ticker.C {
		c.checkK8s()
		c.checkAgent()
	}
}

func (c *Checker) checkK8s() {
	_, err := c.clientset.Discovery().ServerVersion()
	
	c.mu.Lock()
	c.k8sHealthy = err == nil
	c.lastK8sCheck = time.Now()
	c.mu.Unlock()
}

func (c *Checker) checkAgent() {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.agentServiceURL+"/health", nil)
	resp, err := http.DefaultClient.Do(req)
	
	c.mu.Lock()
	c.agentHealthy = err == nil && resp != nil && resp.StatusCode == http.StatusOK
	c.lastAgentCheck = time.Now()
	c.mu.Unlock()

	if resp != nil {
		resp.Body.Close()
	}
}

// LivenessHandler checks if the service itself is alive (not deadlocked)
// Kubernetes restarts the pod if this fails
func (c *Checker) LivenessHandler(w http.ResponseWriter, r *http.Request) {
	// Liveness is simple: if we can respond, we're alive
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"status": "alive",
		"time":   time.Now().UTC().Format(time.RFC3339),
	})
}

// ReadinessHandler checks if the service is ready to receive traffic
// Kubernetes removes the pod from the service if this fails
func (c *Checker) ReadinessHandler(w http.ResponseWriter, r *http.Request) {
	c.mu.RLock()
	k8sOK := c.k8sHealthy
	agentOK := c.agentHealthy
	lastK8s := c.lastK8sCheck
	lastAgent := c.lastAgentCheck
	c.mu.RUnlock()

	status := "ready"
	httpStatus := http.StatusOK

	// We require K8s connection for readiness
	// Agent service is optional (degraded mode)
	if !k8sOK {
		status = "not_ready"
		httpStatus = http.StatusServiceUnavailable
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(httpStatus)
	json.NewEncoder(w).Encode(map[string]any{
		"status": status,
		"checks": map[string]any{
			"kubernetes": map[string]any{
				"healthy":    k8sOK,
				"lastCheck":  lastK8s.UTC().Format(time.RFC3339),
			},
			"agentService": map[string]any{
				"healthy":    agentOK,
				"lastCheck":  lastAgent.UTC().Format(time.RFC3339),
			},
		},
	})
}

// HealthHandler is a simple combined health check (backwards compatible)
func (c *Checker) HealthHandler(w http.ResponseWriter, r *http.Request) {
	c.mu.RLock()
	k8sOK := c.k8sHealthy
	agentOK := c.agentHealthy
	c.mu.RUnlock()

	status := "healthy"
	if !k8sOK {
		status = "degraded"
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"status":       status,
		"kubernetes":   k8sOK,
		"agentService": agentOK,
	})
}

// IsAgentHealthy returns whether the Python agent service is reachable
func (c *Checker) IsAgentHealthy() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.agentHealthy
}
