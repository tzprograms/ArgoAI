"""Python Agent Service -- FastAPI server for ADK-based diagnosis."""

import json
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from sse_starlette.sse import EventSourceResponse

from agent.engine import (
    run_diagnosis,
    clear_diagnosis_cache,
    get_cache_stats,
    list_available_agents,
)
from agent.tools.k8s_tools import _set_go_service_url
from agent import metrics as prom_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GO_SERVICE_URL = os.getenv("GO_SERVICE_URL", "http://localhost:8080")
RAG_INDEX_PATH = os.getenv("RAG_INDEX_PATH", "/rag/vector_db")

# Global state for health checks
_rag_loaded = False
_go_service_healthy = False


def _init_rag():
    """Initialize RAG from pre-built index."""
    global _rag_loaded
    if os.path.isdir(RAG_INDEX_PATH):
        try:
            from agent.rag.retriever import RAGRetriever
            import agent.rag.retriever as ret_module
            import agent.tools.rag_tools as rag_tools

            retriever = RAGRetriever(index_path=RAG_INDEX_PATH)
            ret_module.retriever_instance = retriever
            rag_tools.retriever_instance = retriever
            if retriever.is_loaded():
                logger.info(f"RAG retriever loaded from {RAG_INDEX_PATH}")
                _rag_loaded = True
                prom_metrics.RAG_LOADED.set(1)
                if retriever._index is not None:
                    prom_metrics.RAG_INDEX_SIZE.set(retriever._index.ntotal)
            else:
                logger.warning(f"RAG index at {RAG_INDEX_PATH} could not be loaded")
                prom_metrics.RAG_LOADED.set(0)
        except Exception as e:
            logger.warning(f"RAG initialization failed: {e}")
            prom_metrics.RAG_LOADED.set(0)
    else:
        logger.info(f"RAG index path not found: {RAG_INDEX_PATH}, RAG disabled")
        prom_metrics.RAG_LOADED.set(0)


async def _check_go_service():
    """Check if Go service is reachable."""
    global _go_service_healthy
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{GO_SERVICE_URL}/healthz")
            _go_service_healthy = resp.status_code == 200
    except Exception:
        _go_service_healthy = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown."""
    # Startup
    _set_go_service_url(GO_SERVICE_URL)
    logger.info(f"Go service URL: {GO_SERVICE_URL}")
    prom_metrics.init_service_info()
    _init_rag()
    await _check_go_service()
    yield
    # Shutdown (nothing to clean up)


app = FastAPI(
    title="ArgoCD Agent Service",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )


@app.get("/health")
async def health():
    """Simple health check (backwards compatible)."""
    return {"status": "ok", "service": "agent", "rag_loaded": _rag_loaded}


@app.get("/livez")
async def liveness():
    """Liveness probe - checks if the service is alive."""
    return {"status": "alive"}


@app.get("/readyz")
async def readiness():
    """Readiness probe - checks if the service is ready to handle requests."""
    await _check_go_service()

    # RAG is optional (degraded mode without it)
    # Go service connectivity is optional for readiness (we can still try)
    ready = True  # Service is ready if it can respond

    status_code = 200 if ready else 503
    return Response(
        content=json.dumps({
            "status": "ready" if ready else "not_ready",
            "checks": {
                "rag": {"loaded": _rag_loaded},
                "go_service": {"healthy": _go_service_healthy},
            }
        }),
        status_code=status_code,
        media_type="application/json"
    )


@app.get("/cache/stats")
async def cache_stats():
    """Get semantic cache statistics."""
    return get_cache_stats()


@app.post("/cache/clear")
async def cache_clear():
    """Clear the semantic diagnosis cache."""
    clear_diagnosis_cache()
    return {"status": "ok", "message": "Cache cleared"}


@app.get("/agents")
async def list_agents():
    """List all available specialist agents with their metadata (A2A-style discovery)."""
    return {
        "agents": list_available_agents(),
        "routing": "Requests are automatically routed to the best agent based on symptoms"
    }


@app.post("/diagnose")
async def diagnose(request: Request):
    """Run the multi-agent diagnostic pipeline and stream results via SSE."""
    start_time = time.time()
    prom_metrics.ACTIVE_DIAGNOSES.inc()

    body = await request.json()

    provider = body.get("provider", "gemini")
    api_key = body.get("apiKey", "")
    model_name = body.get("model", "")
    signals = body.get("signals", {})

    if not api_key and provider != "ollama":
        prom_metrics.DIAGNOSIS_REQUESTS.labels(provider=provider, status="bad_request").inc()
        prom_metrics.ACTIVE_DIAGNOSES.dec()
        return {"error": "apiKey is required for provider: " + provider}

    async def event_generator():
        try:
            async for event in run_diagnosis(provider, api_key, model_name, signals):
                yield {"data": json.dumps(event)}
            prom_metrics.DIAGNOSIS_REQUESTS.labels(provider=provider, status="success").inc()
        except Exception as e:
            prom_metrics.DIAGNOSIS_REQUESTS.labels(provider=provider, status="error").inc()
            yield {"data": json.dumps({"type": "error", "error": str(e)})}
        finally:
            prom_metrics.ACTIVE_DIAGNOSES.dec()
            prom_metrics.DIAGNOSIS_DURATION.labels(provider=provider).observe(time.time() - start_time)

    return EventSourceResponse(event_generator())


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8081"))
    uvicorn.run(app, host="0.0.0.0", port=port)
