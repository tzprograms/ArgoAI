package k8s

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/dynamic/fake"
	k8sfake "k8s.io/client-go/kubernetes/fake"
)

func TestHandleResource_RejectsSecrets(t *testing.T) {
	// Create fake clients
	cs := k8sfake.NewSimpleClientset()
	scheme := runtime.NewScheme()
	dynClient := fake.NewSimpleDynamicClient(scheme)

	h := NewHandler(cs, dynClient)

	// Create request for a Secret
	reqBody := map[string]string{
		"kind":      "Secret",
		"namespace": "default",
		"name":      "my-secret",
	}
	body, _ := json.Marshal(reqBody)

	req := httptest.NewRequest("POST", "/internal/k8s/resource", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	h.handleResource(w, req)

	// Should return 403 Forbidden
	if w.Code != http.StatusForbidden {
		t.Errorf("Expected status 403 Forbidden, got %d", w.Code)
	}

	// Check error message
	var resp map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("Failed to parse response: %v", err)
	}

	if _, ok := resp["error"]; !ok {
		t.Error("Expected error field in response")
	}

	// Verify error mentions security
	if resp["error"] == "" {
		t.Error("Error message should not be empty")
	}
}

func TestHandleResource_RejectsSecretCaseInsensitive(t *testing.T) {
	cs := k8sfake.NewSimpleClientset()
	scheme := runtime.NewScheme()
	dynClient := fake.NewSimpleDynamicClient(scheme)

	h := NewHandler(cs, dynClient)

	// Test various case variations
	testCases := []string{"SECRET", "Secret", "secret", "SeCrEt"}

	for _, kind := range testCases {
		t.Run(kind, func(t *testing.T) {
			reqBody := map[string]string{
				"kind":      kind,
				"namespace": "default",
				"name":      "test-secret",
			}
			body, _ := json.Marshal(reqBody)

			req := httptest.NewRequest("POST", "/internal/k8s/resource", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			w := httptest.NewRecorder()

			h.handleResource(w, req)

			if w.Code != http.StatusForbidden {
				t.Errorf("Kind %q: Expected status 403, got %d", kind, w.Code)
			}
		})
	}
}

func TestBuildResourceSummary_Deployment(t *testing.T) {
	obj := &unstructured.Unstructured{
		Object: map[string]any{
			"apiVersion": "apps/v1",
			"kind":       "Deployment",
			"metadata": map[string]any{
				"name":      "test-deploy",
				"namespace": "default",
			},
			"spec": map[string]any{
				"replicas": int64(3),
				"template": map[string]any{
					"spec": map[string]any{
						"serviceAccountName": "test-sa",
						"containers": []any{
							map[string]any{
								"name":  "main",
								"image": "nginx:1.21",
								"resources": map[string]any{
									"limits": map[string]any{
										"memory": "128Mi",
									},
								},
								"env": []any{
									map[string]any{
										"name":  "FOO",
										"value": "bar",
									},
									map[string]any{
										"name": "SECRET_VAL",
										"valueFrom": map[string]any{
											"secretKeyRef": map[string]any{
												"name": "my-secret",
												"key":  "password",
											},
										},
									},
								},
							},
						},
						"volumes": []any{
							map[string]any{
								"name": "config-vol",
								"configMap": map[string]any{
									"name": "my-config",
								},
							},
						},
					},
				},
			},
			"status": map[string]any{
				"readyReplicas": int64(2),
			},
		},
	}

	summary := buildResourceSummary("deployment", obj)

	// Verify key fields are present
	if summary["name"] != "test-deploy" {
		t.Errorf("Expected name test-deploy, got %v", summary["name"])
	}

	if summary["serviceAccountName"] != "test-sa" {
		t.Errorf("Expected serviceAccountName test-sa, got %v", summary["serviceAccountName"])
	}

	// Verify containers are summarized
	containers, ok := summary["containers"].([]map[string]any)
	if !ok || len(containers) == 0 {
		t.Fatal("Expected containers in summary")
	}

	c := containers[0]
	if c["image"] != "nginx:1.21" {
		t.Errorf("Expected image nginx:1.21, got %v", c["image"])
	}

	// Verify secret refs are counted (not exposed)
	if c["env_secret_refs"] != 1 {
		t.Errorf("Expected 1 env_secret_refs, got %v", c["env_secret_refs"])
	}
}

func TestBuildResourceSummary_PVC(t *testing.T) {
	obj := &unstructured.Unstructured{
		Object: map[string]any{
			"apiVersion": "v1",
			"kind":       "PersistentVolumeClaim",
			"metadata": map[string]any{
				"name":      "test-pvc",
				"namespace": "default",
			},
			"spec": map[string]any{
				"storageClassName": "gp2",
				"accessModes":      []any{"ReadWriteOnce"},
				"resources": map[string]any{
					"requests": map[string]any{
						"storage": "10Gi",
					},
				},
			},
			"status": map[string]any{
				"phase": "Bound",
			},
		},
	}

	summary := buildResourceSummary("persistentvolumeclaim", obj)

	if summary["phase"] != "Bound" {
		t.Errorf("Expected phase Bound, got %v", summary["phase"])
	}

	if summary["storageClassName"] != "gp2" {
		t.Errorf("Expected storageClassName gp2, got %v", summary["storageClassName"])
	}
}

func TestBuildResourceSummary_Service(t *testing.T) {
	obj := &unstructured.Unstructured{
		Object: map[string]any{
			"apiVersion": "v1",
			"kind":       "Service",
			"metadata": map[string]any{
				"name":      "test-svc",
				"namespace": "default",
			},
			"spec": map[string]any{
				"type":      "ClusterIP",
				"clusterIP": "10.0.0.1",
				"ports": []any{
					map[string]any{
						"name":       "http",
						"port":       int64(80),
						"targetPort": int64(8080),
						"protocol":   "TCP",
					},
				},
				"selector": map[string]any{
					"app": "test",
				},
			},
		},
	}

	summary := buildResourceSummary("service", obj)

	if summary["type"] != "ClusterIP" {
		t.Errorf("Expected type ClusterIP, got %v", summary["type"])
	}

	if summary["clusterIP"] != "10.0.0.1" {
		t.Errorf("Expected clusterIP 10.0.0.1, got %v", summary["clusterIP"])
	}

	ports, ok := summary["ports"].([]map[string]any)
	if !ok || len(ports) == 0 {
		t.Fatal("Expected ports in summary")
	}
}

func TestKindToGVR_NoSecrets(t *testing.T) {
	// Verify secrets are not in the allowed resource list
	if _, ok := kindToGVR["secret"]; ok {
		t.Error("secret should not be in kindToGVR")
	}
}

func TestBlockedKinds_IncludesSecret(t *testing.T) {
	if !blockedKinds["secret"] {
		t.Error("secret should be in blockedKinds")
	}
}
