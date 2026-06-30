"""
constraints.py — 制約テンプレートの実行器(Event Calculus 接地)

engine.py に直書きされていた hard 制約規則を、データ駆動のテンプレート実行に置き換える。
各テンプレートは「制約の型」であり作品非依存。作品固有の具体(LIFE/dead/LEDGER…)は
constraint レコードの params に外出しされ、store の操作ログで versioning される。

テンプレート(SHACLのパラメータ化コンポーネント方式 / EC接地):
  forbid_after_state : 終了フルーエントの再開始禁止(EC状態制約+慣性)。死後の行為など。
  monotone           : 数値フルーエントの単調性(客観曲線)。台帳など。
  acyclic            : 順序関係の無循環(順序の推移性)。時間順序など。
  release            : 終了フルーエントの解放(EC Release)。forbid_after_state の例外。
                       「派生作では死者復活を許す」をブランチ単位で表現する。

制約レコード形式:
  {"template": <name>, "params": {...}, "enabled": True,
   "scope": {"subject": <name>?}, "note": <str>}
"""

from z3 import Int, Solver, unsat


def _in_scope(fact, scope):
    if not scope:
        return True
    if "subject" in scope and scope["subject"] != fact.subj:
        return False
    return True


def _released(subject, terminal_attr, terminal_value, releases):
    """この主体のこの終端状態を解放する release があるか(EC Release)。"""
    for r in releases:
        p = r["params"]
        if p.get("terminal_attr") == terminal_attr and p.get("terminal_value") == terminal_value:
            rs = p.get("subject")
            if rs is None or rs == subject:  # subject未指定の release は全主体に効く
                return True
    return False


# ---------- テンプレート: (fact, scope_facts, params, releases) -> [violation] ----------
def _forbid_after_state(f, scope, params, releases):
    ta = params["terminal_attr"]
    tv = params["terminal_value"]
    forbidden = params.get("forbidden_attrs", [])
    if _released(f.subj, ta, tv, releases):
        return []
    viol = []
    terminals = [g for g in scope if g.attr == ta and g.value == tv]
    if terminals:
        td = min(g.t for g in terminals)
        if f.attr in forbidden and f.t >= td:
            viol.append(
                (
                    "FORBID_AFTER_STATE",
                    [g.fid for g in terminals if g.t == td] + [f.fid],
                    f"{f.subj}: ch{td}で{ta}={tv} の後 ch{f.t} に「{f.attr}={f.value}」(終了フルーエントの再開始)",
                )
            )
    # 逆: 終端状態を後から挿入しても、既存の未来の禁止行為があれば矛盾
    if f.attr == ta and f.value == tv:
        future = [g for g in scope if g.attr in forbidden and g.t >= f.t]
        if future:
            viol.append(
                (
                    "FORBID_AFTER_STATE",
                    [f.fid] + [g.fid for g in future],
                    f"{f.subj}: ch{f.t}で{ta}={tv} だが ch{future[0].t} に既存の「{future[0].attr}」",
                )
            )
    return viol


def _monotone(f, scope, params, releases):
    attr = params["attr"]
    direction = params.get("direction", "nondecreasing")
    if f.attr != attr or f.num is None:
        return []
    same = [g for g in scope if g.attr == attr and g.value == f.value and g.num is not None]
    series = sorted(same + [f], key=lambda x: x.t)
    viol = []
    for i in range(1, len(series)):
        prev, cur = series[i - 1], series[i]
        bad = cur.num < prev.num if direction == "nondecreasing" else cur.num > prev.num
        if bad:
            viol.append(
                (
                    "MONOTONE_BREAK",
                    [prev.fid, cur.fid],
                    f"{f.value}({attr},{direction}): ch{prev.t}={prev.num} → ch{cur.t}={cur.num}",
                )
            )
    return viol


def _acyclic(f, scope, params, releases):
    oa = params["order_attr"]
    if f.attr != oa:
        return []
    orders = [g for g in scope if g.attr == oa] + [f]
    s = Solver()
    s.set(unsat_core=True)
    vars = {}

    def v(x):
        if x not in vars:
            vars[x] = Int(f"t_{x}")
        return vars[x]

    for g in orders:
        a, b = g.value.split("<")
        s.assert_and_track(v(a) < v(b), g.fid)
    if s.check() == unsat:
        return [("TEMPORAL_CYCLE", [str(c) for c in s.unsat_core()], f"{oa}に循環")]
    return []


TEMPLATES = {
    "forbid_after_state": _forbid_after_state,
    "monotone": _monotone,
    "acyclic": _acyclic,
    # "release" はテンプレート実行ではなく forbid_after_state の例外として作用(下記 check)
}


def check(fact, scope, records):
    """有効な制約レコードを実行し、違反のリストを返す。release は forbid_after_state に渡す。"""
    enabled = [r for r in records if r.get("enabled", True)]
    releases = [r for r in enabled if r["template"] == "release"]
    viol = []
    for r in enabled:
        t = r["template"]
        if t == "release":
            continue
        fn = TEMPLATES.get(t)
        if fn is None or not _in_scope(fact, r.get("scope")):
            continue
        viol += fn(fact, scope, r.get("params", {}), releases)
    return viol


def default_constraints():
    """削除可能なデフォルト制約セット(EC慣性等)。新規ルートブランチに種として投入される。
    これらは特権でなく単なるデータで、作者が disable/remove できる。"""
    return [
        {
            "template": "forbid_after_state",
            "params": {"terminal_attr": "LIFE", "terminal_value": "dead", "forbidden_attrs": ["ACT", "LOC", "RANK"]},
            "enabled": True,
            "scope": {},
            "note": "死後の行為を禁止(EC慣性: 終了フルーエントの再開始禁止)",
        },
        {
            "template": "monotone",
            "params": {"attr": "LEDGER", "direction": "nondecreasing"},
            "enabled": True,
            "scope": {},
            "note": "台帳の単調増加(客観曲線)",
        },
        {
            "template": "acyclic",
            "params": {"order_attr": "ORDER"},
            "enabled": True,
            "scope": {},
            "note": "時間順序の無循環",
        },
    ]
