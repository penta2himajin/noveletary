"""制約の外部化: デフォルト種投入 / disable / release / ブランチ別制約。"""

import pytest

from noveletary import Store
from noveletary import constraints as C
from noveletary.engine import Fact, NarrativeKB


@pytest.fixture
def s():
    return Store(":memory:")


# ---- テンプレート実行器(エンジン非依存) ----
def test_release_overrides_forbid():
    kb = NarrativeKB()
    kb.constraints = C.default_constraints() + [
        {
            "template": "release",
            "params": {"terminal_attr": "LIFE", "terminal_value": "dead", "subject": "X"},
            "enabled": True,
        }
    ]
    kb.facts["d"] = Fact("d", "X", "LIFE", "dead", 5)
    f = Fact("a", "X", "ACT", "再登場", 6, "EVENT")
    assert kb._check_hard(f, kb._affected(f)) == []  # release により許可


def test_disable_removes_check():
    kb = NarrativeKB()
    kb.constraints = [dict(c, enabled=False) for c in C.default_constraints()]
    kb.facts["d"] = Fact("d", "X", "LIFE", "dead", 5)
    f = Fact("a", "X", "ACT", "出航", 6, "EVENT")
    assert kb._check_hard(f, kb._affected(f)) == []


# ---- store: 操作ログでversioned ----
def test_defaults_seeded(s):
    templates = {c["template"] for c in s.list_constraints("main")}
    assert templates == {"forbid_after_state", "monotone", "acyclic"}


def test_disable_default_lets_through(s):
    s.add("main", "ハル", "LIFE", "dead", 5)
    cid = next(c["cid"] for c in s.list_constraints("main") if c["template"] == "forbid_after_state")
    s.set_constraint_enabled("main", cid, False)
    assert s.add("main", "ハル", "ACT", "出航", 6, kind="EVENT")["status"] == "committed"


def test_release_per_branch(s):
    # main では死後行為を弾き、派生Bでは release で復活を許す
    s.add("main", "キャラ", "LIFE", "dead", 10)
    s.create_branch("B")
    s.add_constraint("B", "release", {"terminal_attr": "LIFE", "terminal_value": "dead", "subject": "キャラ"})
    assert s.add("main", "キャラ", "ACT", "再登場", 15, kind="EVENT")["status"] == "rejected"
    assert s.add("B", "キャラ", "ACT", "再登場", 15, kind="EVENT")["status"] == "committed"


def test_constraint_versioning_rollback(s):
    # 制約の削除は操作ログ上にあり、ロールバックで復活する
    cid = next(c["cid"] for c in s.list_constraints("main") if c["template"] == "forbid_after_state")
    op_before = s.get_log("main")[0]["op_id"]
    s.remove_constraint("main", cid)
    assert all(c["cid"] != cid for c in s.list_constraints("main"))
    s.rollback("main", to_op=op_before)
    assert any(c["cid"] == cid for c in s.list_constraints("main"))


# ---- 充足性チェック(構造的矛盾検出) ----
def test_consistency_clean_defaults(s):
    assert s.check_constraints("main")["consistent"] is True


def test_consistency_detects_contradictory_monotone(s):
    s.add_constraint("main", "monotone", {"attr": "LEDGER", "direction": "nonincreasing"})
    issues = s.check_constraints("main")["issues"]
    assert any(i["kind"] == "contradictory_monotone" for i in issues)


def test_consistency_detects_orphan_release(s):
    s.add_constraint("main", "release", {"terminal_attr": "NONE", "terminal_value": "x"})
    assert any(i["kind"] == "orphan_release" for i in s.check_constraints("main")["issues"])


def test_eager_toggle_attaches_warnings(s):
    assert "consistency_warnings" not in s.add_constraint("main", "release", {"terminal_attr": "A", "terminal_value": "b"})
    s.set_constraint_check_eager(True)
    r = s.add_constraint("main", "release", {"terminal_attr": "C", "terminal_value": "d"})
    assert any(i["kind"] == "orphan_release" for i in r.get("consistency_warnings", []))
