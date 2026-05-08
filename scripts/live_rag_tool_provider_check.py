#!/usr/bin/env python
"""Live RAG, tool-call, and provider integration diagnostics.

This probe intentionally exercises three layers:
- local RAG index loading and direct RAG search
- direct Kubernetes tool access through the Go API service
- provider-driven tool calling that must invoke both RAG and cluster tools

It prints JSON and avoids logging secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any

from google.adk.agents.llm_agent import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from agent.engine import (
    _after_model_callback,
    _after_tool_callback,
    _before_model_callback,
    _before_tool_callback,
    _create_model,
    _format_provider_error,
    _is_known_provider_error,
    _on_model_error_callback,
)
from agent.rag.retriever import RAGRetriever
from agent.tools import rag_tools
from agent.tools.k8s_tools import _set_go_service_url, get_resource, get_resource_tool
from agent.tools.rag_tools import rag_search, rag_search_tool


DEFAULT_GEMINI_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)


def _preview(text: str, limit: int = 500) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _exception_payload(error: BaseException) -> dict[str, Any]:
    formatted = _format_provider_error(error)
    raw = str(error).lower()
    lowered = formatted.lower()

    if "quota" in lowered or "resource_exhausted" in raw or "429" in raw:
        kind = "quota_or_rate_limit"
    elif "unavailable" in lowered or "unavailable" in raw or "503" in raw or "overloaded" in raw:
        kind = "provider_unavailable"
    elif "api key" in lowered or "authentication" in lowered or "401" in raw or "403" in raw:
        kind = "auth_or_permission"
    elif _is_known_provider_error(error):
        kind = "provider_error"
    else:
        kind = "unexpected_error"

    return {
        "ok": False,
        "kind": kind,
        "message": formatted,
        "raw_preview": _preview(str(error), 800),
    }


def _load_rag(index_path: str, error_type: str) -> tuple[RAGRetriever, dict[str, Any]]:
    started = time.perf_counter()
    retriever = RAGRetriever(index_path=index_path)
    rag_tools.retriever_instance = retriever
    raw_query = "container memory limit exceeded OOMKilled exit code 137 increase resources"
    results = retriever.search(raw_query, top_k=3)
    tool_text = rag_search(error_type)
    elapsed = time.perf_counter() - started

    index = getattr(retriever, "_index", None)
    docstore = getattr(retriever, "_docstore", None)
    vector_count = getattr(index, "ntotal", 0) if index is not None else 0
    document_count = len(docstore) if isinstance(docstore, dict) else 0
    tool_unavailable = "rag search is unavailable" in tool_text.lower()
    expected_terms = ("oomkilled", "exit code 137", "memory limit", "out of memory", "exceeded memory")
    raw_blob = "\n".join(
        f"{result.get('source', '')}\n{result.get('content', '')}"
        for result in results
        if isinstance(result, dict)
    ).lower()
    tool_blob = tool_text.lower()
    raw_quality_hits = [term for term in expected_terms if term in raw_blob]
    tool_quality_hits = [term for term in expected_terms if term in tool_blob]
    used_inline_fallback = tool_text.startswith("OOMKilled (exit code 137)") or tool_text.startswith("No highly relevant docs")

    payload = {
        "ok": retriever.is_loaded() and len(results) > 0 and bool(tool_quality_hits) and not tool_unavailable,
        "elapsed_seconds": round(elapsed, 2),
        "index_path": index_path,
        "error_type": error_type,
        "vector_count": vector_count,
        "document_count": document_count,
        "result_count": len(results),
        "top_scores": [round(float(result.get("score", 0)), 4) for result in results if isinstance(result, dict)],
        "top_sources": [result.get("source", "") for result in results if isinstance(result, dict)],
        "raw_retrieval_quality_hits": raw_quality_hits,
        "tool_response_quality_hits": tool_quality_hits,
        "used_inline_fallback": used_inline_fallback,
        "tool_preview": _preview(tool_text, 800),
    }
    return retriever, payload


def _check_cluster_tool(go_service_url: str, namespace: str, deployment: str) -> dict[str, Any]:
    started = time.perf_counter()
    _set_go_service_url(go_service_url)
    text = get_resource("Deployment", deployment, namespace)
    elapsed = time.perf_counter() - started
    lowered = text.lower()

    return {
        "ok": deployment.lower() in lowered and "not found" not in lowered and "error" not in lowered[:160],
        "elapsed_seconds": round(elapsed, 2),
        "go_service_url": go_service_url,
        "namespace": namespace,
        "deployment": deployment,
        "preview": _preview(text, 800),
    }


def _part_text(part: Any) -> str:
    text = getattr(part, "text", None)
    return text if isinstance(text, str) else ""


async def _run_provider_tool_probe(
    *,
    provider: str,
    api_key: str,
    model_name: str,
    namespace: str,
    deployment: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    session_service = InMemorySessionService()
    if provider in ("gemini", "google"):
        os.environ["GOOGLE_API_KEY"] = api_key
        os.environ.pop("GEMINI_API_KEY", None)

    session = await session_service.create_session(
        app_name=f"live-{provider}-rag-tool-check",
        user_id="live-check",
        state={
            "_provider": provider,
            "_model_name": model_name,
            "_max_tool_calls": 6,
            "_max_tool_response_chars": 3000,
        },
    )

    instruction = f"""
You are a live integration probe. You must use tools before answering.

Step 1: call rag_search with error_type "OOMKilled".
Step 2: call get_resource with kind "Deployment", name "{deployment}", namespace "{namespace}".
Step 3: answer with compact JSON containing keys rag_used, cluster_tool_used, root_cause, evidence.

Do not answer from memory before both required tool responses are available.
"""
    agent = LlmAgent(
        name=f"{provider}_rag_tool_probe",
        model=_create_model(provider, api_key, model_name),
        instruction=instruction,
        tools=[rag_search_tool, get_resource_tool],
        before_model_callback=_before_model_callback,
        after_model_callback=_after_model_callback,
        on_model_error_callback=_on_model_error_callback,
        before_tool_callback=_before_tool_callback,
        after_tool_callback=_after_tool_callback,
        output_key="probe_result",
    )
    runner = Runner(
        app_name=f"live-{provider}-rag-tool-check",
        agent=agent,
        session_service=session_service,
    )

    function_calls: list[dict[str, Any]] = []
    function_responses: list[dict[str, Any]] = []
    text_chunks: list[str] = []

    try:
        async with asyncio.timeout(timeout_seconds):
            async for event in runner.run_async(
                user_id="live-check",
                session_id=session.id,
                new_message=genai_types.Content(
                    role="user",
                    parts=[
                        genai_types.Part(
                            text=(
                                "Run the live probe now. Use the required tools exactly as instructed, "
                                "then return the compact JSON."
                            )
                        )
                    ],
                ),
            ):
                content = getattr(event, "content", None)
                for part in getattr(content, "parts", []) or []:
                    if getattr(part, "function_call", None):
                        call = part.function_call
                        function_calls.append(
                            {
                                "name": call.name,
                                "args": dict(call.args or {}),
                            }
                        )
                    if getattr(part, "function_response", None):
                        response = part.function_response
                        function_responses.append(
                            {
                                "name": response.name,
                                "preview": _preview(response.response, 600),
                            }
                        )
                    text = _part_text(part)
                    if text:
                        text_chunks.append(text)
    except BaseException as error:  # noqa: BLE001 - diagnostics must preserve provider failures.
        payload = _exception_payload(error)
        payload.update(
            {
                "provider": provider,
                "model": model_name,
                "elapsed_seconds": round(time.perf_counter() - started, 2),
                "function_calls": function_calls,
                "function_responses": function_responses,
            }
        )
        return payload

    tool_names = {call["name"] for call in function_calls}
    required_tools = {"rag_search", "get_resource"}
    final_session = await session_service.get_session(
        app_name=f"live-{provider}-rag-tool-check",
        user_id="live-check",
        session_id=session.id,
    )
    usage = dict(final_session.state.get("_usage", {})) if final_session else {}
    state_probe_result = final_session.state.get("probe_result") if final_session else None
    final_text = "\n".join(text_chunks).strip()

    return {
        "ok": required_tools.issubset(tool_names),
        "kind": "completed" if required_tools.issubset(tool_names) else "missing_required_tool_call",
        "provider": provider,
        "model": model_name,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "required_tools": sorted(required_tools),
        "function_calls": function_calls,
        "function_responses": function_responses,
        "usage": usage,
        "final_text_preview": _preview(final_text or str(state_probe_result), 1000),
    }


async def _run_provider_matrix(args: argparse.Namespace) -> dict[str, Any]:
    providers: dict[str, Any] = {}

    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    if openrouter_key:
        providers["openrouter"] = [
            await _run_provider_tool_probe(
                provider="openrouter",
                api_key=openrouter_key,
                model_name=args.openrouter_model,
                namespace=args.namespace,
                deployment=args.deployment,
                timeout_seconds=args.timeout_seconds,
            )
        ]
    else:
        providers["openrouter"] = [{"ok": False, "kind": "missing_api_key"}]

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        gemini_models = [model.strip() for model in args.gemini_models.split(",") if model.strip()]
        providers["gemini"] = []
        for model_name in gemini_models:
            result = await _run_provider_tool_probe(
                provider="gemini",
                api_key=gemini_key,
                model_name=model_name,
                namespace=args.namespace,
                deployment=args.deployment,
                timeout_seconds=args.timeout_seconds,
            )
            providers["gemini"].append(result)
            if result.get("ok"):
                break
    else:
        providers["gemini"] = [{"ok": False, "kind": "missing_api_key"}]

    return providers


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rag-index-path",
        default=os.environ.get("RAG_INDEX_PATH", "rag_data/vector_db"),
        help="Path to the extracted FAISS vector_db directory.",
    )
    parser.add_argument(
        "--go-service-url",
        default=os.environ.get("GO_SERVICE_URL", "http://localhost:8080"),
        help="Go API service URL used by Kubernetes tools.",
    )
    parser.add_argument("--namespace", default=os.environ.get("CHECK_NAMESPACE", "default"))
    parser.add_argument("--deployment", default=os.environ.get("CHECK_DEPLOYMENT", "demo-oomkilled"))
    parser.add_argument(
        "--openrouter-model",
        default=os.environ.get("OPENROUTER_MODEL", "openai/gpt-oss-20b:free"),
    )
    parser.add_argument(
        "--gemini-models",
        default=os.environ.get("GEMINI_MODELS", ",".join(DEFAULT_GEMINI_MODELS)),
        help="Comma-separated Gemini models to try in order.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("CHECK_TIMEOUT_SECONDS", "180")))
    return parser.parse_args()


async def _amain() -> int:
    args = _parse_args()
    started = time.perf_counter()

    output: dict[str, Any] = {
        "rag": {},
        "cluster_tool": {},
        "providers": {},
        "summary": {},
    }

    try:
        _retriever, output["rag"] = _load_rag(
            args.rag_index_path,
            "OOMKilled",
        )
    except BaseException as error:  # noqa: BLE001 - diagnostics should report, not hide.
        output["rag"] = _exception_payload(error)

    try:
        output["cluster_tool"] = _check_cluster_tool(args.go_service_url, args.namespace, args.deployment)
    except BaseException as error:  # noqa: BLE001
        output["cluster_tool"] = _exception_payload(error)

    output["providers"] = await _run_provider_matrix(args)

    provider_ok = {
        name: any(result.get("ok") for result in attempts)
        for name, attempts in output["providers"].items()
    }
    output["summary"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "rag_ok": bool(output["rag"].get("ok")),
        "rag_used_inline_fallback": bool(output["rag"].get("used_inline_fallback")),
        "cluster_tool_ok": bool(output["cluster_tool"].get("ok")),
        "provider_ok": provider_ok,
        "all_ok": bool(output["rag"].get("ok"))
        and bool(output["cluster_tool"].get("ok"))
        and all(provider_ok.values()),
    }

    print(json.dumps(output, indent=2, sort_keys=True))

    if output["summary"]["all_ok"]:
        return 0

    known_provider_block = False
    for attempts in output["providers"].values():
        for result in attempts:
            if result.get("kind") in {"quota_or_rate_limit", "provider_unavailable"}:
                known_provider_block = True

    if known_provider_block and output["summary"]["rag_ok"] and output["summary"]["cluster_tool_ok"]:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
