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
    assert any(c["type"] == "USE_AFTER_FREE" for c in r["conflict"])


def test_add_commits_clean(s):
    r = s.add("main", "ハル", "LIFE", "alive", 1)
    assert r["status"] == "committed" and r["fid"].startswith("fct_")


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
    assert any(v["type"] == "USE_AFTER_FREE" for v in a["hard_violations"])


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
