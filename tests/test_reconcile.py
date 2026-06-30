"""reconcile_records の照合ロジック。決定論的(モデル不要)・合成レコードで検証。"""

from noveletary.reconcile import reconcile_records


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
