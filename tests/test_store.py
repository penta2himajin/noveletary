"""ストア層: 構築gate / 取込 / 監査 / 永続化 のテスト。NLP依存なし。"""

import pytest

from noveletary import Store


@pytest.fixture
def s():
    return Store(":memory:")


def test_add_gate_use_after_free(s):
    s.add("main", "ハル", "ACT", "出航", 6, kind="EVENT")
    r = s.add("main", "ハル", "LIFE", "dead", 5)
    assert r["status"] == "rejected"
    assert any(c["type"] == "FORBID_AFTER_STATE" for c in r["conflict"])


def test_add_commits_clean(s):
    r = s.add("main", "ハル", "LIFE", "alive", 1)
    assert r["status"] == "committed" and r["fid"].startswith("fct_")


# ---- Phase A: discourse 軸 narrated_in(語りの章) を valid-time(物語内時間) と分離 ----
def test_narrated_in_defaults_to_valid_time(s):
    s.add("main", "ハル", "STATE", "在室", 3)
    f = s.get_state("main")["facts"][0]
    assert f["chapter"] == 3 and f["narrated_in"] == 3  # 既定: 語り=物語内時間


def test_narrated_in_independent_of_valid_time(s):
    # 第5章の回想で、物語内時間=第1章の事実を確定
    s.add("main", "ハル", "STATE", "幼少期に港にいた", 1, narrated_in=5)
    f = s.get_state("main")["facts"][0]
    assert f["chapter"] == 1 and f["narrated_in"] == 5


def test_as_of_narrated_reader_knowledge_slice(s):
    # 物語内では1章から真だが、開示は10章(伏線/叙述トリック)
    s.add("main", "犯人", "STATE", "正体X", 1, narrated_in=10)
    assert len(s.get_state("main", as_of_chapter=1)["facts"]) == 1  # 世界には第1章から在る
    assert len(s.get_state("main", as_of_narrated=3)["facts"]) == 0  # 第3章まで読んだ読者は未だ知らない
    assert len(s.get_state("main", as_of_narrated=10)["facts"]) == 1  # 第10章で開示


def test_valid_time_constraints_unchanged_by_narration(s):
    # 制約は valid-time 基準: narrated_in をずらしても死後行為は弾く(回帰ガード)
    s.add("main", "X", "LIFE", "dead", 1, narrated_in=2)
    r = s.add("main", "X", "ACT", "歩く", 3, narrated_in=2, kind="EVENT")
    assert r["status"] == "rejected"


def test_narrated_in_survives_snapshot(s):
    # スナップショット(25op毎)を跨いでも narrated_in が保持される(6/7要素タプル互換)
    for i in range(30):
        s.add("main", f"E{i}", "STATE", "x", 1, narrated_in=7)
    facts = s.get_state("main")["facts"]
    assert len(facts) == 30 and all(f["narrated_in"] == 7 for f in facts)


def test_as_of_narrated_respects_snapshot(s):
    # スナップショット以前のfactにも narrated スライスが効く(以前は素通りした)
    for i in range(26):  # op25のスナップショットを跨がせる
        s.add("main", f"E{i}", "STATE", "x", 1, narrated_in=9)
    assert s.get_state("main", as_of_narrated=3)["facts"] == []  # 第3章読者には未開示


def test_as_of_chapter_respects_snapshot(s):
    # valid-time スライスもスナップショット復元factに効く(既存バグの回帰ガード)
    for i in range(26):
        s.add("main", f"L{i}", "STATE", "x", 50)  # valid-time=50
    assert s.get_state("main", as_of_chapter=10)["facts"] == []  # 第10章時点には未だ無い


def test_assert_alias_merges_unrelated_names(s):
    # 表層が似ていない別名(偽名)を作者が明示統合できる
    s.add("main", "マイケル・コール", "STATE", "相続人", 3)
    s.add("main", "ミスター・グレイ", "STATE", "灰色の紳士", 2)
    r = s.assert_alias("main", "ミスター・グレイ", "マイケル・コール")
    assert r["status"] == "aliased"
    assert s.get_state("main")["aliases"].get("ミスター・グレイ") == "マイケル・コール"


def test_assert_alias_makes_identity_checkable(s):
    # 別名統合で「グレイの行為」が故人マイケルの死後行為として検出される
    s.add("main", "マイケル・コール", "LIFE", "dead", 1)
    s.add("main", "ミスター・グレイ", "ACT", "歩く", 2, kind="EVENT")  # 別主体なら矛盾なし
    r = s.assert_alias("main", "ミスター・グレイ", "マイケル・コール")
    assert any(v["type"] == "FORBID_AFTER_STATE" for v in r["hard_violations"])


def test_assert_distinct_suppresses_alias_question(s):
    # 別人と明示固定すれば、同姓でも以後ALIAS質問が出ない
    s.add("main", "セバスチャン・コール", "RANK", "時計師", 0)
    s.assert_distinct("main", "マイケル・コール", "セバスチャン・コール")
    r = s.add("main", "マイケル・コール", "STATE", "甥", 3)
    assert "question_id" not in r


def test_add_many_atomic_rolls_back_on_reject(s):
    # 2件目が矛盾 → atomic ならバッチ全体を巻き戻し、何も適用しない
    facts = [
        {"subject": "ハル", "attribute": "LIFE", "value": "dead", "chapter": 1},
        {"subject": "ハル", "attribute": "ACT", "value": "出航", "chapter": 2, "kind": "EVENT"},
    ]
    r = s.add_many("main", facts, atomic=True)
    assert r["applied"] is False
    assert any(x["status"] == "rejected" for x in r["results"])
    assert s.get_state("main")["facts"] == []  # 1件目(dead)も巻き戻る


def test_add_many_non_atomic_keeps_partial(s):
    # 既定(atomic=False)は従来通り逐次適用: 1件目はcommitされ残る
    facts = [
        {"subject": "ハル", "attribute": "LIFE", "value": "dead", "chapter": 1},
        {"subject": "ハル", "attribute": "ACT", "value": "出航", "chapter": 2, "kind": "EVENT"},
    ]
    r = s.add_many("main", facts, atomic=False)
    assert r["applied"] is True
    states = s.get_state("main")["facts"]
    assert len(states) == 1 and states[0]["value"] == "dead"


def test_add_many_atomic_clears_questions(s):
    # バッチ中に生んだ alias 質問も atomic 巻き戻しで取り消す
    s.add("main", "シャーロック・ホームズ", "RANK", "探偵", 1)
    before = len(s.list_questions("main"))
    facts = [
        {"subject": "ホームズ", "attribute": "STATE", "value": "在室", "chapter": 1},  # ALIAS質問が出る
        {"subject": "X", "attribute": "LIFE", "value": "dead", "chapter": 1},
        {"subject": "X", "attribute": "ACT", "value": "歩く", "chapter": 2, "kind": "EVENT"},  # reject
    ]
    r = s.add_many("main", facts, atomic=True)
    assert r["applied"] is False
    assert len(s.list_questions("main")) == before  # 質問が増えていない


def test_import_does_not_gate_but_audit_finds(s):
    s.import_facts(
        "main",
        [
            {"subject": "艦", "attribute": "LIFE", "value": "dead", "chapter": 20},
            {"subject": "艦", "attribute": "ACT", "value": "出航", "chapter": 25, "kind": "EVENT"},
        ],
    )
    a = s.audit("main")
    assert a["consistent"] is False
    assert any(v["type"] == "FORBID_AFTER_STATE" for v in a["hard_violations"])


def test_bitemporal_slice(s):
    s.add("main", "艦", "STATE", "正常", 5)
    s.add("main", "艦", "LIFE", "dead", 20)
    early = s.get_state("main", as_of_chapter=15)
    late = s.get_state("main", as_of_chapter=25)
    assert all(f["attribute"] != "LIFE" for f in early["facts"])
    assert any(f["attribute"] == "LIFE" for f in late["facts"])


def test_persistence_roundtrip(tmp_path):
    db = str(tmp_path / "n.db")
    s1 = Store(db)
    s1.add("main", "ハル", "LIFE", "alive", 1)
    s2 = Store(db)  # 別インスタンスで再オープン
    assert any(f["subject"] == "ハル" for f in s2.get_state("main")["facts"])
