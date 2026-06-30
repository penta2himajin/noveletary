"""抽出/照合(GiNZA依存)。NLP extra 未導入なら skip。"""

import pytest

spacy = pytest.importorskip("spacy")


def _has_ginza():
    try:
        spacy.load("ja_ginza")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_ginza(), reason="ja_ginza not installed (pip install '.[nlp]')")
def test_reconcile_buckets():
    from noveletary.extract import reconcile

    text = "ハルは港へ向かった。"
    llm_facts = [
        {"subject": "ハル", "attribute": "LIFE", "value": "dead", "chapter": 2},  # 捏造
    ]
    r = reconcile(2, llm_facts, text, known_entities=["ハル"])
    # 本文に死亡根拠が無い → llm_only(要根拠確認)へ
    assert any(f["attribute"] == "LIFE" for f in r["llm_only_check_grounding"])
