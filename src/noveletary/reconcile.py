"""
reconcile.py — LLMの自己申告 と 機構の独立抽出(KWJA述語-項レコード) の照合

物語固有の属性(LIFE/LOC/ACT)に依存しない汎用照合。
機構が同じ章テキストから独立に抽出した述語-項レコードと、LLMが申告した事実を
(主語, 述語) を軸に突き合わせ、3分類で返す:
  - agreement                : 一致=確証
  - llm_only_check_grounding : 本文に根拠が薄い=捏造/過剰解釈の疑い
  - mechanism_only_*         : 本文にあるがLLM申告漏れの疑い(state/event別に分離)

modality は KWJAの状態/動態述語タグ由来。状態の漏れは高シグナル、行為の漏れは
低シグナルだが死亡等の重要イベントを含むため別バケツで提示する。
照合は表層レンマの一致なので同義語は取りこぼす(advisory; 確定でなく要確認)。
"""

import re


def _norm(s):
    return re.sub(r"[。、\s（）()]", "", s or "")


_ROLE_SUFFIX = ("士", "官", "長", "師", "兵", "将", "帝", "卿", "侯", "伯", "王")
_DEATH_PRED = ("死ぬ", "死亡", "亡くなる", "逝く", "果てる", "絶命")


def _suggest_type(cand, subject_known=False):
    """記帳補助の物語型“提案”(advisory)。generic抽出は変えず、採用時の精緻化を一目で示す。
    - 死亡述語 かつ 主語が既知実体 → {attribute: LIFE, value: dead}
      ※「磁気圏が死にかける」等の比喩/非生物は、主語が既知実体でない+lemmaのブレもあるため出さない安全側。
    - 「Xの名(前)」主語の状態 → {subject: X, attribute: 呼称}(主客の整形)
    - 役職接尾(…士/官/長/師…)の状態 → {attribute: RANK}"""
    subj = cand.get("subject", "") or ""
    val = cand.get("value", "") or ""
    is_state = cand.get("kind") == "STATE"
    pred = val.split(":")[0]  # value は predicate または predicate:loc
    if pred in _DEATH_PRED and subject_known:
        return {"attribute": "LIFE", "value": "dead"}
    m = re.match(r"^(.+?)の(?:名前|名)$", subj)
    if m and is_state:
        return {"subject": m.group(1), "attribute": "呼称"}
    if is_state and any(val.endswith(s) for s in _ROLE_SUFFIX):
        return {"attribute": "RANK"}
    return None


def triage_candidates(candidates, existing_facts, aliases=None):
    """記帳の下書き候補(records_to_facts 出力)を、採否しやすいよう仕分ける。NLP非依存の純ロジック。
    candidates: [{subject, attribute, value, kind, ...}] / existing_facts: get_state の facts / aliases: 別名表。
    - existing : 既にカノンにある(canon主語×値でdedup)→提示のみ
    - high_new : 新規かつ高シグナル(状態=判定詞由来 STATE、または主語が既知実体の行為)→採用候補
    - low_new  : 新規だが低シグナル(未知主語の瑣末な行為)→要確認
    機構抽出は不完全なので確定でなく候補。high_new を中心に採否し add_facts で確定する。"""
    aliases = aliases or {}

    def canon(s):
        return aliases.get(s, s)

    known = {canon(f["subject"]) for f in existing_facts}
    existing_pairs = {(canon(f["subject"]), _norm(f.get("value"))) for f in existing_facts}
    high_new, low_new, existing_hits = [], [], []
    for c in candidates:
        cs = canon(c.get("subject"))
        is_new = (cs, _norm(c.get("value"))) not in existing_pairs
        signal = "high" if (c.get("kind") == "STATE" or cs in known) else "low"
        item = {**c, "signal": signal, "is_new": is_new}
        suggest = _suggest_type(c, cs in known)  # 物語型の提案(advisory; 採用時に retag/型付けの手間を省く)
        if suggest:
            item["suggest"] = suggest
        if not is_new:
            existing_hits.append(item)
        elif signal == "high":
            high_new.append(item)
        else:
            low_new.append(item)
    return {
        "high_new": high_new,
        "low_new": low_new,
        "existing": existing_hits,
        "summary": {"high_new": len(high_new), "low_new": len(low_new), "existing": len(existing_hits)},
        "note": "high_new(状態/既知実体)を中心に採否し add_facts へ。low_new は瑣末行為の疑い。確定でなく候補。",
    }


def reconcile_records(chapter, llm_facts, records, known_entities=None):
    """llm_facts: [{subject, predicate}] / records: extract_kwjaのrecords。
    known_entities(KBの既知実体)で対象を絞る=domain-shiftで壊れるNERに依存しない。"""
    relevant = {_norm(e) for e in (known_entities or [])}
    relevant |= {_norm(f.get("subject")) for f in llm_facts}

    def key(subj, pred):
        return (_norm(subj), _norm(pred))

    # 機構レコード: 既知実体に主語が解決できたものだけ(ゼロ主語Noneや外界は除外)
    mech = [r for r in records if r.get("subject") and _norm(r["subject"]) in relevant]
    mech_keys = {}
    for r in mech:
        mech_keys.setdefault(key(r["subject"], r["predicate"]), []).append(r)

    llm_keys = {}
    for f in llm_facts:
        llm_keys.setdefault(key(f.get("subject"), f.get("predicate")), []).append(f)

    agree, llm_only = [], []
    for k, fs in llm_keys.items():
        (agree if k in mech_keys else llm_only).extend(fs)

    mech_only = [r for k, rs in mech_keys.items() if k not in llm_keys for r in rs]
    mech_only_state = [r for r in mech_only if r.get("modality") == "state"]
    mech_only_event = [r for r in mech_only if r.get("modality") != "state"]

    return {
        "chapter": chapter,
        "agreement": agree,
        "llm_only_check_grounding": llm_only,
        "mechanism_only_state_possible_omission": mech_only_state,  # 状態の漏れ(高シグナル)
        "mechanism_only_event_possible_omission": mech_only_event,  # 行為の漏れ(死亡等を含む)
        "scope": "(主語,述語)軸・既知実体・ゼロ照応解決済み主語のみ",
        "note": "機構抽出は不完全・表層一致。差分は確定でなく要確認。",
    }
