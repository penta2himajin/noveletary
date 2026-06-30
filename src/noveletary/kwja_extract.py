"""
kwja_extract.py — KWJA(京大)のPAS出力 → 汎用の述語-項レコード

物語固有の語彙(死ぬ/向かう等)を一切持たない。KWJAが付ける言語的タグ
(状態述語/動態述語・用言種別・時制)と述語項構造(ゼロ照応解決済み)を、
そのまま構造化して返すだけのアダプタ。どの作品・どのドメインでも同一に動く。

各レコード:
  subject        : ガ項(ゼロ照応解決済み)。無ければ None。
  predicate      : 述語の生レンマ(属性に畳まない)
  modality       : "state" | "event"  ← KWJAの状態述語/動態述語タグのみ由来
  predicate_type : "動"(動詞) | "判"(判定詞/コピュラ) | その他
  arguments      : {"ガ":..,"ヲ":..,"ニ":..,"ト":..} 値はlemma or 外界照応ラベル
  tense          : "過去" | "非過去" | None
  zero_resolution: "直接" | "ゼロ照応" | "著者→POV" | "外界照応" | "なし"
  provenance     : 典拠文

軽量構成: model_size=base, tasks=char,word。char+word同時ロードは数GB必要なので
低RAM環境では --word-batch-size 1 か文単位処理(下記 analyze_per_sentence)。
"""

import subprocess
import tempfile

_kwja = None
_CASES = ("ガ", "ヲ", "ニ", "ト", "デ", "カラ", "ヘ", "ヨリ", "マデ")


def _get_kwja(model_size="base"):
    global _kwja
    if _kwja is None:
        from rhoknp import KWJA

        _kwja = KWJA(options=["--tasks", "char,word", "--model-size", model_size, "--device", "cpu"])
    return _kwja


def _resolve_arg(arg, pov_character):
    """項 → (値, 解決型)。endophoraはlemma、exophoraは外界ラベル(著者→POV)。"""
    if type(arg).__name__ == "ExophoraArgument":
        ref = str(getattr(arg, "exophora_referent", "") or "")
        if "著者" in ref:
            return (pov_character or "(著者/POV)"), "著者→POV"
        return f"(外界:{ref})", "外界照応"
    head = arg.base_phrase.head.lemma
    atype = getattr(arg, "type", None)
    aname = atype.name if atype is not None else ""
    if aname in ("OMISSION", "CASE_HIDDEN"):
        return head, "ゼロ照応"
    return head, "直接"


def _records_from_doc(doc, chapter, pov_character):
    out = []
    for sent in doc.sentences:
        for bp in sent.base_phrases:
            f = bp.features
            if "用言" not in f:  # 述語(用言)のみ
                continue
            pas = bp.pas
            args = {}
            ga_subject, ga_zero = None, "なし"
            for case in _CASES:
                got = pas.get_arguments(case, relax=False)
                if not got:
                    continue
                val, ztype = _resolve_arg(got[0], pov_character)
                args[case] = val
                if case == "ガ":
                    ga_subject, ga_zero = val, ztype
            modality = "state" if "状態述語" in f else ("event" if "動態述語" in f else None)
            out.append(
                {
                    "subject": ga_subject,
                    "predicate": bp.head.lemma,
                    "modality": modality,
                    "predicate_type": f.get("用言"),
                    "arguments": args,
                    "tense": f.get("時制"),
                    "zero_resolution": ga_zero,
                    "chapter": chapter,
                    "provenance": sent.text.strip()[:60],
                }
            )
    return out


def extract_kwja(text, chapter, pov_character=None, model_size="base"):
    """テキスト全体をKWJAでPAS解析し、汎用の述語-項レコード列を返す。"""
    kwja = _get_kwja(model_size)
    doc = kwja.apply(text)
    recs = _records_from_doc(doc, chapter, pov_character)
    return {
        "chapter": chapter,
        "record_count": len(recs),
        "records": recs,
        "engine": f"KWJA({model_size}, char+word, PAS+zero-anaphora)",
    }


def analyze_per_sentence(sentences, chapter, pov_character=None, model_size="base"):
    """低RAM環境向け: 1文ずつ別プロセスでKWJA CLIを回し、メモリをリセットしながら集計。
    sentences は文字列のリスト。モデルは毎回ロードされるため低速だが省メモリ。"""
    from rhoknp import Document

    recs = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=True) as tf:
            tf.write(s)
            tf.flush()
            r = subprocess.run(
                [
                    "kwja",
                    "--filename",
                    tf.name,
                    "--tasks",
                    "char,word",
                    "--model-size",
                    model_size,
                    "--device",
                    "cpu",
                    "--word-batch-size",
                    "1",
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
        if r.returncode != 0 or not r.stdout.strip():
            continue
        try:
            doc = Document.from_knp(r.stdout)
        except Exception:
            continue
        recs.extend(_records_from_doc(doc, chapter, pov_character))
    return {
        "chapter": chapter,
        "record_count": len(recs),
        "records": recs,
        "engine": f"KWJA({model_size}, char+word, per-sentence)",
    }


def records_to_facts(records):
    """述語-項レコード → store.import_facts 形式へ(汎用マッピング)。
    modality を kind/attribute へ畳む(state→STATE, event→ACT)。物語固有の型付けはしない。
    主語が解決できたレコードのみ。位置(ニ格)があれば value に併記。"""
    facts = []
    for r in records:
        if not r.get("subject"):
            continue
        is_state = r.get("modality") == "state"
        ni = r.get("arguments", {}).get("ニ")
        value = r["predicate"]
        if ni and not str(ni).startswith("(外界"):
            value = f"{r['predicate']}:{ni}"
        facts.append(
            {
                "subject": r["subject"],
                "attribute": "STATE" if is_state else "ACT",
                "value": value,
                "chapter": r.get("chapter"),
                "kind": "STATE" if is_state else "EVENT",
            }
        )
    return facts


if __name__ == "__main__":
    import json
    import sys

    txt = sys.stdin.read() if not sys.stdin.isatty() else "モローは撃たれて死んだ。ハルは港にいる。"
    print(json.dumps(extract_kwja(txt, 1, pov_character="ハル"), ensure_ascii=False, indent=2))
