package metrics

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	// HTTP request metrics
	HTTPRequestsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "argocd_agent_http_requests_total",
			Help: "Total number of HTTP requests",
		},
		[]string{"method", "endpoint", "status"},
	)

	HTTPRequestDuration = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "argocd_agent_http_request_duration_seconds",
			Help:    "HTTP request duration in seconds",
			Buckets: []float64{0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120},
		},
		[]string{"method", "endpoint"},
	)

	HTTPActiveRequests = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "argocd_agent_http_active_requests",
			Help: "Number of active HTTP requests",
		},
	)

	// Diagnosis-specific metrics
	DiagnosisRequestsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "argocd_agent_diagnosis_requests_total",
			Help: "Total number of diagnosis requests",
		},
		[]string{"provider", "status"},
	)

	DiagnosisDuration = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "argocd_agent_diagnosis_duration_seconds",
			Help:    "Diagnosis request duration in seconds",
			Buckets: []float64{1, 5, 10, 30, 60, 120, 300},
		},
		[]string{"provider"},
	)

	// K8s API call metrics
	K8sAPICallsTotal = promauto.NewCounterVec(
		prometheus.CounterOpts{
			Name: "argocd_agent_k8s_api_calls_total",
			Help: "Total number of K8s API calls",
		},
		[]string{"endpoint", "status"},
	)

	K8sAPICallDuration = promauto.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "argocd_agent_k8s_api_call_duration_seconds",
			Help:    "K8s API call duration in seconds",
			Buckets: []float64{0.01, 0.05, 0.1, 0.5, 1, 2, 5},
		},
		[]string{"endpoint"},
	)

	// Agent service connection metrics
	AgentServiceUp = promauto.NewGauge(
		prometheus.GaugeOpts{
			Name: "argocd_agent_python_service_up",
			Help: "Whether the Python agent service is reachable (1 = up, 0 = down)",
		},
	)
)

// MetricsHandler returns the Prometheus metrics handler
func MetricsHandler() http.Handler {
	return promhttp.Handler()
}

// InstrumentHandler wraps an HTTP handler with metrics collection
func InstrumentHandler(endpoint string, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		HTTPActiveRequests.Inc()
		defer HTTPActiveRequests.Dec()

		// Wrap ResponseWriter to capture status code
		wrapped := &statusRecorder{ResponseWriter: w, statusCode: http.StatusOK}

		next(wrapped, r)

		duration := time.Since(start).Seconds()
		status := strconv.Itoa(wrapped.statusCode)

		HTTPRequestsTotal.WithLabelValues(r.Method, endpoint, status).Inc()
		HTTPRequestDuration.WithLabelValues(r.Method, endpoint).Observe(duration)
	}
}

type statusRecorder struct {
	http.ResponseWriter
	statusCode int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.statusCode = code
	r.ResponseWriter.WriteHeader(code)
}

// Flush implements http.Flusher for SSE support
func (r *statusRecorder) Flush() {
	if flusher, ok := r.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}
