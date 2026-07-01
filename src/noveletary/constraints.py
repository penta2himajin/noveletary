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
                    f"{f.subj}: ch{td}で{ta}={tv} の後 ch{f.t} に「{f.attr}={f.value}」(終了フルーエントの再開始)"
                    + _after_state_hint(f.attr, td),
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
                    f"{f.subj}: ch{f.t}で{ta}={tv} だが ch{future[0].t} に既存の「{future[0].attr}」"
                    + _after_state_hint(future[0].attr, f.t),
                )
            )
    return viol


def _after_state_hint(attr, death_ch):
    """rejected を受けた時の回収パターン(point-of-use)。行為は誤り、経歴/位置は生前に畳めば解消。"""
    if attr == "ACT":
        return "｜対処: 死者は行動できない=物語側を見直す(死亡章/対象が誤りかも)"
    return f"｜対処: 生前の事実なら chapter を死亡章({death_ch})より前にするか valid_to={death_ch} で死に畳む(例: 発見時に死んでいる被害者の居所)"


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
        parts = (g.value or "").split("<")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            continue  # 不正なORDER値は辺として扱わない(構築時にgate済; importで混入しても頑健)
        a, b = parts[0].strip(), parts[1].strip()
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
            "params": {"terminal_attr": "LIFE", "terminal_value": "dead", "forbidden_attrs": ["ACT", "LOC"]},
            "enabled": True,
            "scope": {},
            "note": "死後の行為(ACT)・移動(LOC)を禁止(EC慣性: 終了フルーエントの再開始禁止)。"
            "RANK(地位/職業)は静的な経歴属性=fluentでないため対象外(死後も真)。posthumous昇進を禁じたい作者は別途追加する。",
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


def check_consistency(records):
    """制約セットの構造的な矛盾・無効設定を検出する(SHACL充足性/包含の実務版)。
    テンプレートの性質上「空ストーリーは常に充足」なので純粋な不充足は稀。代わりに
    作者設定ミスを拾う: 増減両立 monotone / 重複 / 対応forbidの無いrelease / 全体releaseで死蔵forbid。
    遅延検査(既定)。store側の設定で add 時に走らせることもできる。"""
    import json as _json

    enabled = [r for r in records if r.get("enabled", True)]
    issues = []

    # ① 同一attrに nondecreasing と nonincreasing(=実質定数強制; 多くは設定ミス)
    mon = {}
    for r in enabled:
        if r["template"] == "monotone":
            mon.setdefault(r["params"]["attr"], set()).add(r["params"].get("direction", "nondecreasing"))
    for attr, ds in mon.items():
        if {"nondecreasing", "nonincreasing"} <= ds:
            issues.append({"kind": "contradictory_monotone", "detail": f"{attr}に増加と減少の両制約(実質定数強制)"})

    # ② 重複(同一template・同一params・同一scope)
    seen = set()
    for r in enabled:
        key = (
            r["template"],
            _json.dumps(r.get("params", {}), sort_keys=True, ensure_ascii=False),
            _json.dumps(r.get("scope", {}), sort_keys=True, ensure_ascii=False),
        )
        if key in seen:
            issues.append({"kind": "duplicate", "detail": f"{r['template']} の重複(同一params)"})
        seen.add(key)

    forbids = [r for r in enabled if r["template"] == "forbid_after_state"]
    releases = [r for r in enabled if r["template"] == "release"]

    def _match(fb, rel):
        return fb["params"]["terminal_attr"] == rel["params"].get("terminal_attr") and fb["params"][
            "terminal_value"
        ] == rel["params"].get("terminal_value")

    # ③ 対応するforbidの無いrelease(no-op)
    for rel in releases:
        if not any(_match(fb, rel) for fb in forbids):
            p = rel["params"]
            issues.append(
                {
                    "kind": "orphan_release",
                    "detail": f"{p.get('terminal_attr')}={p.get('terminal_value')} のreleaseに対応forbidが無い(no-op)",
                }
            )

    # ④ 全体release(subject未指定)で常に無効化されるforbid(死蔵)
    for fb in forbids:
        if fb.get("scope", {}).get("subject"):
            continue
        if any(_match(fb, rel) and rel["params"].get("subject") is None for rel in releases):
            p = fb["params"]
            issues.append(
                {
                    "kind": "shadowed_forbid",
                    "detail": f"{p['terminal_attr']}={p['terminal_value']} のforbidが全体releaseで常時無効化",
                }
            )

    return issues
