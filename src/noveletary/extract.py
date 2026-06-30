"""
extract.py — GiNZAによる述語-項レコード抽出(KWJA非導入時のフォールバック)

KWJA(kwja_extract.py)と同じ汎用スキーマ(述語-項レコード)を返す。
ただしGiNZAは状態/動態タグもゼロ照応解決も持たないため degraded:
  - modality は None(KWJAのみ判定可能)
  - ゼロ主語は subject=None のまま(解決しない)
物語固有の語彙は一切持たない。属性への畳み込みもしない。

KWJAが使える環境では kwja_extract.extract_kwja を使うこと。これは退避用。
"""

_nlp = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy

        _nlp = spacy.load("ja_ginza", config={"components": {"compound_splitter": {"split_mode": "C"}}})
    return _nlp


# UDの格的依存 → 日本語格ラベルへの粗いマップ(語彙ではなく構文)
_DEP_TO_CASE = {"nsubj": "ガ", "nsubj:pass": "ガ", "obj": "ヲ", "iobj": "ニ", "obl": "ニ"}


def ginza_records(text, chapter, pov_character=None):
    """GiNZAで述語-項レコードを抽出(KWJAと同一スキーマ, degraded)。"""
    nlp = _get_nlp()
    recs = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for sent in nlp(line).sents:
            for t in sent:
                if t.pos_ != "VERB":
                    continue
                args = {}
                for c in t.children:
                    case = _DEP_TO_CASE.get(c.dep_)
                    if case and case not in args:
                        args[case] = c.lemma_
                subject = args.get("ガ")  # ゼロ主語は None のまま(GiNZAは解決しない)
                recs.append(
                    {
                        "subject": subject,
                        "predicate": t.lemma_,
                        "modality": None,  # GiNZAは状態/動態を判定しない
                        "predicate_type": None,
                        "arguments": args,
                        "tense": None,
                        "zero_resolution": "直接" if subject else "なし",
                        "chapter": chapter,
                        "provenance": sent.text.strip()[:60],
                    }
                )
    return {
        "chapter": chapter,
        "record_count": len(recs),
        "records": recs,
        "engine": "GiNZA(fallback, no modality/zero-anaphora)",
    }
