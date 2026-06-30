"""GiNZA退避抽出 ginza_records のスキーマ確認。NLP extra 未導入なら skip。"""

import pytest

spacy = pytest.importorskip("spacy")


def _has_ginza():
    try:
        spacy.load("ja_ginza")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _has_ginza(), reason="ja_ginza not installed (pip install '.[nlp]')")
def test_ginza_records_schema():
    from noveletary.extract import ginza_records

    r = ginza_records("ハルは港へ向かった。", 2)
    assert r["record_count"] >= 1
    rec = r["records"][0]
    # 汎用スキーマのキーが揃い、物語固有属性に畳まれていないこと
    for k in ("subject", "predicate", "modality", "arguments", "zero_resolution"):
        assert k in rec
    assert rec["modality"] is None  # GiNZAは状態/動態を判定しない
