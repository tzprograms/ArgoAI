# RAG Retriever using pre-built FAISS index from Lightspeed BYOK
#
# PRODUCTION NOTES:
# - Model is loaded from local cache if available (for air-gapped/restricted clusters)
# - Set TRANSFORMERS_CACHE and HF_HOME env vars to writable paths
# - For fully offline operation, pre-download model into Docker image

import json
import os
import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_INDEX_PATH = "/rag/vector_db"
DEFAULT_MODEL_NAME = "sentence-transformers/all-mpnet-base-v2"


def _configure_cache_paths():
    """Configure HuggingFace/SentenceTransformers cache to writable paths.

    This is critical for:
    - Read-only root filesystem in production
    - Air-gapped/restricted clusters
    - Reproducible builds
    """
    # Default to /.cache if not set (must be mounted as emptyDir in K8s)
    cache_dir = os.environ.get("TRANSFORMERS_CACHE", "/.cache")

    # Set all relevant cache environment variables
    cache_vars = [
        "TRANSFORMERS_CACHE",
        "HF_HOME",
        "HF_HUB_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
    ]

    for var in cache_vars:
        if var not in os.environ:
            os.environ[var] = cache_dir

    # Ensure cache directory exists
    os.makedirs(cache_dir, exist_ok=True)

    return cache_dir


class RAGRetriever:
    """Retriever that loads a pre-built FAISS index + docstore from Lightspeed BYOK.

    Production-safe features:
    - Configurable cache paths for restricted clusters
    - Graceful degradation if model unavailable
    - Pre-bundled model support via environment variables
    """

    def __init__(self, index_path: str = DEFAULT_INDEX_PATH, model_name: str = DEFAULT_MODEL_NAME):
        self.index_path = index_path
        self.model_name = model_name
        self._index = None
        self._docstore = None
        self._index_to_docid = None
        self._embed_model = None

        # Configure cache paths before loading anything
        self._cache_dir = _configure_cache_paths()

        self._load_index()

    def _load_index(self):
        """Load the FAISS index and docstore from disk."""
        if not os.path.isdir(self.index_path):
            logger.warning(f"Index path not found: {self.index_path}. RAG will be disabled.")
            return

        try:
            import faiss

            logger.info(f"Loading RAG index from {self.index_path}")

            # Load FAISS index
            index_file = os.path.join(self.index_path, "default__vector_store.json")
            if not os.path.exists(index_file):
                logger.warning(f"FAISS index file not found: {index_file}")
                return

            self._index = faiss.read_index(index_file)
            logger.info(f"FAISS index loaded: {self._index.ntotal} vectors")

            # Load docstore (node ID -> content mapping)
            docstore_file = os.path.join(self.index_path, "docstore.json")
            if os.path.exists(docstore_file):
                with open(docstore_file, "r") as f:
                    docstore_data = json.load(f)
                self._docstore = docstore_data.get("docstore/data", {})
                logger.info(f"Docstore loaded: {len(self._docstore)} documents")

            # Load index_store to get node ID mapping
            index_store_file = os.path.join(self.index_path, "index_store.json")
            if os.path.exists(index_store_file):
                with open(index_store_file, "r") as f:
                    index_store = json.load(f)

                # Extract the nodes_dict mapping (index position -> node ID)
                index_data = index_store.get("index_store/data", {})
                for key, value in index_data.items():
                    if isinstance(value, dict) and "__data__" in value:
                        data = json.loads(value["__data__"])
                        self._index_to_docid = data.get("nodes_dict", {})
                        break

            if not self._index_to_docid:
                logger.warning("Could not find nodes_dict in index_store")

            # Load embedding model with offline support
            self._load_embedding_model()

        except Exception as e:
            logger.error(f"Failed to load RAG index: {e}")
            self._index = None

    def _load_embedding_model(self):
        """Load embedding model with offline/restricted cluster support.

        Tries:
        1. Pre-bundled model at SENTENCE_TRANSFORMERS_HOME
        2. Cached model at TRANSFORMERS_CACHE
        3. Download from HuggingFace Hub (if network available)
        """
        try:
            from sentence_transformers import SentenceTransformer

            # Check for pre-bundled model path
            bundled_path = os.environ.get("RAG_MODEL_PATH", "")
            if bundled_path and os.path.isdir(bundled_path):
                logger.info(f"Loading pre-bundled embedding model from {bundled_path}")
                self._embed_model = SentenceTransformer(bundled_path)
            else:
                # Load from cache or download
                logger.info(f"Loading embedding model: {self.model_name}")
                logger.info(f"Cache directory: {self._cache_dir}")
                self._embed_model = SentenceTransformer(self.model_name)

            logger.info("Embedding model loaded successfully")

        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            logger.warning("RAG search will be disabled. To fix:")
            logger.warning("  1. Set RAG_MODEL_PATH to a pre-downloaded model directory")
            logger.warning("  2. Or ensure network access to HuggingFace Hub")
            logger.warning("  3. Or pre-bake the model into the container image")
            self._embed_model = None

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Search the index and return top_k results."""
        if self._index is None or self._embed_model is None:
            return []

        # Embed query
        query_vec = self._embed_model.encode([query], normalize_embeddings=True)
        query_vec = np.array(query_vec, dtype=np.float32)

        # Search FAISS
        scores, indices = self._index.search(query_vec, top_k)

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0:
                continue

            # Map index position to document ID
            doc_id = None
            if self._index_to_docid:
                doc_id = self._index_to_docid.get(str(idx))

            content = ""
            source = ""
            if doc_id and self._docstore:
                doc_data = self._docstore.get(doc_id, {})
                if isinstance(doc_data, dict) and "__data__" in doc_data:
                    node_data = doc_data["__data__"]
                    # __data__ can be dict or JSON string
                    if isinstance(node_data, str):
                        node_data = json.loads(node_data)
                    content = node_data.get("text", "")
                    metadata = node_data.get("metadata", {})
                    source = metadata.get("filename", metadata.get("file_name", ""))

            results.append({
                "source": source,
                "title": "",
                "content": content,
                "score": float(score),
            })

        return results

    def is_loaded(self) -> bool:
        """Check if the index is loaded."""
        return self._index is not None


# Global instance -- initialized in main.py
retriever_instance: Optional[RAGRetriever] = None
