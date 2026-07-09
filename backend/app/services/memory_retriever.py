"""Memory Retriever — abstract interface + multi-source implementations.

When long-term memory count > 50 or recall rate drops, enable HybridRetriever:
    retriever = HybridRetriever([
        ChromaDBRetriever(weight=0.7),
        MySQLKeywordRetriever(weight=0.3),
    ])
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from app.services.rag import rag_service


@dataclass
class MemoryResult:
    """A single memory search result."""
    content: str
    score: float          # 0.0 ~ 1.0
    source: str           # "chromadb" | "mysql" | ...
    tier: str = "long"    # "long" | "medium"
    memory_id: str = ""   # DB id for dedup/reference


class MemoryRetriever(ABC):
    """Base interface for memory retrieval backends.

    Implementations:
      - ChromaDBRetriever: semantic search via bge-m3 embeddings
      - MySQLKeywordRetriever: LIKE-based keyword search (for identity/fact matching)
      - HybridRetriever: weighted merge of multiple retrievers
    """

    @abstractmethod
    async def search(
        self, query: str, top_k: int = 8, min_score: float = 0.15
    ) -> list[MemoryResult]:
        """Search for memories matching the query."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this retriever, used in logging and weighting."""
        ...


class ChromaDBRetriever(MemoryRetriever):
    """Semantic search via ChromaDB + bge-m3 embeddings. (default, already working)"""

    @property
    def name(self) -> str:
        return "chromadb"

    async def search(
        self, query: str, top_k: int = 8, min_score: float = 0.15
    ) -> list[MemoryResult]:
        tiered = rag_service.search_by_tier(query, top_k=top_k, min_score=min_score)
        results = []
        for tier_name, items in tiered.items():
            for text, score in items:
                if score >= min_score:
                    results.append(MemoryResult(
                        content=text, score=score, source="chromadb", tier=tier_name,
                    ))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]


class MySQLKeywordRetriever(MemoryRetriever):
    """Keyword-based search on MySQL Memory table. Activated when ChromaDB misses identity/fact queries.

    Usage (future):
        retriever = MySQLKeywordRetriever(db_session_factory)
        results = await retriever.search("你是谁", top_k=5)
    """

    def __init__(self):
        self._enabled = True

    @property
    def name(self) -> str:
        return "mysql"

    async def search(
        self, query: str, top_k: int = 5, min_score: float = 0.2
    ) -> list[MemoryResult]:
        """Split query into CJK keywords, search MySQL Memory table via LIKE."""
        from app.db.session import async_session
        from app.models.chat import Memory
        from sqlalchemy import or_, select

        # Extract meaningful CJK characters as keywords
        keywords = [c for c in query if '一' <= c <= '鿿']
        if not keywords:
            return []

        # Build conditions: each keyword matched against content
        conditions = [Memory.content.contains(kw) for kw in keywords[:6]]

        async with async_session() as db:
            result = await db.execute(
                select(Memory)
                .where(
                    Memory.is_deleted == False,
                    Memory.is_faded == False,
                    or_(*conditions),
                )
                .order_by(Memory.confidence.desc())
                .limit(top_k)
            )
            memories = result.scalars().all()

        results = []
        for m in memories:
            # Simple relevance score: keyword hit ratio
            hit_count = sum(1 for kw in keywords if kw in m.content)
            score = min(0.8, 0.3 + hit_count / max(len(keywords), 1) * 0.5)
            results.append(MemoryResult(
                content=m.content, score=score, source="mysql",
                tier="long", memory_id=m.id,
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]


class HybridRetriever(MemoryRetriever):
    """Weighted merge of multiple retrievers. Deduplicates by content similarity.

    Usage (when needed):
        retriever = HybridRetriever([
            ChromaDBRetriever(weight=0.7),
            MySQLKeywordRetriever(weight=0.3),
        ])
    """

    def __init__(self, retrievers: list[tuple[MemoryRetriever, float]]):
        """
        Args:
            retrievers: list of (retriever, weight) tuples. Weights should sum to ~1.0.
        """
        self._retrievers = retrievers

    @property
    def name(self) -> str:
        return "hybrid"

    async def search(
        self, query: str, top_k: int = 8, min_score: float = 0.15
    ) -> list[MemoryResult]:
        # Run all retrievers concurrently
        import asyncio
        all_results: list[MemoryResult] = []

        async def fetch(retriever: MemoryRetriever, weight: float):
            results = await retriever.search(query, top_k=top_k, min_score=min_score)
            for r in results:
                r.score *= weight  # Apply weight
            return results

        tasks = [fetch(r, w) for r, w in self._retrievers]
        batches = await asyncio.gather(*tasks, return_exceptions=True)

        for batch in batches:
            if isinstance(batch, list):
                all_results.extend(batch)

        # Deduplicate by content (keep highest weighted score)
        seen = {}
        for r in sorted(all_results, key=lambda x: x.score, reverse=True):
            key = r.content[:60]  # first 60 chars as dedup key
            if key not in seen or r.score > seen[key].score:
                seen[key] = r

        merged = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        return merged[:top_k]
