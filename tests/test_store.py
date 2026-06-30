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
