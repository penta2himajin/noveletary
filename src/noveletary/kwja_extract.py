"""
kwja_extract.py — KWJA(京大)のPAS出力 → 事実候補アダプタ

extract.py のGiNZA+鎖heuristic(ゼロ主語回復14%天井)を、
KWJAの述語項構造解析(ゼロ照応解決済み, PAS F≈77 / coref≈79)で置換する。
extract.extract() と同じ候補形式を返すので reconcile_facts にそのまま差し込める。

実証(実章ch2の実文): モロー死亡のゼロ主語(LIFE=dead)を解決、テ形連鎖の落ちた
主語(ハル←落とす/上げる)も補完。新規実体(ハル/モロー)でドメインシフトに耐える。
無生物・存在主語(被覆/索/痕など)も拾うが、reconcile の既知実体フィルタ+状態系限定で除外される。

軽量構成: model_size=base, tasks=char,word (typo/seq2seqは不要)。
チェックポイントは ~/.cache/kwja/<ver>/ に配置(char_/word_ deberta-v2-base)。
メモリ: char+word 同時ロードは数GB必要。低RAM環境では --word-batch-size 1 か文単位処理。

KWJAのガ格(主語)解決の型(rhoknp ArgumentType):
  - CASE_EXPLICIT      : 文中に明示(直接)
  - OMISSION/CASE_HIDDEN: 省略主語を文脈中の先行詞に解決(←GiNZAが落としていた本命=ゼロ照応)
  - EXOPHORA           : 外界照応(著者=語り手/POV, 読者, 不特定:人 など)
"""

import re

_DEATH = re.compile(r"(死|没|斃|壊滅|全滅|戦死|絶命|息絶え|落命)")
_MOVE = re.compile(r"(向かう|行く|出る|入る|着く|戻る|乗り込む|降りる|到着)")

_kwja = None


def _get_kwja(model_size="base"):
    global _kwja
    if _kwja is None:
        from rhoknp import KWJA

        _kwja = KWJA(options=["--tasks", "char,word", "--model-size", model_size, "--device", "cpu"])
    return _kwja


def _classify(pred_lemma):
    if _DEATH.search(pred_lemma):
        return "LIFE", "dead"
    if _MOVE.search(pred_lemma):
        return "LOC", None  # 値はニ格(行先)で埋める
    return "ACT", pred_lemma


def _arg_subject(arg, pov_character):
    """ガ格引数 → (subject, zero_resolution)。"""
    tname = type(arg).__name__
    atype = getattr(arg, "type", None)
    aname = atype.name if atype is not None else ""
    if tname == "ExophoraArgument":
        ref = str(getattr(arg, "exophora_referent", "") or "")
        if "著者" in ref:
            return (pov_character or "(著者/POV)"), "著者→POV"
        return f"(外界:{ref})", "外界照応"
    # EndophoraArgument
    head = arg.base_phrase.head.lemma
    if aname in ("OMISSION", "CASE_HIDDEN"):
        return head, "ゼロ照応"  # ←回復した省略主語
    return head, "直接"


def extract_kwja(text, chapter, pov_character=None, model_size="base"):
    """KWJAでPAS解析し、ゼロ照応解決済みの事実候補を返す。
    pov_character を渡すと『著者』(語り手)解決の項をそのキャラに割り当てる。"""
    kwja = _get_kwja(model_size)
    doc = kwja.apply(text)
    cands = []
    for sent in doc.sentences:
        for bp in sent.base_phrases:
            if "用言" not in bp.features:  # 述語(用言)のみ
                continue
            pas = bp.pas
            pred_lemma = bp.head.lemma
            ga = pas.get_arguments("ガ", relax=False)
            subject, ztype = (None, "なし")
            if ga:
                subject, ztype = _arg_subject(ga[0], pov_character)
            ni = pas.get_arguments("ニ", relax=False)
            wo = pas.get_arguments("ヲ", relax=False)
            attr, val = _classify(pred_lemma)
            if attr == "LOC":
                # 行先(ニ格 endophora)を値に
                for a in ni:
                    if type(a).__name__ == "EndophoraArgument":
                        val = a.base_phrase.head.lemma
                        break
                if val is None:
                    val = pred_lemma
            elif attr == "ACT":
                val = pred_lemma
            obj = None
            for a in wo:
                if type(a).__name__ == "EndophoraArgument":
                    obj = a.base_phrase.head.lemma
                    break
            cands.append(
                {
                    "subject": subject,
                    "attribute": attr,
                    "value": val,
                    "chapter": chapter,
                    "zero_subject": subject is None,
                    "zero_resolution": ztype,  # 直接 / ゼロ照応 / 著者→POV / 外界照応
                    "object": obj,
                    "provenance": sent.text.strip()[:60],
                }
            )
    return {
        "chapter": chapter,
        "candidate_count": len(cands),
        "candidates": cands,
        "engine": f"KWJA({model_size}, char+word, PAS+zero-anaphora)",
    }


if __name__ == "__main__":
    import json
    import sys

    txt = sys.stdin.read() if not sys.stdin.isatty() else "モローは撃たれて死んだ。ハルは港へ向かった。"
    print(json.dumps(extract_kwja(txt, 1, pov_character="ハル"), ensure_ascii=False, indent=2))
