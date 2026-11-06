from agentkit.retrieval.bm25 import Bm25Document, bm25_scores
from agentkit.retrieval.tokenize import tokenize


def test_tokenize_keeps_ascii_terms_and_cjk_bigrams() -> None:
    assert tokenize("PostgreSQL 多租户") == ["postgresql", "多租", "租户"]


def test_bm25_prefers_name_hits_and_returns_empty_for_unknown_terms() -> None:
    docs = [
        Bm25Document(
            key="a",
            counts={"github": 1},
            length=1,
            head_tokens=frozenset({"github"}),
        ),
        Bm25Document(
            key="b",
            counts={"github": 1},
            length=20,
            head_tokens=frozenset(),
        ),
    ]
    scores = bm25_scores(
        docs,
        query_terms={"github": 1.0},
        k1=1.2,
        b=0.75,
        name_weight=3.0,
    )
    assert scores["a"] > scores["b"] > 0
    assert bm25_scores(docs, query_terms={"missing": 1.0}) == {}
