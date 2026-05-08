// Entry point for the golang service

package main

import (
	"context"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/argoai/argocd-agent/internal/api"
	"github.com/argoai/argocd-agent/internal/health"
	"github.com/argoai/argocd-agent/internal/k8s"
	"github.com/argoai/argocd-agent/internal/metrics"
	"github.com/argoai/argocd-agent/internal/secrets"

	"k8s.io/client-go/dynamic"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

func main() {
	var (
		addr            = flag.String("addr", ":8080", "HTTP listen address")
		kubeconfig      = flag.String("kubeconfig", "", "Path to kubeconfig (uses in-cluster if empty)")
		agentServiceURL = flag.String("agent-url", "http://localhost:8081", "Python agent service URL")
		secretNamespace = flag.String("secret-namespace", "", "Namespace for LLM keys secret (defaults to pod namespace)")
	)
	flag.Parse()

	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo})))

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	config, err := buildK8sConfig(*kubeconfig)
	if err != nil {
		slog.Error("failed to build k8s config", "error", err)
		os.Exit(1)
	}

	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		slog.Error("failed to create k8s clientset", "error", err)
		os.Exit(1)
	}

	dynClient, err := dynamic.NewForConfig(config)
	if err != nil {
		slog.Error("failed to create dynamic client", "error", err)
		os.Exit(1)
	}

	slog.Info("kubernetes clients initialized")

	secretNS := *secretNamespace
	if secretNS == "" {
		if data, err := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/namespace"); err == nil {
			secretNS = string(data)
		} else {
			secretNS = "argocd-agent"
		}
	}

	secretsManager := secrets.NewManager(clientset, secretNS)
	slog.Info("secrets manager initialized", "namespace", secretNS)

	healthChecker := health.NewChecker(clientset, *agentServiceURL)

	mux := http.NewServeMux()
	mux.Handle("GET /metrics", metrics.MetricsHandler())
	mux.HandleFunc("GET /healthz", healthChecker.HealthHandler)
	mux.HandleFunc("GET /livez", healthChecker.LivenessHandler)
	mux.HandleFunc("GET /readyz", healthChecker.ReadinessHandler)

	apiHandler := api.NewHandler(clientset, dynClient, *agentServiceURL, healthChecker, secretsManager)
	apiHandler.RegisterRoutes(mux)

	k8sHandler := k8s.NewHandler(clientset, dynClient)
	k8sHandler.RegisterRoutes(mux)

	mux.Handle("GET /ui/", http.StripPrefix("/ui/", http.FileServer(http.Dir("ui-extension/dist"))))

	server := &http.Server{Addr: *addr, Handler: corsMiddleware(mux)}

	go func() {
		slog.Info("starting go service", "addr", *addr, "agentService", *agentServiceURL)
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("server error", "error", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	slog.Info("shutting down server")
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	server.Shutdown(shutdownCtx)
}

func buildK8sConfig(kubeconfig string) (*rest.Config, error) {
	if kubeconfig != "" {
		return clientcmd.BuildConfigFromFlags("", kubeconfig)
	}
	config, err := rest.InClusterConfig()
	if err != nil {
		home, _ := os.UserHomeDir()
		return clientcmd.BuildConfigFromFlags("", home+"/.kube/config")
	}
	return config, nil
}

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}
		next.ServeHTTP(w, r)
	})
}
