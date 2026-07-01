"""
engine.py — 制約維持された物語知識ベース(最小実装)
構築(add/update/delete)と検証(制約検査)を単一エンジンに統合。
- ハード制約は書込時にmutationをgate(prevention)。規則はコード直書きでなく
  constraints.py のテンプレート実行器に委譲(規則は store の操作ログでversioned)。
- 未解決(ソフト)は LLM でなく「作者への質問」を発行(human-in-the-loop)
- 検査は影響部分グラフのみ(差分検査, 全スキャンしない)
"""

from dataclasses import dataclass, field
from typing import Optional

from . import constraints as _constraints

# 別名候補判定で共有されても同一性の弱いタイトル/前置詞/助詞語(主に英字名の誤発火抑制用)
_NAME_STOPWORDS = frozenset(
    {
        "the",
        "of",
        "a",
        "an",
        "de",
        "la",
        "le",
        "du",
        "von",
        "van",
        "der",
        "den",
        "el",
        "al",
        "lord",
        "lady",
        "king",
        "queen",
        "prince",
        "princess",
        "duke",
        "duchess",
        "earl",
        "baron",
        "sir",
        "dame",
        "general",
        "captain",
        "commander",
        "master",
        "mistress",
        "saint",
        "st",
        "dr",
        "mr",
        "mrs",
        "ms",
        "the",
    }
)


def surface_similar(a, b):
    """2つの呼称が同一指示対象“候補”か(表層のみ・言語非依存に頑健化)。
    複数語(空白区切り。英字名に多い)は、タイトル/前置詞を除いた共有トークン(語)があれば候補。
      → 「King Aldric」と「General Kessik」は共有語なし=非候補(小さいラテン字母での偶然一致を排除)。
      → 「King Aldric」と「Aldric the Bold」は "aldric" 共有=候補。
    単一語(日本語名や「イオ・チェン」等、空白なし)は従来の文字集合Jaccard≥0.3(漢字/仮名の共有を捉える)。"""
    ta, tb = (a or "").split(), (b or "").split()
    if len(ta) > 1 or len(tb) > 1:
        sa = {t.lower() for t in ta if len(t) >= 2 and t.lower() not in _NAME_STOPWORDS}
        sb = {t.lower() for t in tb if len(t) >= 2 and t.lower() not in _NAME_STOPWORDS}
        return bool(sa & sb)
    A, B = set(a or ""), set(b or "")
    return (len(A & B) / len(A | B) if (A | B) else 0) >= 0.3


@dataclass
class Fact:
    fid: str
    subj: str
    attr: str  # LIFE / ACT / LOC / RANK / LEDGER / ALIAS / ORDER ...
    value: Optional[str]
    t: int  # valid-time(物語内時間)の開始章。フルーエントは区間 [t, valid_to) で保持
    kind: str = "STATE"  # STATE / EVENT / LEDGER_LEVEL / ALIAS / ORDER
    num: Optional[int] = None
    deps: list = field(default_factory=list)
    narrated_in: Optional[int] = None  # discourse-time(語りの章)。None なら valid-time(t)と同値=順送り
    valid_to: Optional[int] = None  # valid-time(物語内時間)の終了章(排他)。None なら +∞(開区間/supersessionで暗黙終了)

    @property
    def narrated(self) -> int:
        """discourse-time(語りの章)。未指定なら valid-time(t)に等しい。"""
        return self.narrated_in if self.narrated_in is not None else self.t

    def holds_at(self, chapter: int) -> bool:
        """valid-time chapter で保持しているか。区間 [t, valid_to) に含まれるか。"""
        return self.t <= chapter and (self.valid_to is None or chapter < self.valid_to)


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
        self.constraints = []  # 有効な制約レコード(storeが materialize して差し込む)
        self.log = []

    # ---------- 影響部分グラフ(差分検査の肝) ----------
    def _affected(self, f: Fact):
        """新factが触れる既存factだけを返す(同一主体 or 同一台帳系列 or 時間近傍)"""
        out = []
        for g in self.facts.values():
            same_subj = self._canon(g.subj) == self._canon(f.subj)
            same_series = g.attr == f.attr and g.value == f.value  # 任意attrの系列(monotone等)
            if same_subj or same_series:
                out.append(g)
        return out

    def _canon(self, name):
        return self.aliases.get(name, name)

    # ---------- ハード制約検査(制約テンプレート実行器に委譲) ----------
    def _check_hard(self, f: Fact, scope, constraint_records=None):
        """有効な制約レコードを実行する。規則はコードに直書きせず constraints.py の
        テンプレート(forbid_after_state/monotone/acyclic/release)に params を渡して実行。
        constraint_records 未指定なら self.constraints(storeが差し込む)を使う。"""
        records = constraint_records if constraint_records is not None else self.constraints
        return _constraints.check(f, scope, records)

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
        return surface_similar(a, b)

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
