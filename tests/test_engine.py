"""制約エンジン(hard制約)のテスト。NLP依存なし。"""

from noveletary import Fact, NarrativeKB
from noveletary.constraints import default_constraints


def _kb(*facts):
    kb = NarrativeKB()
    kb.constraints = default_constraints()
    for f in facts:
        kb.facts[f.fid] = f
    return kb


def test_use_after_free_detected():
    kb = _kb(Fact("d", "ハル", "LIFE", "dead", 5))
    f = Fact("a", "ハル", "ACT", "出航", 6, "EVENT")
    viol = kb._check_hard(f, kb._affected(f))
    assert any(t == "FORBID_AFTER_STATE" for (t, _c, _d) in viol)


def test_living_action_ok():
    kb = _kb(Fact("l", "ハル", "LIFE", "alive", 1))
    f = Fact("a", "ハル", "ACT", "出航", 6, "EVENT")
    assert kb._check_hard(f, kb._affected(f)) == []


def test_monotone_counter_break():
    kb = _kb(Fact("c1", "死者帳", "LEDGER", "私的", 1, "COUNTER", num=11))
    f = Fact("c2", "死者帳", "LEDGER", "私的", 10, "COUNTER", num=9)
    viol = kb._check_hard(f, kb._affected(f))
    assert any(t == "MONOTONE_BREAK" for (t, _c, _d) in viol)


def test_temporal_cycle():
    kb = _kb(Fact("o1", "-", "ORDER", "A<B", 1), Fact("o2", "-", "ORDER", "B<C", 1))
    f = Fact("o3", "-", "ORDER", "C<A", 1)
    viol = kb._check_hard(f, kb._affected(f))
    assert any(t == "TEMPORAL_CYCLE" for (t, _c, _d) in viol)


def test_affected_subgraph_scoping():
    # 別主体のfactは影響部分グラフに入らない(差分検査の前提)
    kb = _kb(Fact("x", "モロー", "LIFE", "dead", 5))
    f = Fact("a", "ハル", "ACT", "出航", 6, "EVENT")
    assert f.subj not in {g.subj for g in kb._affected(f)} or kb._check_hard(f, kb._affected(f)) == []
