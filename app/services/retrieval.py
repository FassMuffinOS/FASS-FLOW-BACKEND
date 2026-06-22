"""
Lightweight RAG retrieval — TF-IDF cosine similarity, zero extra dependencies.

Why not embeddings: the corpus here is a single user's past-performance
references (typically 1-10 short entries), re-ranked per proposal section.
That's a handful of short documents, refreshed rarely, queried once per
draft request — paying for an embeddings API call (latency + $ + another
provider dependency) buys nothing a few hundred microseconds of pure-Python
TF-IDF doesn't already give you at this corpus size. This is the right
tool for the job, not a workaround: swap this module for a real vector
store the moment the corpus stops being "one company's contract history."

Retrieval grounds the LLM draft in the user's *actual* past performance
record instead of letting the model invent plausible-sounding experience —
the standard RAG motivation, just sized to the problem.
"""
import math
import re
from collections import Counter

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has",
    "in", "is", "it", "its", "of", "on", "or", "that", "the", "to", "was",
    "will", "with", "this", "we", "our", "their", "they", "shall", "must",
    "include", "including", "i", "ii", "iii", "iv", "v",
}


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 2]


def _tf(tokens: list[str]) -> Counter:
    return Counter(tokens)


def _cosine(vec_a: dict, vec_b: dict) -> float:
    common = set(vec_a) & set(vec_b)
    if not common:
        return 0.0
    dot = sum(vec_a[t] * vec_b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def rank_passages(query: str, passages: list[str], top_k: int = 3) -> list[dict]:
    """Rank `passages` by relevance to `query`. Returns
    [{index, score, text}, ...] sorted descending, capped at top_k.
    Passages with zero overlap are dropped rather than padded in —
    a low-signal match shouldn't be forced into the prompt."""
    if not passages:
        return []

    docs = passages + [query]
    tokenized = [_tokenize(d) for d in docs]

    doc_count = len(docs)
    df = Counter()
    for tokens in tokenized:
        for term in set(tokens):
            df[term] += 1
    idf = {term: math.log((doc_count + 1) / (count + 1)) + 1 for term, count in df.items()}

    def tfidf_vector(tokens: list[str]) -> dict:
        tf = _tf(tokens)
        total = sum(tf.values()) or 1
        return {term: (count / total) * idf.get(term, 0.0) for term, count in tf.items()}

    vectors = [tfidf_vector(t) for t in tokenized]
    query_vec = vectors[-1]
    passage_vecs = vectors[:-1]

    scored = [
        {"index": i, "score": _cosine(query_vec, pv), "text": passages[i]}
        for i, pv in enumerate(passage_vecs)
    ]
    scored = [s for s in scored if s["score"] > 0]
    scored.sort(key=lambda s: s["score"], reverse=True)
    return scored[:top_k]
