"""Load and validate corpus and golden-set YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml

from eval.retrieval.schema import Corpus, GoldenCase, GoldenSet


def load_corpus(path: Path) -> Corpus:
    """Load and validate a corpus YAML file.

    Args:
        path: Path to a YAML file containing a list of CorpusMessage dicts.

    Returns:
        A validated Corpus instance.

    Raises:
        ValueError: If the file cannot be parsed or the data fails validation.
    """
    with open(path) as f:
        data = yaml.safe_load(f)
    return Corpus.model_validate(data)


def load_golden_set(path: Path, *, corpus: Corpus | None = None) -> GoldenSet:
    """Load and validate a golden-set YAML file.

    Args:
        path: Path to a YAML file containing a list of GoldenCase dicts.
        corpus: Optional Corpus to validate expected_message_ids against.
                If provided, dangling references raise ValueError.

    Returns:
        A validated GoldenSet instance.

    Raises:
        ValueError: If validation fails for any reason:
            - Dangling expected_message_ids (referencing an id not in corpus).
            - Empty expected_message_ids (SD6 / correctness-4).
            - scope=='thread' with thread_id is None (correctness-3).
            - scope=='topic' with topic_id is None (callers-1).
    """
    with open(path) as f:
        data = yaml.safe_load(f)

    golden_set = GoldenSet.model_validate(data)

    # Build corpus id set if corpus provided.
    corpus_ids: set[str] | None = None
    if corpus is not None:
        corpus_ids = {m.id for m in corpus.messages}

    for case in golden_set.cases:
        # (b) Empty expected_message_ids
        if not case.expected_message_ids:
            raise ValueError(
                f"GoldenCase '{case.id}' has empty expected_message_ids"
            )

        # (a) Dangling refs
        if corpus_ids is not None:
            for msg_id in case.expected_message_ids:
                if msg_id not in corpus_ids:
                    raise ValueError(
                        f"GoldenCase '{case.id}' references message id "
                        f"'{msg_id}' which is not in the corpus"
                    )

        # (c) Scope / id consistency
        if case.scope == "thread" and case.thread_id is None:
            raise ValueError(
                f"GoldenCase '{case.id}' has scope='thread' but thread_id is None"
            )
        if case.scope == "topic" and case.topic_id is None:
            raise ValueError(
                f"GoldenCase '{case.id}' has scope='topic' but topic_id is None"
            )

    return golden_set
