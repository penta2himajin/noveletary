"""
kwja_extract.py — KWJA(京大)のPAS出力 → 事実候補アダプタ

extract.py のGiNZA+鎖heuristic(ゼロ主語回復14%天井)を、
KWJAの述語項構造解析(ゼロ照応解決済み, PAS F≈77 / coref≈79)で置換する。
extract.extract() と同じ候補形式を返すので reconcile_facts にそのまま差し込める。

【現状】京大配信サーバ lotus.kuee.kyoto-u.ac.jp が503のため未実走。
       サーバ復活後 `pip install kwja rhoknp` 済み環境でそのまま動く。
       軽量構成: model_size=base, tasks=char,word (typo/seq2seqは不要)。

KWJAのゼロ照応はガ格の項を以下の型で返す:
  - 直接   : 文中に明示された項
  - ゼロ照応: 省略された項を文脈中の先行詞に解決(←GiNZAが落としていた本命)
  - 著者   : 一人称/語り手(POV)。物語ではPOVキャラ(例:ハル)に対応
  - 読者/不特定: 二人称/総称
"""

import re

_DEATH = re.compile(r"(死|没|斃|壊滅|全滅|戦死|絶命|息絶え|落命)")
_MOVE = re.compile(r"(向かう|行く|出る|入る|着く|戻る|乗り込む|降りる|到着)")

_kwja = None


def _get_kwja(model_size="base"):
    global _kwja
    if _kwja is None:
        from rhoknp import KWJA

        # char,word のみ(typo/seq2seq不要)。baseで精度/軽さの最適点。
        _kwja = KWJA(options=["--tasks", "char,word", "--model-size", model_size, "--device", "cpu"])
    return _kwja


def _classify(pred_lemma):
    if _DEATH.search(pred_lemma):
        return "LIFE", "dead"
    if _MOVE.search(pred_lemma):
        return "LOC", None  # 値はニ/ヘ格(landmark)で埋める
    return "ACT", pred_lemma


def extract_kwja(text, chapter, pov_character=None, model_size="base"):
    """KWJAでPAS解析し、ゼロ照応解決済みの事実候補を返す。
    pov_character を渡すと『著者』(語り手)解決の項をそのキャラに割り当てる。"""
    kwja = _get_kwja(model_size)
    doc = kwja.apply(text)
    cands = []
    for sent in doc.sentences:
        for bp in sent.base_phrases:
            pas = getattr(bp, "pas", None)
            if pas is None or pas.predicate is None:
                continue
            pred_lemma = (
                pas.predicate.base_phrase.head.lemma if hasattr(pas.predicate, "base_phrase") else bp.head.lemma
            )
            # ガ格(主語)
            ga_args = pas.get_arguments("ガ", relax=False)
            subject = None
            ztype = "直接"
            if ga_args:
                arg = ga_args[0]
                atype = getattr(arg, "type", None)
                aname = type(arg).__name__
                if "Exophora" in aname or getattr(arg, "exophora_referent", None) is not None:
                    ref = str(getattr(arg, "exophora_referent", "") or arg)
                    if "著者" in ref:
                        subject = pov_character or "(著者/POV)"
                        ztype = "著者→POV"
                    else:
                        subject = f"(外界:{ref})"
                        ztype = "外界照応"
                else:
                    subject = arg.base_phrase.head.lemma if getattr(arg, "base_phrase", None) else str(arg)
                    # 直接かゼロ照応か(rhoknpのargには直接/間接の区別がある)
                    ztype = "ゼロ照応" if getattr(arg, "is_zero", False) or atype == "omission" else "直接"
            # ヲ/ニ格
            wo = pas.get_arguments("ヲ", relax=False)
            ni = pas.get_arguments("ニ", relax=False)
            attr, val = _classify(pred_lemma)
            if attr == "LOC" and ni:
                val = ni[0].base_phrase.head.lemma if getattr(ni[0], "base_phrase", None) else str(ni[0])
            elif attr == "ACT":
                val = pred_lemma
            cands.append(
                {
                    "subject": subject,
                    "attribute": attr,
                    "value": val,
                    "chapter": chapter,
                    "zero_subject": subject is None,
                    "zero_resolution": ztype,  # ←KWJAがどう主語を埋めたか(直接/ゼロ照応/著者→POV)
                    "object": (wo[0].base_phrase.head.lemma if wo and getattr(wo[0], "base_phrase", None) else None),
                    "provenance": sent.text.strip()[:60],
                }
            )
    return {
        "chapter": chapter,
        "candidate_count": len(cands),
        "candidates": cands,
        "engine": f"KWJA({model_size}, char+word, PAS+zero-anaphora)",
    }


# reconcile は extract.reconcile をそのまま利用可能(候補形式が同一)。
# 置換は extract_facts/reconcile_facts ツール内の extract() を extract_kwja() に差し替えるだけ。
if __name__ == "__main__":
    import sys

    txt = sys.stdin.read() if not sys.stdin.isatty() else "モローは撃たれて死んだ。ハルは港へ向かった。"
    import json

    print(json.dumps(extract_kwja(txt, 1, pov_character="ハル"), ensure_ascii=False, indent=2))
