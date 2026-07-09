"""RAG service — 3-tier memory: short(实时), medium(ChromaDB+TTL), long(ChromaDB永久)."""
import json
import logging
from datetime import datetime, timedelta
from chromadb import PersistentClient
from chromadb.api.types import EmbeddingFunction as ChromaEmbeddingFunction
from chromadb.utils import embedding_functions
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger("uvicorn")


class OllamaEmbedding(ChromaEmbeddingFunction):
    def __init__(self):
        import httpx
        self._client = httpx.Client(timeout=30)
        self._base = settings.ollama_base_url
        self._model = settings.ollama_embed_model
        self._available = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                r = self._client.get(f"{self._base}/api/tags", timeout=5)
                self._available = r.status_code == 200
            except Exception:
                self._available = False
            logger.info(f"Ollama available: {self._available}")
        return self._available

    def __call__(self, input: list[str]) -> list[list[float]]:
        embeddings = []
        for text in input:
            r = self._client.post(
                f"{self._base}/api/embeddings",
                json={"model": self._model, "prompt": text},
            )
            r.raise_for_status()
            emb = json.loads(r.text)["embedding"]
            logger.info(f"Ollama embed: len={len(emb)}, sample={emb[:5]}, text='{text[:30]}...'")
            embeddings.append(emb)
        return embeddings


class RagService:
    def __init__(self):
        self._client = None
        self._collection = None
        self._ollama = None

    @property
    def ollama(self):
        if self._ollama is None:
            self._ollama = OllamaEmbedding()
        return self._ollama

    @property
    def client(self):
        if self._client is None:
            self._client = PersistentClient(path="./chroma_data")
        return self._client

    @property
    def collection(self):
        if self._collection is None:
            if self.ollama.available:
                ef = self.ollama
                logger.info("RAG using Ollama bge-m3")
            else:
                ef = embedding_functions.DefaultEmbeddingFunction()
                logger.warning("RAG using default embedding")

            self._collection = self.client.get_or_create_collection(
                name="conversation_memory_v4",
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    # ── Write ──

    def add_memory(self, message_id: str, content: str, role: str,
                   tier: str = "medium", ttl_days: int = 7):
        if len(content) < 4:
            return
        meta = {"role": role, "tier": tier}
        if tier == "medium" and ttl_days > 0:
            meta["expires_at"] = (datetime.utcnow() + timedelta(days=ttl_days)).isoformat()
        try:
            self.collection.add(documents=[content], metadatas=[meta], ids=[message_id])
        except Exception as e:
            logger.warning(f"add_memory failed: {e}")

    def remove_memory(self, message_id: str):
        try:
            self.collection.delete(ids=[message_id])
        except Exception:
            pass

    def clear_all(self):
        try:
            self.client.delete_collection("conversation_memory_v4")
            self._collection = None
            return True
        except Exception:
            return False

    # ── Search ──

    def search_with_scores(self, query: str, top_k: int = 5) -> list[tuple[str, float, str]]:
        """Retrieve with (text, score, chroma_id)."""
        try:
            results = self.collection.query(
                query_texts=[query], n_results=top_k,
                include=["documents", "distances"],
            )
            docs = results.get("documents", [[]])[0]
            dists = results.get("distances", [[]])[0]
            ids = results.get("ids", [[]])[0]
            return [
                (d, max(0.0, 1.0 - min(dist, 2.0) / 2.0), rid)
                for d, dist, rid in zip(docs, dists, ids) if d
            ]
        except Exception:
            return []

    def search_by_category(self, query: str, category: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Search within a specific category. Returns [(text, score), ...]."""
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=top_k,
                where={"category": category},
                include=["documents", "distances"],
            )
            docs = results.get("documents", [[]])[0]
            dists = results.get("distances", [[]])[0]
            return [
                (d, max(0.0, 1.0 - min(dist, 2.0) / 2.0))
                for d, dist in zip(docs, dists) if d
            ]
        except Exception:
            return []

    def search_by_tier(self, query: str, top_k: int = 8,
                       min_score: float = 0.3) -> dict[str, list[tuple[str, float]]]:
        result = {"medium": [], "long": []}
        try:
            # DEBUG: check collection size
            count = self.collection.count()
            logger.info(f"RAG DEBUG: ChromaDB total documents = {count}")
            results = self.collection.query(
                query_texts=[query],
                n_results=top_k * 2,
                include=["documents", "distances", "metadatas"],
            )
            docs = results.get("documents", [[]])[0]
            dists = results.get("distances", [[]])[0]
            metas = results.get("metadatas", [[]])[0]

            for d, dist, m in zip(docs, dists, metas):
                if not d or not m:
                    continue
                score = max(0.0, 1.0 - min(dist, 2.0) / 2.0)
                tier = m.get("tier", "medium")
                if score < min_score:
                    continue
                result[tier].append((d, score))

            # Sort by score descending within each tier
            result["medium"].sort(key=lambda x: x[1], reverse=True)
            result["long"].sort(key=lambda x: x[1], reverse=True)
        except Exception:
            pass

        return result

    # ── Maintenance ──

    def cleanup_expired(self) -> int:
        try:
            now = datetime.utcnow().isoformat()
            results = self.collection.get(include=["metadatas"])
            ids = results.get("ids", [])
            metas = results.get("metadatas", [])
            expired = [rid for rid, m in zip(ids, metas)
                       if m and m.get("expires_at") and m["expires_at"] < now]
            if expired:
                self.collection.delete(ids=expired)
                logger.info(f"Cleanup: removed {len(expired)} expired")
            return len(expired)
        except Exception as e:
            logger.warning(f"Cleanup failed: {e}")
            return 0


rag_service = RagService()
