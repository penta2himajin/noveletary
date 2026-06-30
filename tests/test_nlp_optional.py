"""GiNZA退避抽出 ginza_records のスキーマ確認。NLP extra 未導入なら skip。"""

import pytest

spacy = pytest.importorskip("spacy")


def _has_ginza():
    try:
        spacy.load("ja_ginza")
        return True
    except Exception:
        return False


class _M:
    def __init__(self, text, pos):
        self.text, self.pos = text, pos


class _Phrase:
    def __init__(self, morphemes):
        self.morphemes = morphemes


class _Head:
    def __init__(self, lemma):
        self.lemma = lemma


class _BP:
    # KWJA基本句のダック型(必要属性のみ)
    def __init__(self, idx, content, head_lemma, particle=None, parent_idx=None):
        ms = [_M(c, "名詞") for c in content]
        if particle:
            ms.append(_M(particle, "助詞"))
        self.morphemes = ms
        self.phrase = _Phrase(ms)  # この単純ケースでは文節=基本句
        self.head = _Head(head_lemma)
        self.index = idx
        self.parent = type("P", (), {"index": parent_idx})() if parent_idx is not None else None
        self.sentence = None


def test_content_surface_keeps_compound_noun():
    # 文節の内容語を連結し、複合名詞が頭形態素に切り詰められないこと
    from noveletary.kwja_extract import _content_surface

    ms = [_M("補修", "名詞"), _M("潜水", "名詞"), _M("士", "接尾辞"), _M("だ", "判定詞")]
    bp = type("BP", (), {"phrase": _Phrase(ms), "morphemes": ms, "head": _Head("士")})()
    assert _content_surface(bp) == "補修潜水士"  # not 「士」


def test_content_surface_keeps_name_middledot():
    # 残差①: 固有名の中黒(記号)を潰さない
    from noveletary.kwja_extract import _content_surface

    ms = [_M("イオ", "名詞"), _M("・", "特殊"), _M("チェン", "名詞"), _M("は", "助詞")]
    bp = type("BP", (), {"phrase": _Phrase(ms), "morphemes": ms, "head": _Head("チェン")})()
    assert _content_surface(bp) == "イオ・チェン"  # not 「イオチェン」


def test_np_surface_prepends_genitive_modifier():
    # 属格の連体修飾(「艦長の」)を前置して名詞句を復元
    from noveletary.kwja_extract import _np_surface

    head = _BP(1, ["名"], "名")
    mod = _BP(0, ["艦長"], "艦長", particle="の", parent_idx=1)
    sent = type("S", (), {"base_phrases": [mod, head]})()
    head.sentence = mod.sentence = sent
    assert _np_surface(head) == "艦長の名"  # not 「名」


def test_ensure_kwja_cache_skips_when_present(tmp_path, monkeypatch):
    # キャッシュに既存なら(ネットワークに触れず)skipしてTrueを返す経路の回帰ガード
    pytest.importorskip("kwja")
    from kwja.cli.config import ModelSize
    from kwja.cli.utils import _CHECKPOINT_FILE_NAMES, _get_model_version

    from noveletary.kwja_extract import ensure_kwja_cache

    monkeypatch.setenv("KWJA_CACHE_DIR", str(tmp_path))
    ver = _get_model_version()
    fn = _CHECKPOINT_FILE_NAMES[ModelSize.BASE]["char"]
    d = tmp_path / ver
    d.mkdir(parents=True)
    (d / fn).write_bytes(b"x" * 16)  # char を既存扱いに
    assert ensure_kwja_cache("base", tasks=("char",)) is True


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
