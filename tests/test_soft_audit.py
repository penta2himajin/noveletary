"""soft監査の時間ゲート論理。NLIモデルは使わず、決定論スタブscorerで検証。"""

import pytest

from noveletary import Store


def _contra_all(a, b):
    """全対を矛盾と判定するスタブ(ゲートが対を絞れているかを試す)。"""
    return "contradiction"


@pytest.fixture
def s():
    return Store(":memory:")


def test_temporal_gate_filters_different_time_acts(s):
    # 別時点の行為(回収@10 / 破壊@40)はゲートで除外され、scorerに届かない
    s.import_facts(
        "main",
        [
            {"subject": "ハル", "attribute": "ACT", "value": "回収", "chapter": 10},
            {"subject": "ハル", "attribute": "ACT", "value": "破壊", "chapter": 40},
        ],
    )
    r = s.audit("main", scorer=_contra_all)
    assert r["nli_calls"] == 0  # 全部ゲートで除外
    assert r["soft_questions_created"] == []


def test_temporal_gate_filters_supersession(s):
    # 同属性・別章(中将→元帥)は supersession でゲート除外
    s.import_facts(
        "main",
        [
            {"subject": "ラインハルト", "attribute": "RANK", "value": "中将", "chapter": 3},
            {"subject": "ラインハルト", "attribute": "RANK", "value": "元帥", "chapter": 10},
        ],
    )
    r = s.audit("main", scorer=_contra_all)
    assert r["nli_calls"] == 0


def test_cross_attribute_pair_reaches_scorer(s):
    # 異属性・持続状態の共存(LIFE alive @30 / STATE 葬儀 @30)はゲートを通る=跨ぎ矛盾候補
    s.import_facts(
        "main",
        [
            {"subject": "モロー", "attribute": "LIFE", "value": "alive", "chapter": 30},
            {"subject": "モロー", "attribute": "STATE", "value": "葬儀", "chapter": 30},
        ],
    )
    r = s.audit("main", scorer=_contra_all)
    assert r["nli_calls"] == 1
    assert len(r["soft_questions_created"]) == 1
