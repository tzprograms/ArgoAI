.PHONY: all build-go build-agent run-go run-agent run docker-build docker-up docker-down demo-apps extract-rag clean

RAG_IMAGE ?= quay.io/devtools_gitops/argocd_lightspeed_byok:v0.0.4
CONTAINER_RUNTIME ?= $(shell if command -v docker >/dev/null 2>&1; then echo docker; elif command -v podman >/dev/null 2>&1; then echo podman; else echo docker; fi)

# --- Local Development ---

build-go:
	cd cmd/server && go build -o ../../bin/go-service .

run-go: build-go
	./bin/go-service --addr=:8080 --agent-url=http://localhost:8081

run-agent:
	RAG_INDEX_PATH=./rag_data/vector_db uv run python -m agent.main

run: ## Run both services locally (use two terminals)
	@echo "First, extract RAG data: make extract-rag"
	@echo "Terminal 1: make run-go"
	@echo "Terminal 2: make run-agent"

# --- RAG Data ---

# Extract RAG data from the pre-built Quay image (run once)
extract-rag:
	@echo "Extracting RAG data from $(RAG_IMAGE)..."
	@mkdir -p rag_data
	$(CONTAINER_RUNTIME) run --rm -v $(PWD)/rag_data:/out $(RAG_IMAGE) cp -r /rag/vector_db /out/
	@echo "RAG data extracted to ./rag_data/vector_db"
	@ls -la rag_data/vector_db

# --- Docker ---

docker-build:
	$(CONTAINER_RUNTIME) build -f Dockerfile.server -t argocd-agent-go:latest .
	$(CONTAINER_RUNTIME) build -f Dockerfile.agent -t argocd-agent-python:latest .

docker-build-go:
	$(CONTAINER_RUNTIME) build -f Dockerfile.server -t argocd-agent-go:latest .

docker-build-agent:
	$(CONTAINER_RUNTIME) build -f Dockerfile.agent -t argocd-agent-python:latest .

# Push to Quay (update registry as needed)
REGISTRY ?= quay.io/devtools_gitops

docker-push: docker-build
	$(CONTAINER_RUNTIME) tag argocd-agent-go:latest $(REGISTRY)/argocd-agent-go:latest
	$(CONTAINER_RUNTIME) tag argocd-agent-python:latest $(REGISTRY)/argocd-agent-python:latest
	$(CONTAINER_RUNTIME) push $(REGISTRY)/argocd-agent-go:latest
	$(CONTAINER_RUNTIME) push $(REGISTRY)/argocd-agent-python:latest

docker-up: extract-rag docker-build
	$(CONTAINER_RUNTIME) compose up -d

docker-down:
	$(CONTAINER_RUNTIME) compose down

docker-logs:
	$(CONTAINER_RUNTIME) compose logs -f

# --- K8s Deployment ---

# Deploy using kustomize (recommended)
deploy:
	kubectl apply -k config/deploy/

deploy-dry-run:
	kubectl apply -k config/deploy/ --dry-run=client -o yaml

# Individual component deployment
deploy-go: docker-build-go
	kubectl apply -f config/deploy/namespace.yaml
	kubectl apply -f config/deploy/go-service.yaml

deploy-python: docker-build-agent
	kubectl apply -f config/deploy/namespace.yaml
	kubectl apply -f config/deploy/python-service.yaml

deploy-monitoring:
	kubectl apply -f config/deploy/servicemonitor.yaml

undeploy:
	kubectl delete -k config/deploy/ --ignore-not-found

# Restart deployments (useful after image push)
restart:
	kubectl -n argocd-agent rollout restart deployment argocd-agent-go
	kubectl -n argocd-agent rollout restart deployment argocd-agent-python

# Check deployment status
status:
	kubectl -n argocd-agent get pods,svc,servicemonitor

logs-go:
	kubectl -n argocd-agent logs -f deployment/argocd-agent-go

logs-python:
	kubectl -n argocd-agent logs -f deployment/argocd-agent-python

# --- Demo ---

demo-apps:
	kubectl apply -f demo/oomkilled/
	kubectl apply -f demo/imagepull/
	kubectl apply -f demo/missing-config/

demo-clean:
	kubectl delete -f demo/oomkilled/ --ignore-not-found
	kubectl delete -f demo/imagepull/ --ignore-not-found
	kubectl delete -f demo/missing-config/ --ignore-not-found

# --- Cleanup ---

clean:
	rm -rf bin/ rag_data/
	$(CONTAINER_RUNTIME) compose down -v
