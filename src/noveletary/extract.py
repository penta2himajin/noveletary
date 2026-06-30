"""
extract.py — 散文→事実の独立抽出 + LLM抽出との照合

役割: LLMの自己申告(「この章にこういう事実がある」)に対する第二の観測。
機構が同じ章テキストを決定論的NLP(GiNZA)で別経路から読み、両者を突き合わせる。
 - 一致      = 確証
 - LLMのみ   = 本文に根拠が薄い → 捏造/過剰解釈の候補
 - 機構のみ  = 本文にあるがLLMが申告漏れ → 抽出漏れの候補

抽出は不完全(ゼロ主語・新規実体で取りこぼす)。authoritativeではなく第二の信号。
最終判断は照合結果を見たLLM/作家に委ねる。
"""

import re

_nlp = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy

        _nlp = spacy.load("ja_ginza", config={"components": {"compound_splitter": {"split_mode": "C"}}})
    return _nlp


# 述語→候補属性の粗いマップ(弱信号)
_DEATH = re.compile(r"(死|没|斃|壊滅|全滅|戦死|絶命|息絶え|落命)")
_MOVE = re.compile(r"(向かう|行く|出る|入る|着く|戻る|乗り込む|降りる|到着)")
_LEDGER_CUE = re.compile(r"(残高|死者帳|賞金|戦没|借金|クレジット)")
_KANJI_NUM = re.compile(r"[〇零一二三四五六七八九十百千万億0-9]+")


def extract(chapter_text, chapter):
    """章テキストから事実候補を抽出。各候補に provenance(典拠文)を付ける。"""
    nlp = _get_nlp()
    cands = []
    ents_all = set()
    for line in chapter_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for sent in nlp(line).sents:
            s = sent.text.strip()
            ents = {e.text: e.label_ for e in sent.ents}
            ents_all |= set(ents)
            for t in sent:
                if t.pos_ != "VERB":
                    continue
                subj = [c.lemma_ for c in t.children if c.dep_ in ("nsubj", "nsubj:pass")]
                obj = [c.lemma_ for c in t.children if c.dep_ in ("obj", "iobj")]
                subject = subj[0] if subj else None  # ゼロ主語は None(=未解決)
                obl = [c.lemma_ for c in t.children if c.dep_ == "obl"]  # へ/に格=行先
                # 候補属性の推定(死亡は述語自体のみ; 文全体には波及させない)
                if _DEATH.search(t.lemma_):
                    attr, val = "LIFE", "dead"
                elif _MOVE.search(t.lemma_):
                    attr, val = "LOC", (obl[0] if obl else (obj[0] if obj else t.lemma_))
                else:
                    attr, val = "ACT", t.lemma_
                cands.append(
                    {
                        "subject": subject,
                        "attribute": attr,
                        "value": val,
                        "chapter": chapter,
                        "zero_subject": subject is None,
                        "provenance": s[:60],
                    }
                )
            # 台帳候補(数値+台帳語)
            if _LEDGER_CUE.search(s):
                nums = _KANJI_NUM.findall(s)
                if nums:
                    cands.append(
                        {
                            "subject": "(台帳)",
                            "attribute": "LEDGER",
                            "value": s[:20],
                            "chapter": chapter,
                            "zero_subject": False,
                            "provenance": s[:60],
                        }
                    )
    return {"chapter": chapter, "entities": sorted(ents_all), "candidate_count": len(cands), "candidates": cands}


def _norm(s):
    return re.sub(r"[。、\s]", "", s or "")


# 一貫性に効くのは状態系(行為ACTは対象外=多すぎ&矛盾になりにくい)
_STATE_ATTRS = {"LIFE", "LOC", "RANK", "STATE", "LEDGER"}


def reconcile(chapter, llm_facts, chapter_text, known_entities=None):
    """LLM申告事実 と 機構の独立抽出 を突き合わせ3分類。
    known_entities(KBの既知実体)で対象を絞る=domain-shiftで壊れるNERに依存しない。
    状態系(LIFE/LOC/RANK/STATE/LEDGER)のみ照合し、値まで見る。"""
    known = set(_norm(e) for e in (known_entities or []))
    known |= set(_norm(f.get("subject")) for f in llm_facts)  # LLMが触れた実体も対象
    mech = [
        c
        for c in extract(chapter_text, chapter)["candidates"]
        if c["attribute"] in _STATE_ATTRS and not c["zero_subject"] and _norm(c["subject"]) in known
    ]

    def key(f):
        return (_norm(f.get("subject")), f.get("attribute"), _norm(f.get("value")))

    mech_keys = {}
    for m in mech:
        mech_keys.setdefault(key(m), []).append(m)
    llm_state = [lf for lf in llm_facts if lf.get("attribute") in _STATE_ATTRS]
    llm_keys = {key(lf): lf for lf in llm_state}

    agree, llm_only = [], []
    for k, lf in llm_keys.items():
        (agree if k in mech_keys else llm_only).append(lf)
    mech_only = [m for k, ms in mech_keys.items() if k not in llm_keys for m in ms]

    return {
        "chapter": chapter,
        "agreement": agree,  # 一致=確証
        "llm_only_check_grounding": llm_only,  # 本文に根拠薄=捏造/過剰解釈の疑い
        "mechanism_only_possible_omission": mech_only,  # 本文にあるがLLM申告漏れの疑い
        "scope": "状態系(LIFE/LOC/RANK/STATE/LEDGER)・既知実体・値照合",
        "note": "機構抽出は不完全。差分は確定でなく要確認。行為(ACT)とゼロ主語は照合対象外。",
    }
