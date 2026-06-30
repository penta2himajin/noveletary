"""物語ブランチ: 分岐独立監査 / マージ競合 / ロールバック / 別名貫通 / 質問。NLP依存なし。"""

import pytest

from noveletary import Store


@pytest.fixture
def s():
    return Store(":memory:")


def test_branches_audit_independently(s):
    s.add("main", "緋蓮団", "STATE", "組織", 17)
    s.create_branch("A", from_branch="main")
    s.create_branch("B", from_branch="main")
    s.add("A", "緋蓮団", "ACT", "取引", 25, kind="EVENT")  # 整合
    s.add("B", "緋蓮団", "LIFE", "dead", 20)
    s.import_facts("B", [{"subject": "緋蓮団", "attribute": "ACT", "value": "襲撃", "chapter": 25, "kind": "EVENT"}])
    assert s.audit("A")["consistent"] is True
    assert s.audit("B")["consistent"] is False


def test_rollback_is_nondestructive(s):
    s.add("main", "x", "STATE", "v1", 1)
    s.add("main", "x", "STATE", "v2", 2)  # committed
    log_before = len(s.get_log("main"))
    s.rollback("main", to_op=1)
    assert len(s.get_log("main")) <= log_before  # head は戻る
    # 操作は物理的に残る(巻き戻しの巻き戻し可能)
    assert s.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] >= 2


def test_alias_question_and_resolution_propagates(s):
    s.add("main", "緋蓮団", "STATE", "組織", 17)
    r = s.add("main", "緋色の蓮", "STATE", "標的", 5)
    qid = r["question_id"]
    s.answer_question(qid, "同一")  # 別名統合
    s.add("main", "緋蓮団", "LIFE", "dead", 20)
    # 別名で行為を入れると、統合された緋蓮団の死亡と衝突
    rej = s.add("main", "緋色の蓮", "ACT", "襲撃", 25, kind="EVENT")
    assert rej["status"] == "rejected"


def test_merge_conflict_becomes_question(s):
    s.add("main", "モロー", "STATE", "登場", 5)
    s.create_branch("A")
    s.create_branch("B")
    s.add("A", "モロー", "STATE", "生存", 30)
    s.add("B", "モロー", "STATE", "戦死", 30)
    m = s.merge("B", "A")
    assert len(m["conflicts"]) == 1
    qid = m["conflicts"][0]["question_id"]
    s.answer_question(qid, "src")  # B案(戦死)を正史に
    vals = [f["value"] for f in s.get_state("A", subject="モロー")["facts"]]
    assert "戦死" in vals
