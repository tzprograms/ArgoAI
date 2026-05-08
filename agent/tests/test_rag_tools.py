"""Tests for RAG search relevance guards."""

from types import SimpleNamespace

from agent import main
from agent.rag import retriever as retriever_module
from agent.tools import rag_tools


class FakeRetriever:
    def __init__(self, results):
        self.results = results
        self.queries = []

    def search(self, query, top_k=3):
        self.queries.append((query, top_k))
        return self.results


def test_rag_search_falls_back_when_high_score_doc_lacks_expected_terms(monkeypatch):
    """High vector similarity alone should not feed off-topic docs to the model."""
    retriever = FakeRetriever([
        {
            "source": "7134627.md",
            "title": "",
            "content": "Back-off restarting failed container argocd-application-controller.",
            "score": 0.91,
        }
    ])
    monkeypatch.setattr(rag_tools, "retriever_instance", retriever)

    result = rag_tools.rag_search("OOMKilled")

    assert result.startswith("OOMKilled (exit code 137)")
    assert "argocd-application-controller" not in result


def test_rag_search_canonicalizes_long_error_text(monkeypatch):
    """Models often pass a sentence; known errors should still use mapped queries."""
    retriever = FakeRetriever([
        {
            "source": "oom.md",
            "title": "",
            "content": "OOMKilled exit code 137 means the container exceeded its memory limit.",
            "score": 0.88,
        }
    ])
    monkeypatch.setattr(rag_tools, "retriever_instance", retriever)

    result = rag_tools.rag_search("OOMKilled exit code 137 Kubernetes memory limit")

    assert retriever.queries == [
        ("container memory limit exceeded OOMKilled exit code 137 increase resources", 3)
    ]
    assert "[oom.md]" in result
    assert "exceeded its memory limit" in result


def test_rag_search_snippet_keeps_matched_evidence(monkeypatch):
    """Returned snippets should show the reason a document was considered relevant."""
    retriever = FakeRetriever([
        {
            "source": "long.md",
            "title": "",
            "content": ("intro " * 90) + "OOMKilled exit code 137 means memory limit exceeded.",
            "score": 0.9,
        }
    ])
    monkeypatch.setattr(rag_tools, "retriever_instance", retriever)

    result = rag_tools.rag_search("OOMKilled")

    assert "OOMKilled exit code 137" in result


def test_init_rag_wires_retriever_into_tool_module(monkeypatch):
    """Health-loaded RAG should be the same instance the rag_search tool uses."""
    created = object()

    class FakeRAGRetriever:
        _index = SimpleNamespace(ntotal=7)

        def __init__(self, index_path):
            assert index_path == "fake-index"

        def is_loaded(self):
            return True

    monkeypatch.setattr(main.os.path, "isdir", lambda path: path == "fake-index")
    monkeypatch.setattr(main, "RAG_INDEX_PATH", "fake-index")
    monkeypatch.setattr(retriever_module, "RAGRetriever", FakeRAGRetriever)
    monkeypatch.setattr(retriever_module, "retriever_instance", created)
    monkeypatch.setattr(rag_tools, "retriever_instance", None)
    monkeypatch.setattr(main, "_rag_loaded", False)

    main._init_rag()

    assert isinstance(retriever_module.retriever_instance, FakeRAGRetriever)
    assert rag_tools.retriever_instance is retriever_module.retriever_instance
    assert main._rag_loaded is True
