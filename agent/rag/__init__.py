"""RAG pipeline: LlamaIndex vector search with pre-built FAISS index."""

__all__ = [
    "RAGRetriever",
    "chunk_document",
    "chunk_directory",
]

def __getattr__(name):
    if name == "RAGRetriever":
        from agent.rag.retriever import RAGRetriever
        return RAGRetriever
    if name in ("chunk_document", "chunk_directory"):
        from agent.rag import chunker
        return getattr(chunker, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
