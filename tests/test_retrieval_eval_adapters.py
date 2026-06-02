"""Tests for eval/retrieval/adapters.py."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import sys
import types
from uuid import UUID, uuid4

import pytest

from eval.retrieval.adapters import (
    HybridRetriever,
    IlikeBaselineRetriever,
    SemanticRetriever,
    StubSemanticRetriever,
    message_text,
)
from eval.retrieval.schema import Corpus, CorpusMessage, RankedSourceKey


def _source_ids(results: list[RankedSourceKey]) -> list[str]:
    return [result.source_id for result in results]


def _source_pairs(results: list[RankedSourceKey]) -> list[tuple[str, str, int]]:
    return [(result.source_type, result.source_id, result.rank) for result in results]


def _make_test_corpus() -> Corpus:
    """Build a small but realistic corpus for adapter tests."""
    return Corpus(
        messages=[
            CorpusMessage(
                id="m001",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
                content="The server is down in production",
            ),
            CorpusMessage(
                id="m002",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 5, 1, 10, 1, 0, tzinfo=timezone.utc),
                content="Looking into it now",
            ),
            CorpusMessage(
                id="m003",
                thread_id="thread_b",
                topic_id="topic_y",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 1, 9, 0, 0, tzinfo=timezone.utc),
                content="Did you see the game last night?",
            ),
            CorpusMessage(
                id="m004",
                thread_id="thread_b",
                topic_id="topic_y",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 5, 1, 9, 1, 0, tzinfo=timezone.utc),
                content="Yeah what a finish",
            ),
            # Message with media_analysis signal (content is generic).
            CorpusMessage(
                id="m005",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 2, 8, 0, 0, tzinfo=timezone.utc),
                content="Check this out",
                media_analysis={
                    "explanation": "The database connection pool is exhausting under load - we're seeing connection timeout errors at 200 concurrent users",
                },
            ),
            # Message with only media_analysis.description containing signal.
            CorpusMessage(
                id="m006",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 5, 2, 8, 5, 0, tzinfo=timezone.utc),
                content="Here is the report",
                media_analysis={
                    "description": "CPU utilization spiked to 98% on all four Kubernetes nodes after the load balancer failover",
                },
            ),
            # Message with only media_analysis.summary containing signal.
            CorpusMessage(
                id="m007",
                thread_id="thread_b",
                topic_id="topic_y",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 2, 8, 10, 0, tzinfo=timezone.utc),
                content="Notes from the call",
                media_analysis={
                    "summary": "Decision made to push the deployment to next Tuesday instead of Friday due to the QA backlog",
                },
            ),
            # Two messages with same sent_at to test tiebreaker.
            CorpusMessage(
                id="m008",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Bob",
                recipient="Alice",
                sent_at=datetime(2025, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
                content="first same-time message",
            ),
            CorpusMessage(
                id="m009",
                thread_id="thread_a",
                topic_id="topic_x",
                sender="Alice",
                recipient="Bob",
                sent_at=datetime(2025, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
                content="second same-time message",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# IlikeBaselineRetriever tests
# ---------------------------------------------------------------------------


def test_baseline_finds_verbatim_substring_case_insensitive():
    """Baseline should match case-insensitively on content."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # Exact case
    results = retriever.retrieve("server is down", scope="all", limit=50)
    assert "m001" in _source_ids(results)

    # Different case
    results = retriever.retrieve("SERVER IS DOWN", scope="all", limit=50)
    assert "m001" in _source_ids(results)

    # Mixed case
    results = retriever.retrieve("SeRvEr Is DoWn", scope="all", limit=50)
    assert "m001" in _source_ids(results)


def test_baseline_respects_thread_scope():
    """Thread scope should filter to matching thread_id only."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # thread_a has m001 and m002 matching "server"
    results = retriever.retrieve("server", scope="thread", thread_id="thread_a", limit=50)
    assert "m001" in _source_ids(results)
    assert all(
        corpus.messages[int(result.source_id[1:]) - 1].thread_id == "thread_a" for result in results
    )

    # thread_b should not contain "server"
    results_b = retriever.retrieve("server", scope="thread", thread_id="thread_b", limit=50)
    assert len(results_b) == 0


def test_baseline_respects_topic_scope():
    """Topic scope should filter to matching topic_id only."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # topic_x has m001, m002 with "server"
    results = retriever.retrieve("server", scope="topic", topic_id="topic_x", limit=50)
    assert len(results) >= 1
    assert "m001" in _source_ids(results)

    # topic_y should not contain "server"
    results_b = retriever.retrieve("server", scope="topic", topic_id="topic_y", limit=50)
    assert len(results_b) == 0


def test_baseline_respects_all_scope():
    """All scope should return matches across threads and topics."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    results = retriever.retrieve("the", scope="all", limit=50)
    # "the" appears in m001, m003, m005, m006, m007
    assert len(results) >= 4


def test_baseline_media_analysis_explanation_fallback():
    """Should match on media_analysis.explanation when content is generic."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # "connection timeout" is only in m005's media_analysis.explanation
    results = retriever.retrieve("connection timeout", scope="all", limit=50)
    assert "m005" in _source_ids(results)


def test_baseline_media_analysis_description_fallback():
    """Should match on media_analysis.description when content is generic."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # "kubernetes" is only in m006's media_analysis.description
    results = retriever.retrieve("kubernetes", scope="all", limit=50)
    assert "m006" in _source_ids(results)


def test_baseline_media_analysis_summary_fallback():
    """Should match on media_analysis.summary when content is generic."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # "deployment to next tuesday" is only in m007's media_analysis.summary
    results = retriever.retrieve("deployment to next tuesday", scope="all", limit=50)
    assert "m007" in _source_ids(results)


def test_baseline_paraphrase_only_query_returns_empty():
    """A query that is a paraphrase with no substring overlap returns []."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # "production outage" is a paraphrase of "server is down in production"
    # but the exact substring "production outage" does not appear anywhere.
    results = retriever.retrieve("production outage", scope="all", limit=50)
    assert results == []


def test_baseline_tiebreaker_ordering_is_stable():
    """Messages with same sent_at should be ordered by id DESC as tiebreaker."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # m008 and m009 share the same sent_at and both contain "message"
    results = retriever.retrieve("message", scope="all", limit=50)
    result_ids = _source_ids(results)
    m008_idx = result_ids.index("m008") if "m008" in result_ids else -1
    m009_idx = result_ids.index("m009") if "m009" in result_ids else -1
    # m009 should come before m008 (id DESC tiebreaker)
    assert m009_idx < m008_idx


def test_baseline_limit_truncation():
    """Results should be truncated to the specified limit."""
    corpus = _make_test_corpus()
    retriever = IlikeBaselineRetriever(corpus)

    # Query matching many messages
    results = retriever.retrieve("the", scope="all", limit=2)
    assert len(results) <= 2


# ---------------------------------------------------------------------------
# StubSemanticRetriever tests
# ---------------------------------------------------------------------------


def test_stub_returns_empty_list():
    """Stub retriever should always return [] deterministically."""
    corpus = _make_test_corpus()
    retriever = StubSemanticRetriever(corpus)

    assert retriever.retrieve("anything", scope="all", limit=50) == []
    assert retriever.retrieve("server", scope="thread", thread_id="thread_a", limit=10) == []
    assert retriever.retrieve("", scope="topic", topic_id="topic_x", limit=1) == []
    # Multiple calls return same result
    assert retriever.retrieve("test", scope="all", limit=50) == []
    assert retriever.retrieve("test", scope="all", limit=50) == []


def test_stub_empty_corpus():
    """Stub works even with empty corpus."""
    corpus = Corpus(messages=[])
    retriever = StubSemanticRetriever(corpus)
    assert retriever.retrieve("test", scope="all", limit=50) == []


def test_offline_adapters_return_ranked_message_source_keys() -> None:
    corpus = _make_test_corpus()
    baseline = IlikeBaselineRetriever(corpus)
    stub = StubSemanticRetriever(corpus)

    baseline_results = baseline.retrieve("server", scope="all", limit=5)
    assert _source_pairs(baseline_results)[0] == ("message", "m001", 1)
    assert [result.rank for result in baseline_results] == list(range(1, len(baseline_results) + 1))
    assert stub.retrieve("server", scope="all", limit=5) == []


# ---------------------------------------------------------------------------
# No app.* imports check
# ---------------------------------------------------------------------------


def test_adapters_module_has_no_app_imports():
    """Verify adapters.py does not import anything from app.*"""
    import ast
    from pathlib import Path

    adapters_path = Path(__file__).parent.parent / "eval" / "retrieval" / "adapters.py"
    source = adapters_path.read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(
                    "app."
                ), f"adapters.py imports app.*: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert not node.module.startswith(
                    "app."
                ), f"adapters.py imports from app.*: {node.module}"


def test_only_db_backed_retriever_lazy_imports_production_app_module():
    """Offline adapters stay app-pure; DbBackedRetriever owns the lazy app import."""
    adapters_path = Path(__file__).parent.parent / "eval" / "retrieval" / "adapters.py"
    tree = ast.parse(adapters_path.read_text())

    app_import_call_classes: list[str] = []
    for class_node in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
        for node in ast.walk(class_node):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and isinstance(func.value, ast.Name)
                and func.value.id == "importlib"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith("app.")
            ):
                app_import_call_classes.append(class_node.name)

    assert app_import_call_classes == ["DbBackedRetriever"]


def test_db_retriever_direct_database_env_name_matches_settings_contract():
    """The eval DB adapter and app Settings must use the exact same env name."""
    from app.config import Settings
    from eval.retrieval.adapters import DbBackedRetriever

    adapters_path = Path(__file__).parent.parent / "eval" / "retrieval" / "adapters.py"
    config_path = Path(__file__).parent.parent / "app" / "config.py"

    adapter_source = adapters_path.read_text()
    config_source = config_path.read_text()

    assert 'os.environ.get("DIRECT_DATABASE_URL")' in adapter_source
    assert 'os.environ.get("DATABASE_URL")' not in adapter_source
    assert "direct_database_url: str | None = None" in config_source
    assert Settings.model_fields["direct_database_url"].validation_alias is None
    assert DbBackedRetriever.__name__ in adapter_source


def test_eval_docs_pin_db_adapter_boundary_and_m1_gate_diagnostics():
    """Docs keep the DB eval adapter and M1 gate contracts visible."""
    readme = (Path(__file__).parent.parent / "eval" / "retrieval" / "README.md").read_text()
    adapters = (Path(__file__).parent.parent / "eval" / "retrieval" / "adapters.py").read_text()

    assert "DbBackedRetriever" in readme
    assert "DIRECT_DATABASE_URL" in readme
    assert "--assert-m1-gate" in readme
    assert "M1 gate FAILED" in readme
    assert "no\nM1 tool consumer depends on production retrieval beyond eval" in adapters


# ---------------------------------------------------------------------------
# message_text helper
# ---------------------------------------------------------------------------


def test_message_text_concatenates_media_in_field_order():
    """message_text appends media_analysis fields in explanation/description/summary order."""
    msg = CorpusMessage(
        id="m001",
        thread_id="t",
        topic_id="x",
        sender="A",
        recipient="B",
        sent_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
        content="See attached",
        media_analysis={"summary": "SUM", "explanation": "EXP", "description": "DESC"},
    )
    assert message_text(msg) == "See attached EXP DESC SUM"


def test_message_text_plain_content():
    msg = CorpusMessage(
        id="m001",
        thread_id="t",
        topic_id="x",
        sender="A",
        recipient="B",
        sent_at=datetime(2025, 5, 1, tzinfo=timezone.utc),
        content="just content",
    )
    assert message_text(msg) == "just content"


# ---------------------------------------------------------------------------
# SemanticRetriever / HybridRetriever
#
# These require the local MiniLM model + numpy + sentence-transformers. If the
# backend is unavailable (e.g. no cached model / no deps), the tests skip so the
# core harness suite still runs dependency-free.
# ---------------------------------------------------------------------------


def _semantic_or_skip(corpus: Corpus):
    try:
        return SemanticRetriever(corpus)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"semantic backend unavailable: {exc}")


def test_semantic_finds_paraphrase_baseline_misses():
    """Semantic should retrieve a meaning-match the ILIKE baseline cannot."""
    corpus = _make_test_corpus()
    sem = _semantic_or_skip(corpus)
    # "production outage" is a paraphrase of m001 "The server is down in production".
    results = sem.retrieve("production outage", scope="all", limit=5)
    assert "m001" in _source_ids(results)
    # Baseline returns [] for the same query (verified in another test).
    assert IlikeBaselineRetriever(corpus).retrieve("production outage", scope="all", limit=5) == []


def test_semantic_respects_thread_scope():
    corpus = _make_test_corpus()
    sem = _semantic_or_skip(corpus)
    results = sem.retrieve("anything", scope="thread", thread_id="thread_b", limit=50)
    assert all(
        corpus.messages[int(result.source_id[1:]) - 1].thread_id == "thread_b"
        for result in results
    )


def test_semantic_is_deterministic():
    corpus = _make_test_corpus()
    sem = _semantic_or_skip(corpus)
    a = sem.retrieve("server down", scope="all", limit=10)
    b = sem.retrieve("server down", scope="all", limit=10)
    assert a == b


def test_semantic_limit_truncation():
    corpus = _make_test_corpus()
    sem = _semantic_or_skip(corpus)
    results = sem.retrieve("the", scope="all", limit=3)
    assert len(results) <= 3


def test_hybrid_fuses_baseline_and_semantic():
    """Hybrid should return a non-empty fused ranking covering both rankers' hits."""
    corpus = _make_test_corpus()
    try:
        hyb = HybridRetriever(corpus)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"semantic backend unavailable: {exc}")
    # Verbatim hit (baseline) plus paraphrase ability (semantic).
    results = hyb.retrieve("server is down", scope="all", limit=10)
    assert "m001" in _source_ids(results)
    # Deterministic.
    assert results == hyb.retrieve("server is down", scope="all", limit=10)


def test_hybrid_respects_scope():
    corpus = _make_test_corpus()
    try:
        hyb = HybridRetriever(corpus)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"semantic backend unavailable: {exc}")
    results = hyb.retrieve("game", scope="topic", topic_id="topic_y", limit=50)
    assert all(
        corpus.messages[int(result.source_id[1:]) - 1].topic_id == "topic_y"
        for result in results
    )


# ---------------------------------------------------------------------------
# DbBackedRetriever tests (T12)
# ---------------------------------------------------------------------------


def test_db_retriever_requires_env_var():
    """DbBackedRetriever raises ValueError without DIRECT_DATABASE_URL."""
    import os

    from eval.retrieval.adapters import DbBackedRetriever

    corpus = _make_test_corpus()
    # Ensure the env var is NOT set for this test.
    old = os.environ.pop("DIRECT_DATABASE_URL", None)
    try:
        with pytest.raises(ValueError) as exc_info:
            DbBackedRetriever(corpus)
        assert "DIRECT_DATABASE_URL" in str(exc_info.value)
    finally:
        if old is not None:
            os.environ["DIRECT_DATABASE_URL"] = old


@pytest.mark.skipif(
    not __import__("os").environ.get("DIRECT_DATABASE_URL"),
    reason="DIRECT_DATABASE_URL not set — skipping live DB test",
)
def test_db_retriever_construction_succeeds_with_env():
    """DbBackedRetriever constructs successfully when env var is set."""
    import os

    from eval.retrieval.adapters import DbBackedRetriever

    corpus = _make_test_corpus()
    # Should not raise.
    retriever = DbBackedRetriever(corpus)
    assert retriever is not None


@pytest.mark.skipif(
    not __import__("os").environ.get("DIRECT_DATABASE_URL"),
    reason="DIRECT_DATABASE_URL not set — skipping live DB test",
)
def test_db_retriever_retrieve_returns_results():
    """DbBackedRetriever.retrieve() returns ids on a simple query."""
    from eval.retrieval.adapters import DbBackedRetriever

    corpus = _make_test_corpus()
    retriever = DbBackedRetriever(corpus)
    # A query that should find something in the real db.
    results = retriever.retrieve(
        "test", scope="all", limit=5
    )
    # Results should be ranked source-key objects.
    assert isinstance(results, list)
    if results:
        assert all(isinstance(r, RankedSourceKey) for r in results)
        assert len(results) <= 5


def test_db_retriever_lazy_imports_production_retrieval_and_preserves_sync_api(
    monkeypatch: pytest.MonkeyPatch,
):
    """DbBackedRetriever bridges sync eval calls to async production retrieval."""
    from eval.retrieval.adapters import DbBackedRetriever

    calls: list[object] = []
    pool_closed = False
    viewer_id = uuid4()
    topic_id = uuid4()
    message_id = uuid4()

    class FakePool:
        async def close(self) -> None:
            nonlocal pool_closed
            pool_closed = True

    fake_asyncpg = types.ModuleType("asyncpg")

    async def create_pool(database_url: str, **kwargs: object) -> FakePool:
        calls.append(("create_pool", database_url, kwargs))
        init = kwargs.get("init")
        if init is not None:
            await init(object())
        return FakePool()

    fake_asyncpg.create_pool = create_pool  # type: ignore[attr-defined]

    fake_pgvector_asyncpg = types.ModuleType("pgvector.asyncpg")

    async def register_vector(conn: object) -> None:
        calls.append(("register_vector", conn))

    fake_pgvector_asyncpg.register_vector = register_vector  # type: ignore[attr-defined]

    fake_retrieval = types.ModuleType("app.services.retrieval")

    class RetrievalQuery:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)
            calls.append(("query", kwargs))

    async def hybrid_search(
        pool: object, request: object, **kwargs: object
    ) -> list[object]:
        calls.append(("hybrid_search", pool, request, kwargs))
        return [types.SimpleNamespace(message_id=message_id)]

    fake_retrieval.RetrievalQuery = RetrievalQuery  # type: ignore[attr-defined]
    fake_retrieval.hybrid_search = hybrid_search  # type: ignore[attr-defined]

    monkeypatch.setenv("DIRECT_DATABASE_URL", "postgresql://direct.example/db")
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    monkeypatch.setitem(sys.modules, "pgvector.asyncpg", fake_pgvector_asyncpg)
    monkeypatch.setitem(sys.modules, "app.services.retrieval", fake_retrieval)

    retriever = DbBackedRetriever(_make_test_corpus())
    try:
        results = retriever.retrieve(
            "quoted words",
            scope="topic",
            topic_id=str(topic_id),
            limit=3,
            viewer_user_id=str(viewer_id),
            bot_id="mediator",
            query_type="verbatim_quote",
        )
    finally:
        retriever.close()

    assert _source_pairs(results) == [("message", str(message_id), 1)]
    assert pool_closed is True
    assert retriever._closed is True
    assert retriever._loop.is_closed() is True
    assert retriever._thread.is_alive() is False
    retriever.close()
    create_call = calls[0]
    assert create_call[0] == "create_pool"
    assert create_call[1] == "postgresql://direct.example/db"
    query_call = next(
        call for call in calls if isinstance(call, tuple) and call[0] == "query"
    )
    query_kwargs = query_call[1]
    assert query_kwargs["mode"] == "exact"
    assert query_kwargs["viewer_user_id"] == viewer_id
    assert query_kwargs["topic_id"] == topic_id
    assert query_kwargs["limit"] == 3
    hybrid_call = next(
        call for call in calls if isinstance(call, tuple) and call[0] == "hybrid_search"
    )
    assert hybrid_call[3]["source_weight_map"] is None


def test_db_retriever_maps_paraphrase_cases_to_hybrid_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    from eval.retrieval.adapters import DbBackedRetriever

    modes: list[str] = []

    fake_asyncpg = types.ModuleType("asyncpg")

    class FakePool:
        async def close(self) -> None:
            pass

    async def create_pool(database_url: str, **kwargs: object) -> object:
        return FakePool()

    fake_asyncpg.create_pool = create_pool  # type: ignore[attr-defined]

    fake_retrieval = types.ModuleType("app.services.retrieval")

    class RetrievalQuery:
        def __init__(self, **kwargs: object) -> None:
            modes.append(str(kwargs["mode"]))
            self.message_id = UUID(int=0)

    async def hybrid_search(
        pool: object, request: object, **kwargs: object
    ) -> list[object]:
        return []

    fake_retrieval.RetrievalQuery = RetrievalQuery  # type: ignore[attr-defined]
    fake_retrieval.hybrid_search = hybrid_search  # type: ignore[attr-defined]

    monkeypatch.setenv("DIRECT_DATABASE_URL", "postgresql://direct.example/db")
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    monkeypatch.setitem(sys.modules, "app.services.retrieval", fake_retrieval)

    retriever = DbBackedRetriever(_make_test_corpus())
    try:
        assert retriever.retrieve(
            "same idea different words",
            scope="all",
            limit=5,
            viewer_user_id=str(uuid4()),
            query_type="paraphrase",
        ) == []
    finally:
        retriever.close()

    assert modes == ["hybrid"]


def test_db_retriever_maps_exact_and_hybrid_query_types(
    monkeypatch: pytest.MonkeyPatch,
):
    from eval.retrieval.adapters import DbBackedRetriever

    modes: list[str] = []

    fake_asyncpg = types.ModuleType("asyncpg")

    class FakePool:
        async def close(self) -> None:
            pass

    async def create_pool(database_url: str, **kwargs: object) -> object:
        return FakePool()

    fake_asyncpg.create_pool = create_pool  # type: ignore[attr-defined]

    fake_retrieval = types.ModuleType("app.services.retrieval")

    class RetrievalQuery:
        def __init__(self, **kwargs: object) -> None:
            modes.append(str(kwargs["mode"]))

    async def hybrid_search(
        pool: object, request: object, **kwargs: object
    ) -> list[object]:
        return []

    fake_retrieval.RetrievalQuery = RetrievalQuery  # type: ignore[attr-defined]
    fake_retrieval.hybrid_search = hybrid_search  # type: ignore[attr-defined]

    monkeypatch.setenv("DIRECT_DATABASE_URL", "postgresql://direct.example/db")
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    monkeypatch.setitem(sys.modules, "app.services.retrieval", fake_retrieval)

    retriever = DbBackedRetriever(_make_test_corpus())
    try:
        for query_type in (
            "verbatim_quote",
            "paraphrase",
            "cross_thread",
            "topic_recall",
            None,
        ):
            assert retriever.retrieve(
                "query",
                scope="all",
                limit=5,
                viewer_user_id=str(uuid4()),
                query_type=query_type,
            ) == []
    finally:
        retriever.close()

    assert modes == ["exact", "hybrid", "hybrid", "hybrid", "hybrid"]


def test_db_retriever_preserves_non_message_results_in_source_key_output(
    monkeypatch: pytest.MonkeyPatch,
):
    from eval.retrieval.adapters import DbBackedRetriever

    fake_asyncpg = types.ModuleType("asyncpg")

    class FakePool:
        async def close(self) -> None:
            pass

    async def create_pool(database_url: str, **kwargs: object) -> object:
        return FakePool()

    fake_asyncpg.create_pool = create_pool  # type: ignore[attr-defined]

    fake_retrieval = types.ModuleType("app.services.retrieval")
    message_id = uuid4()
    memory_id = uuid4()

    class RetrievalQuery:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    async def hybrid_search(
        pool: object, request: object, **kwargs: object
    ) -> list[object]:
        return [
            types.SimpleNamespace(message_id=None, source_type="memory", source_id=memory_id),
            types.SimpleNamespace(message_id=message_id, source_type="message", source_id=message_id),
        ]

    fake_retrieval.RetrievalQuery = RetrievalQuery  # type: ignore[attr-defined]
    fake_retrieval.hybrid_search = hybrid_search  # type: ignore[attr-defined]

    monkeypatch.setenv("DIRECT_DATABASE_URL", "postgresql://direct.example/db")
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    monkeypatch.setitem(sys.modules, "app.services.retrieval", fake_retrieval)

    retriever = DbBackedRetriever(_make_test_corpus())
    try:
        results = retriever.retrieve(
            "query",
            scope="all",
            limit=5,
            viewer_user_id=str(uuid4()),
        )
    finally:
        retriever.close()

    assert _source_pairs(results) == [
        ("memory", str(memory_id), 1),
        ("message", str(message_id), 2),
    ]


def test_db_retriever_preserves_all_non_message_source_types_in_ranked_keys(
    monkeypatch: pytest.MonkeyPatch,
):
    """DbBackedRetriever maps conversation_note, theme, and artifact results
    to RankedSourceKey objects — they are NOT filtered out or collapsed
    to message_id."""
    from eval.retrieval.adapters import DbBackedRetriever

    fake_asyncpg = types.ModuleType("asyncpg")

    class FakePool:
        async def close(self) -> None:
            pass

    async def create_pool(database_url: str, **kwargs: object) -> object:
        return FakePool()

    fake_asyncpg.create_pool = create_pool  # type: ignore[attr-defined]

    fake_retrieval = types.ModuleType("app.services.retrieval")
    note_id = uuid4()
    theme_id = uuid4()
    artifact_id = uuid4()

    class RetrievalQuery:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    async def hybrid_search(
        pool: object, request: object, **kwargs: object
    ) -> list[object]:
        return [
            types.SimpleNamespace(
                message_id=None,
                source_type="conversation_note",
                source_id=note_id,
            ),
            types.SimpleNamespace(
                message_id=None,
                source_type="theme",
                source_id=theme_id,
            ),
            types.SimpleNamespace(
                message_id=None,
                source_type="artifact",
                source_id=artifact_id,
            ),
        ]

    fake_retrieval.RetrievalQuery = RetrievalQuery  # type: ignore[attr-defined]
    fake_retrieval.hybrid_search = hybrid_search  # type: ignore[attr-defined]

    monkeypatch.setenv("DIRECT_DATABASE_URL", "postgresql://direct.example/db")
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    monkeypatch.setitem(sys.modules, "app.services.retrieval", fake_retrieval)

    retriever = DbBackedRetriever(_make_test_corpus())
    try:
        results = retriever.retrieve(
            "query",
            scope="all",
            limit=5,
            viewer_user_id=str(uuid4()),
        )
    finally:
        retriever.close()

    assert _source_pairs(results) == [
        ("conversation_note", str(note_id), 1),
        ("theme", str(theme_id), 2),
        ("artifact", str(artifact_id), 3),
    ]


def test_db_retriever_passes_source_weight_map_override_to_production_hybrid_search(
    monkeypatch: pytest.MonkeyPatch,
):
    from eval.retrieval.adapters import DbBackedRetriever

    fake_asyncpg = types.ModuleType("asyncpg")
    received_kwargs: list[dict[str, object]] = []

    class FakePool:
        async def close(self) -> None:
            pass

    async def create_pool(database_url: str, **kwargs: object) -> object:
        return FakePool()

    fake_asyncpg.create_pool = create_pool  # type: ignore[attr-defined]

    fake_retrieval = types.ModuleType("app.services.retrieval")
    message_id = uuid4()

    class RetrievalQuery:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    async def hybrid_search(
        pool: object, request: object, **kwargs: object
    ) -> list[object]:
        received_kwargs.append(dict(kwargs))
        return [
            types.SimpleNamespace(
                message_id=message_id,
                source_type="message",
                source_id=message_id,
            )
        ]

    fake_retrieval.RetrievalQuery = RetrievalQuery  # type: ignore[attr-defined]
    fake_retrieval.hybrid_search = hybrid_search  # type: ignore[attr-defined]

    monkeypatch.setenv("DIRECT_DATABASE_URL", "postgresql://direct.example/db")
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    monkeypatch.setitem(sys.modules, "app.services.retrieval", fake_retrieval)

    retriever = DbBackedRetriever(
        _make_test_corpus(),
        source_weight_map={"theme": 0.25, "conversation_note": 1.15},
    )
    try:
        results = retriever.retrieve(
            "query",
            scope="all",
            limit=5,
            viewer_user_id=str(uuid4()),
        )
    finally:
        retriever.close()

    assert _source_pairs(results) == [("message", str(message_id), 1)]
    assert received_kwargs == [
        {"source_weight_map": {"theme": 0.25, "conversation_note": 1.15}}
    ]
