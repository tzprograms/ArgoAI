package secrets

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

const (
	// SecretName is the name of the Kubernetes secret containing LLM API keys
	SecretName = "argocd-agent-llm-keys"

	// Key names in the secret
	GeminiKeyName     = "gemini-api-key"
	OpenAIKeyName     = "openai-api-key"
	AnthropicKeyName  = "anthropic-api-key"
	GroqKeyName       = "groq-api-key"
	OpenRouterKeyName = "openrouter-api-key"
)

var noKeyProviders = map[string]bool{
	"ollama": true,
}

// Provider represents an LLM provider configuration
type Provider struct {
	ID       string
	Name     string
	HasKey   bool
	KeyField string
}

// Manager handles LLM API key retrieval from Kubernetes secrets
type Manager struct {
	clientset kubernetes.Interface
	namespace string

	mu         sync.RWMutex
	cachedKeys map[string]string
	lastFetch  time.Time
	cacheTTL   time.Duration
}

// NewManager creates a new secrets manager
func NewManager(cs kubernetes.Interface, namespace string) *Manager {
	return &Manager{
		clientset:  cs,
		namespace:  namespace,
		cachedKeys: make(map[string]string),
		cacheTTL:   30 * time.Second, // Refresh keys every 30 seconds
	}
}

// GetAPIKey retrieves the API key for a given provider
// Falls back to the provided key if secret lookup fails
func (m *Manager) GetAPIKey(ctx context.Context, provider, fallbackKey string) (string, error) {
	// Local/offline providers don't need an API key.
	if noKeyProviders[strings.ToLower(provider)] {
		return "", nil
	}

	// If fallback key provided, use it (BYOM mode)
	if fallbackKey != "" {
		return fallbackKey, nil
	}

	// Try to get from secret
	key, err := m.getKeyFromSecret(ctx, provider)
	if err != nil {
		return "", fmt.Errorf("no API key provided and secret lookup failed: %w", err)
	}

	if key == "" {
		return "", fmt.Errorf("no API key configured for provider %q in secret %s", provider, SecretName)
	}

	return key, nil
}

// getKeyFromSecret retrieves the API key from the Kubernetes secret
func (m *Manager) getKeyFromSecret(ctx context.Context, provider string) (string, error) {
	m.mu.RLock()
	if time.Since(m.lastFetch) < m.cacheTTL {
		key := m.cachedKeys[provider]
		m.mu.RUnlock()
		return key, nil
	}
	m.mu.RUnlock()

	// Refresh cache
	if err := m.refreshCache(ctx); err != nil {
		return "", err
	}

	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.cachedKeys[provider], nil
}

// refreshCache fetches the secret and updates the cache
func (m *Manager) refreshCache(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	secret, err := m.clientset.CoreV1().Secrets(m.namespace).Get(ctx, SecretName, metav1.GetOptions{})
	if err != nil {
		slog.Warn("failed to fetch LLM keys secret", "error", err, "secret", SecretName, "namespace", m.namespace)
		return err
	}

	// Map secret data to provider keys
	m.cachedKeys = make(map[string]string)

	providerKeyMap := map[string]string{
		"gemini":     GeminiKeyName,
		"google":     GeminiKeyName,
		"openai":     OpenAIKeyName,
		"chatgpt":    OpenAIKeyName,
		"anthropic":  AnthropicKeyName,
		"claude":     AnthropicKeyName,
		"groq":       GroqKeyName,
		"openrouter": OpenRouterKeyName,
	}

	for provider, keyName := range providerKeyMap {
		if data, ok := secret.Data[keyName]; ok {
			m.cachedKeys[provider] = string(data)
		}
	}

	m.lastFetch = time.Now()
	slog.Info("refreshed LLM keys cache", "providers", len(m.cachedKeys))

	return nil
}

// ListProviders returns available providers and their configuration status
func (m *Manager) ListProviders(ctx context.Context) []Provider {
	// Try to refresh cache to get current status
	_ = m.refreshCache(ctx)

	m.mu.RLock()
	defer m.mu.RUnlock()

	providers := []Provider{
		{ID: "gemini", Name: "Google Gemini", KeyField: GeminiKeyName, HasKey: m.cachedKeys["gemini"] != ""},
		{ID: "openai", Name: "OpenAI / ChatGPT", KeyField: OpenAIKeyName, HasKey: m.cachedKeys["openai"] != ""},
		{ID: "anthropic", Name: "Anthropic / Claude", KeyField: AnthropicKeyName, HasKey: m.cachedKeys["anthropic"] != ""},
		{ID: "groq", Name: "Groq", KeyField: GroqKeyName, HasKey: m.cachedKeys["groq"] != ""},
		{ID: "openrouter", Name: "OpenRouter", KeyField: OpenRouterKeyName, HasKey: m.cachedKeys["openrouter"] != ""},
		{ID: "ollama", Name: "Ollama (Local)", KeyField: "", HasKey: true},
	}

	return providers
}

// HasConfiguredProvider checks if at least one provider has a key configured
func (m *Manager) HasConfiguredProvider(ctx context.Context) bool {
	providers := m.ListProviders(ctx)
	for _, p := range providers {
		if p.KeyField != "" && p.HasKey {
			return true
		}
	}
	return false
}

// CreateSecretTemplate returns a template for the LLM keys secret
func CreateSecretTemplate(namespace string) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      SecretName,
			Namespace: namespace,
			Labels: map[string]string{
				"app.kubernetes.io/name":      "argocd-agent",
				"app.kubernetes.io/component": "llm-keys",
			},
		},
		Type: corev1.SecretTypeOpaque,
		StringData: map[string]string{
			GeminiKeyName:     "", // Add your Gemini API key
			OpenAIKeyName:     "", // Add your OpenAI API key
			AnthropicKeyName:  "", // Add your Anthropic API key
			GroqKeyName:       "", // Add your Groq API key
			OpenRouterKeyName: "", // Add your OpenRouter API key
		},
	}
}
