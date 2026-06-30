"""
engine.py — 制約維持された物語知識ベース(最小実装)
構築(add/update/delete)と検証(制約検査)を単一エンジンに統合。
- ハード制約は書込時にmutationをgate(prevention)
- 未解決(ソフト)は LLM でなく「作者への質問」を発行(human-in-the-loop)
- 検査は影響部分グラフのみ(差分検査, 全スキャンしない)
"""

from dataclasses import dataclass, field
from typing import Optional

from z3 import Int, Solver, unsat


@dataclass
class Fact:
    fid: str
    subj: str
    attr: str  # LIFE / ACT / LOC / RANK / LEDGER / ALIAS / ORDER ...
    value: Optional[str]
    t: int  # 物語時間(章)
    kind: str = "STATE"  # STATE / EVENT / LEDGER_LEVEL / ALIAS / ORDER
    num: Optional[int] = None
    deps: list = field(default_factory=list)


class Question(Exception):
    def __init__(self, q, options, on_answer):
        self.q = q
        self.options = options
        self.on_answer = on_answer


class NarrativeKB:
    def __init__(self):
        self.facts = {}  # fid -> Fact
        self.aliases = {}  # surface -> canonical
        self.cannot_link = set()  # frozenset({a,b})
        self.log = []

    # ---------- 影響部分グラフ(差分検査の肝) ----------
    def _affected(self, f: Fact):
        """新factが触れる既存factだけを返す(同一主体 or 同一台帳系列 or 時間近傍)"""
        out = []
        for g in self.facts.values():
            same_subj = self._canon(g.subj) == self._canon(f.subj)
            same_series = g.attr == "LEDGER" and f.attr == "LEDGER" and g.value == f.value
            if same_subj or same_series:
                out.append(g)
        return out

    def _canon(self, name):
        return self.aliases.get(name, name)

    # ---------- ハード制約検査(影響部分グラフ上) ----------
    def _check_hard(self, f: Fact, scope):
        viol = []
        cs = self._canon(f.subj)
        # (1) use-after-free: 死亡後の行為/状態
        deaths = [g for g in scope if g.attr == "LIFE" and g.value == "dead"]
        if deaths:
            td = min(g.t for g in deaths)
            if f.attr in ("ACT", "LOC", "RANK") and f.t >= td:
                viol.append(
                    (
                        "USE_AFTER_FREE",
                        [g.fid for g in deaths if g.t == td] + [f.fid],
                        f"{cs}: ch{td}死亡後 ch{f.t}で「{f.attr}={f.value}」",
                    )
                )
        # 逆: 既存ACTより後に死亡を挿入しても、既存の未来ACTがあれば矛盾
        if f.attr == "LIFE" and f.value == "dead":
            future_acts = [g for g in scope if g.attr in ("ACT", "LOC", "RANK") and g.t >= f.t]
            if future_acts:
                viol.append(
                    (
                        "USE_AFTER_FREE",
                        [f.fid] + [g.fid for g in future_acts],
                        f"{cs}: ch{f.t}死亡だが ch{future_acts[0].t}に既存の行為あり",
                    )
                )
        # (2) 台帳: 単調カウンタ / 保存則(数値)
        if f.attr == "LEDGER" and f.num is not None:
            same = [g for g in scope if g.attr == "LEDGER" and g.value == f.value and g.num is not None]
            same_sorted = sorted(same + [f], key=lambda x: x.t)
            if f.kind == "COUNTER":
                for i in range(1, len(same_sorted)):
                    if same_sorted[i].num < same_sorted[i - 1].num:
                        viol.append(
                            (
                                "MONOTONE_BREAK",
                                [same_sorted[i - 1].fid, same_sorted[i].fid],
                                f"{f.value}: ch{same_sorted[i - 1].t}={same_sorted[i - 1].num} → ch{same_sorted[i].t}={same_sorted[i].num} (減少)",
                            )
                        )
        # (3) 時間順序の循環(z3)
        orders = [g for g in scope if g.attr == "ORDER"] + ([f] if f.attr == "ORDER" else [])
        if f.attr == "ORDER" and orders:
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
                viol.append(("TEMPORAL_CYCLE", [str(c) for c in s.unsat_core()], "時間順序に循環"))
        return viol

    # ---------- ソフト未解決 → 作者質問 ----------
    def _soft_questions(self, f: Fact):
        qs = []
        # alias曖昧: 新主体名が既存と表層的に近い
        cs = self._canon(f.subj)
        for g in list(self.facts.values()):
            gs = self._canon(g.subj)
            if (
                gs != cs
                and self._similar(cs, gs)
                and frozenset({cs, gs}) not in self.cannot_link
                and self.aliases.get(f.subj) != gs
            ):
                qs.append(("ALIAS", cs, gs))
                break
        return qs

    def _similar(self, a, b):
        # 文字集合Jaccard(漢字共有を捉える)
        A, B = set(a), set(b)
        return (len(A & B) / len(A | B) if (A | B) else 0) >= 0.3

    # ---------- mutation API ----------
    def add(self, f: Fact, author=None):
        scope = self._affected(f)
        viol = self._check_hard(f, scope)
        if viol:
            self.log.append(("REJECT", f.fid, viol))
            return ("REJECT", viol)
        qs = self._soft_questions(f)
        if qs and author is not None:
            typ, a, b = qs[0]
            ans = author(f"『{a}』と既存の『{b}』は同一指示対象か?", ["同一", "別物"])
            if ans == "同一":
                self.aliases[a] = b
                self.log.append(("ALIAS_MERGE", a, b))
            else:
                self.cannot_link.add(frozenset({a, b}))
                self.log.append(("CANNOT_LINK", a, b))
        self.facts[f.fid] = f
        self.log.append(("COMMIT", f.fid, len(scope)))
        return ("COMMIT", f.fid, len(scope))

    def update(self, fid, new_value, new_num=None, author=None):
        old = self.facts[fid]
        # supersession: 旧を残しvalid-time終端、新を追加。retcon検査=影響部分グラフ再検査
        nf = Fact(fid + "'", old.subj, old.attr, new_value, old.t, old.kind, new_num)
        scope = [g for g in self._affected(nf) if g.fid != fid]
        viol = self._check_hard(nf, scope)
        if viol:
            return ("REJECT(retcon)", viol)
        self.facts[nf.fid] = nf
        self.log.append(("SUPERSEDE", fid, nf.fid))
        return ("COMMIT", nf.fid, len(scope))

    def delete(self, fid):
        # 参照整合性: このfactに依存する他factの孤児化
        orphans = [g for g in self.facts.values() if fid in g.deps]
        if orphans:
            return ("REJECT(orphan)", [g.fid for g in orphans])
        del self.facts[fid]
        self.log.append(("DELETE", fid))
        return ("COMMIT_DELETE", fid)
