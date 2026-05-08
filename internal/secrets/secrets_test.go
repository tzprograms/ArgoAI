package secrets

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func newTestManager(secretData map[string][]byte) *Manager {
	const namespace = "openshift-gitops"
	client := fake.NewSimpleClientset(&corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      SecretName,
			Namespace: namespace,
		},
		Data: secretData,
	})
	return NewManager(client, namespace)
}

func TestListProvidersDoesNotExposeDemoProvider(t *testing.T) {
	manager := newTestManager(map[string][]byte{})

	providers := manager.ListProviders(context.Background())

	for _, provider := range providers {
		if provider.ID == "demo" {
			t.Fatal("demo provider should not be exposed")
		}
	}
}

func TestHasConfiguredProviderIgnoresNoKeyProviders(t *testing.T) {
	manager := newTestManager(map[string][]byte{})

	if manager.HasConfiguredProvider(context.Background()) {
		t.Fatal("expected no hosted provider to be configured")
	}
}

func TestHasConfiguredProviderDetectsHostedKey(t *testing.T) {
	manager := newTestManager(map[string][]byte{
		GeminiKeyName: []byte("gemini-key"),
	})

	if !manager.HasConfiguredProvider(context.Background()) {
		t.Fatal("expected hosted provider key to be configured")
	}
}

func TestOpenRouterProviderUsesOwnSecretField(t *testing.T) {
	manager := newTestManager(map[string][]byte{
		OpenRouterKeyName: []byte("openrouter-key"),
	})

	key, err := manager.GetAPIKey(context.Background(), "openrouter", "")
	if err != nil {
		t.Fatalf("expected openrouter key: %v", err)
	}
	if key != "openrouter-key" {
		t.Fatalf("unexpected openrouter key %q", key)
	}
}
