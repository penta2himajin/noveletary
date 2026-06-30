"""reconcile_records の照合ロジック。決定論的(モデル不要)・合成レコードで検証。"""

from noveletary.reconcile import reconcile_records, triage_candidates


def test_triage_candidates_signal_and_dedup():
    # 記帳下書きの仕分け: 状態/既知実体=high, 瑣末行為=low, 既出(alias経由)=dedup
    existing = [{"subject": "イオ・チェン", "value": "補修潜水士", "attribute": "STATE"}]
    aliases = {"イオ": "イオ・チェン"}
    cands = [
        {"subject": "イオ", "attribute": "STATE", "value": "補修潜水士", "kind": "STATE"},  # 既出(alias)
        {"subject": "艦長の名", "attribute": "STATE", "value": "ネモ", "kind": "STATE"},  # 新規・状態
        {"subject": "磁気圏", "attribute": "ACT", "value": "かける:死", "kind": "EVENT"},  # 新規・行為・未知主語
        {"subject": "イオ", "attribute": "ACT", "value": "向かう:係留区", "kind": "EVENT"},  # 新規・行為・既知主語
    ]
    r = triage_candidates(cands, existing, aliases)
    assert {c["value"] for c in r["high_new"]} == {"ネモ", "向かう:係留区"}
    assert {c["value"] for c in r["low_new"]} == {"かける:死"}
    assert {c["value"] for c in r["existing"]} == {"補修潜水士"}
    assert r["summary"] == {"high_new": 2, "low_new": 1, "existing": 1}


def test_triage_suggests_story_types():
    # 残差②③: 採用候補に物語型の提案(advisory)が付く。死亡は既知実体に限定(比喩/非生物を避ける)。
    existing = [{"subject": "モロー", "value": "生存", "attribute": "STATE"}]  # モローは既知実体
    cands = [
        {"subject": "イオ", "attribute": "STATE", "value": "補修潜水士", "kind": "STATE"},  # 役職→RANK
        {"subject": "艦長の名", "attribute": "STATE", "value": "ネモ", "kind": "STATE"},  # Xの名→呼称/主客整形
        {"subject": "モロー", "attribute": "ACT", "value": "死ぬ", "kind": "EVENT"},  # 既知実体の死→LIFE=dead
        {"subject": "磁気圏", "attribute": "ACT", "value": "死ぬ", "kind": "EVENT"},  # 未知/非生物→死亡提案しない
    ]
    r = triage_candidates(cands, existing)
    allc = r["high_new"] + r["low_new"]
    by_subj = {c["subject"]: c for c in allc}
    assert by_subj["イオ"]["suggest"] == {"attribute": "RANK"}
    assert by_subj["艦長の名"]["suggest"] == {"subject": "艦長", "attribute": "呼称"}
    assert by_subj["モロー"]["suggest"] == {"attribute": "LIFE", "value": "dead"}  # 既知実体の死
    assert "suggest" not in by_subj["磁気圏"]  # 未知/非生物の「死ぬ」は LIFE 提案しない


def _recs():
    return [
        {"subject": "モロー", "predicate": "死ぬ", "modality": "event"},
        {"subject": "ハル", "predicate": "いる", "modality": "state"},
        {"subject": "被覆", "predicate": "吸う", "modality": "event"},  # 未知実体
    ]


def test_agreement():
    r = reconcile_records(2, [{"subject": "ハル", "predicate": "いる"}], _recs(), known_entities=["ハル", "モロー"])
    assert [(f["subject"], f["predicate"]) for f in r["agreement"]] == [("ハル", "いる")]


def test_fabrication_to_llm_only():
    r = reconcile_records(2, [{"subject": "ハル", "predicate": "負傷する"}], _recs(), known_entities=["ハル"])
    assert any(f["predicate"] == "負傷する" for f in r["llm_only_check_grounding"])


def test_event_omission_includes_death():
    # モロー死亡をLLMが申告漏れ → event omission に出る(死亡検出の要)
    r = reconcile_records(2, [], _recs(), known_entities=["ハル", "モロー"])
    assert any(
        f["subject"] == "モロー" and f["predicate"] == "死ぬ" for f in r["mechanism_only_event_possible_omission"]
    )


def test_state_omission_separated():
    r = reconcile_records(2, [], _recs(), known_entities=["ハル", "モロー"])
    assert any(f["subject"] == "ハル" and f["predicate"] == "いる" for f in r["mechanism_only_state_possible_omission"])


def test_unknown_entity_filtered():
    # 被覆(未知実体)はどのバケツにも出ない
    r = reconcile_records(2, [], _recs(), known_entities=["ハル", "モロー"])
    allrecs = r["mechanism_only_state_possible_omission"] + r["mechanism_only_event_possible_omission"]
    assert all(f["subject"] != "被覆" for f in allrecs)
