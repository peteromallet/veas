from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_NUMBER = "0059"
UP_PATH = MIGRATIONS_DIR / f"{MIGRATION_NUMBER}_content_embeddings_deferred_source_types.sql"
DOWN_PATH = MIGRATIONS_DIR / f"{MIGRATION_NUMBER}_content_embeddings_deferred_source_types.down.sql"
UP_SQL = UP_PATH.read_text()
DOWN_SQL = DOWN_PATH.read_text()


def _compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_0059_files_exist_and_are_next_numbered_pair() -> None:
    numbered = sorted(
        path.name
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    assert numbered[-1].startswith(f"{MIGRATION_NUMBER}_")
    assert UP_PATH.exists()
    assert DOWN_PATH.exists()


def test_0059_widens_both_source_type_constraints() -> None:
    lowered = _compact(UP_SQL)
    for table in ("content_embeddings", "embed_jobs"):
        assert f"alter table mediator.{table}" in lowered
    assert lowered.count("'conversation_note'") >= 2
    assert lowered.count("'theme'") >= 2
    assert "check ( source_type in ( 'message', 'memory', 'observation', 'distillation', 'artifact', 'conversation_note', 'theme' ) )" in lowered


def test_0059_adds_conversation_note_arm_with_non_empty_note_gate() -> None:
    lowered = _compact(UP_SQL)
    assert "'conversation_note'::text as source_type" in lowered
    assert "from mediator.conversation_notes cn join mediator.conversations c on c.id = cn.conversation_id" in lowered
    assert "c.bot_id" in lowered
    assert "c.topic_id" in lowered
    assert "cn.text as canonical_text" in lowered
    assert "to_tsvector('simple'::regconfig, cn.text) as search_tsv" in lowered
    assert "where btrim(coalesce(cn.text, '')) <> ''" in lowered


def test_0059_adds_theme_arm_with_active_gate_topic_aggregation_and_bot_scope() -> None:
    lowered = _compact(UP_SQL)
    assert "'theme'::text as source_type" in lowered
    assert "t.recorded_by_bot_id as bot_id" in lowered
    assert "btrim(concat_ws(e'\\n', t.title, t.description)) as canonical_text" in lowered
    assert "to_tsvector('simple'::regconfig, btrim(concat_ws(e'\\n', t.title, t.description))) as search_tsv" in lowered
    assert "from mediator.themes t left join lateral" in lowered
    assert "at.artifact_table = 'themes'" in lowered
    assert "array_agg(at.topic_id order by at.topic_id)" in lowered
    assert "coalesce(topics.topic_ids, array[]::uuid[]) as topic_ids" in lowered
    assert "where t.status = 'active'" in lowered


def test_0059_preserves_existing_artifact_exclusions_and_fallback_contract() -> None:
    lowered = _compact(UP_SQL)
    assert "ca.deleted_at is null" in lowered
    assert "(ca.expires_at is null or ca.expires_at > now())" in lowered
    assert "else btrim(concat_ws(e'\\n'" in lowered


def test_0059_down_cleans_new_rows_before_reinstating_old_constraints() -> None:
    lowered = _compact(DOWN_SQL)
    assert "delete from mediator.embed_jobs where source_type in ('conversation_note', 'theme')" in lowered
    assert "delete from mediator.content_embeddings where source_type in ('conversation_note', 'theme')" in lowered
    assert lowered.index("delete from mediator.embed_jobs") < lowered.index("alter table mediator.content_embeddings")
    assert lowered.index("delete from mediator.content_embeddings") < lowered.index("alter table mediator.content_embeddings")
    assert "check ( source_type in ( 'message', 'memory', 'observation', 'distillation', 'artifact' ) )" in lowered
