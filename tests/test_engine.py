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


def test_forbid_after_state_detail_has_recovery_hint():
    # rejected の conflict メッセージに point-of-use の回収ヒントが載る
    kb = _kb(Fact("d", "被害者", "LIFE", "dead", 1))
    # 位置(LOC)=生前に畳めば解消、のヒント
    loc = kb._check_hard(Fact("l", "被害者", "LOC", "工房", 1), kb._affected(Fact("l", "被害者", "LOC", "工房", 1)))
    assert any("valid_to" in d for (t, _c, d) in loc if t == "FORBID_AFTER_STATE")
    # 行為(ACT)=死者は行動できない、のヒント
    act = kb._check_hard(
        Fact("a", "被害者", "ACT", "歩く", 2, "EVENT"), kb._affected(Fact("a", "被害者", "ACT", "歩く", 2))
    )
    assert any("死者は行動できない" in d for (t, _c, d) in act if t == "FORBID_AFTER_STATE")


def test_living_action_ok():
    kb = _kb(Fact("l", "ハル", "LIFE", "alive", 1))
    f = Fact("a", "ハル", "ACT", "出航", 6, "EVENT")
    assert kb._check_hard(f, kb._affected(f)) == []


def test_rank_persists_after_death():
    # RANK(地位/職業)は静的な経歴属性。死後も真で、forbid_after_state の対象外。
    # 被害者を「死んでいる」と同章で「時計師だった」と書けること。
    kb = _kb(Fact("d", "セバスチャン", "LIFE", "dead", 1))
    f = Fact("r", "セバスチャン", "RANK", "時計師", 1)
    assert kb._check_hard(f, kb._affected(f)) == []


def test_location_after_death_still_forbidden():
    # 位置(LOC)はfluent: 死者が後の章で移動するのは矛盾。過剰除去の回帰ガード。
    kb = _kb(Fact("d", "セバスチャン", "LIFE", "dead", 1))
    f = Fact("loc", "セバスチャン", "LOC", "酒場", 5)
    viol = kb._check_hard(f, kb._affected(f))
    assert any(t == "FORBID_AFTER_STATE" for (t, _c, _d) in viol)


def test_surface_similar_latin_names():
    # ラテン字母は字母が小さく偶然一致しやすい → 共有トークン(タイトル除く)で判定
    from noveletary.engine import surface_similar

    assert surface_similar("King Aldric", "General Kessik") is False  # 共有語なし=別人
    assert surface_similar("King Aldric", "King Bertram") is False  # 共有はタイトルのみ
    assert surface_similar("King Aldric", "Aldric the Bold") is True  # 名前(Aldric)を共有


def test_surface_similar_japanese_preserved():
    # 日本語名(空白なし)は従来の文字集合Jaccard≥0.3を維持
    from noveletary.engine import surface_similar

    assert surface_similar("マイケル・コール", "セバスチャン・コール") is True  # 「コール」共有
    assert surface_similar("ホームズ", "シャーロック・ホームズ") is True
    assert surface_similar("イオ", "モロー") is False


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
