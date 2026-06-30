"""
store.py — 制約維持された物語KB + 物語ブランチ + 永続化(SQLite)
MCPサーバーの中核。engine(制約エンジン)を操作ログ/ブランチ層で包む。

設計:
- operations: 不変・append-only。唯一の真実の源。事実・別名・制約の全変更がここに乗る。
- branches : head_op を指すポインタ。分岐=1行。
- open_questions: 未解決(alias/競合/意味矛盾)を永続化。作者oracleが答える。
- 状態は materialize(replay)で導出。snapshotで高速化。
- add は hard制約で gate(prevention) / import は gateせず後で audit(detection)。
- 制約(hard規則)もデータ。操作ログで versioned され、ブランチ単位で分岐・ロールバックする。
"""

import json
import os
import sqlite3
import uuid

from .constraints import TEMPLATES, check_consistency, default_constraints
from .engine import Fact, NarrativeKB


class Store:
    def __init__(self, path="data/narrative.db"):
        new = not os.path.exists(path) if path != ":memory:" else True
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        if new and self._branch_id("main") is None:
            self._insert_branch("main", None, None)
            for c in default_constraints():  # 削除可能なデフォルト制約を種として投入(EC慣性等)
                self._commit("main", "add_constraint", {"cid": self._new_cid(), **c}, 0, "seed")

    # ---------------- schema ----------------
    def _init_schema(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS operations(
          op_id INTEGER PRIMARY KEY AUTOINCREMENT,
          parent_id INTEGER, branch_id INTEGER,
          op_type TEXT, payload TEXT,
          valid_from INTEGER, author TEXT, ts INTEGER);
        CREATE TABLE IF NOT EXISTS branches(
          branch_id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE, head_op INTEGER, forked_from INTEGER);
        CREATE TABLE IF NOT EXISTS snapshots(op_id INTEGER PRIMARY KEY, facts TEXT, branch_id INTEGER);
        CREATE TABLE IF NOT EXISTS open_questions(
          qid INTEGER PRIMARY KEY AUTOINCREMENT,
          branch TEXT, qtype TEXT, payload TEXT,
          status TEXT DEFAULT 'open', answer TEXT,
          created_ts INTEGER, resolved_ts INTEGER);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v INTEGER);
        """)
        # 既存DB移行: snapshots に branch_id 列が無ければ追加(旧snapshotは NULL→どのブランチにも採用されず full replay=安全)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(snapshots)").fetchall()]
        if "branch_id" not in cols:
            self.db.execute("ALTER TABLE snapshots ADD COLUMN branch_id INTEGER")
        self.db.commit()

    def _tick(self):
        row = self.db.execute("SELECT v FROM meta WHERE k='ts'").fetchone()
        v = (row[0] if row else 0) + 1
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('ts',?)", (v,))
        return v

    # ---------------- branches ----------------
    def _branch_id(self, name):
        r = self.db.execute("SELECT branch_id FROM branches WHERE name=?", (name,)).fetchone()
        return r[0] if r else None

    def _insert_branch(self, name, head, forked):
        cur = self.db.execute("INSERT INTO branches(name,head_op,forked_from) VALUES(?,?,?)", (name, head, forked))
        self.db.commit()
        return cur.lastrowid

    def _head(self, name):
        return self.db.execute("SELECT head_op FROM branches WHERE name=?", (name,)).fetchone()[0]

    def list_branches(self):
        rows = self.db.execute("SELECT name,head_op,forked_from FROM branches").fetchall()
        return [{"name": n, "head_op": h, "forked_from": f} for n, h, f in rows]

    def create_branch(self, name, from_branch="main", at_op=None):
        if self._branch_id(name) is not None:
            return {"error": f"branch '{name}' already exists"}
        head = at_op if at_op is not None else self._head(from_branch)
        self._insert_branch(name, head, head)
        return {"created": name, "forked_from_op": head}

    # ---------------- operations ----------------
    def _commit(self, branch, op_type, payload, valid_from, author="author"):
        ts = self._tick()
        parent = self._head(branch)
        bid = self._branch_id(branch)
        cur = self.db.execute(
            "INSERT INTO operations(parent_id,branch_id,op_type,payload,valid_from,author,ts) VALUES(?,?,?,?,?,?,?)",
            (parent, bid, op_type, json.dumps(payload, ensure_ascii=False), valid_from, author, ts),
        )
        op = cur.lastrowid
        self.db.execute("UPDATE branches SET head_op=? WHERE name=?", (op, branch))
        self.db.commit()
        if op % 25 == 0:
            self._snapshot(branch, op)
        return op

    def _ancestors(self, op_id, stop=None):
        ops = []
        cur = op_id
        while cur is not None and cur != stop:
            r = self.db.execute(
                "SELECT op_id,parent_id,op_type,payload,valid_from,author FROM operations WHERE op_id=?", (cur,)
            ).fetchone()
            if r is None:
                break
            ops.append(r)
            cur = r[1]
        return list(reversed(ops))

    # ---------------- replay / snapshot ----------------
    def _apply(self, kb, op_type, payload, vf):
        p = json.loads(payload)
        if op_type == "add_fact":
            kb.facts[p["fid"]] = Fact(
                p["fid"],
                p["subj"],
                p["attr"],
                p.get("value"),
                vf,
                p.get("kind", "STATE"),
                p.get("num"),
                p.get("deps", []),
                narrated_in=p.get("narrated_in"),
                valid_to=p.get("valid_to"),
            )
        elif op_type == "merge_alias":
            kb.aliases[p["from"]] = p["to"]
        elif op_type == "cannot_link":
            kb.cannot_link.add(frozenset((p["a"], p["b"])))
        elif op_type == "supersede":
            if p["fid"] in kb.facts:
                o = kb.facts[p["fid"]]
                kb.facts[p["fid"]] = Fact(
                    o.fid,
                    o.subj,
                    o.attr,
                    p["value"],
                    o.t,
                    o.kind,
                    p.get("num"),
                    narrated_in=o.narrated_in,
                    valid_to=o.valid_to,
                )
        elif op_type == "retag":
            if p["fid"] in kb.facts:
                o = kb.facts[p["fid"]]
                kb.facts[p["fid"]] = Fact(
                    o.fid,
                    o.subj,
                    p.get("attr", o.attr),
                    p.get("value", o.value),
                    p.get("t", o.t),
                    o.kind,
                    p.get("num", o.num),
                    narrated_in=p.get("narrated_in", o.narrated_in),
                    valid_to=p.get("valid_to", o.valid_to),
                )
        elif op_type == "delete_fact":
            kb.facts.pop(p["fid"], None)

    def _snapshot(self, branch, op_id):
        kb = self.materialize(branch, upto_op=op_id)
        facts = {
            fid: (f.subj, f.attr, f.value, f.t, f.kind, f.num, f.narrated_in, f.valid_to) for fid, f in kb.facts.items()
        }
        self.db.execute(
            "INSERT OR REPLACE INTO snapshots(op_id,facts,branch_id) VALUES(?,?,?)",
            (
                op_id,
                json.dumps(
                    {"facts": facts, "aliases": kb.aliases, "cl": [list(x) for x in kb.cannot_link]}, ensure_ascii=False
                ),
                self._branch_id(branch),
            ),
        )
        self.db.commit()

    def materialize(self, branch, as_of_valid=None, upto_op=None, as_of_narrated=None):
        """ブランチ状態を replay で再構成。
        as_of_valid: valid-time(物語内時間)スライス。「その章時点の世界」。
        as_of_narrated: discourse-time(語りの章)スライス。「第N章まで読んだ読者が知る事実」。
        両者は独立軸(bitemporal の布石)。未指定軸はフィルタしない。"""
        head = upto_op if upto_op is not None else self._head(branch)
        if head is None:
            return NarrativeKB()
        # スナップショットは同一ブランチのもののみ採用(op_idはグローバルなので他ブランチの混入を防ぐ)
        row = self.db.execute(
            "SELECT op_id,facts FROM snapshots WHERE op_id<=? AND branch_id=? ORDER BY op_id DESC",
            (head, self._branch_id(branch)),
        ).fetchone()
        stop = row[0] if row else None
        delta = self._ancestors(head, stop=stop)
        kb = NarrativeKB()
        if row:
            snap = json.loads(row[1])
            for fid, vals in snap["facts"].items():
                s, a, v, t, k, nm = vals[:6]  # 旧snapshot(6要素)互換
                ni = vals[6] if len(vals) > 6 else None  # narrated_in(7要素目)
                vt = vals[7] if len(vals) > 7 else None  # valid_to(8要素目)
                # discourse(narrated)はスナップショット時点で確定済みなのでここでスライス
                if as_of_narrated is not None:
                    nn = ni if ni is not None else t
                    if nn is not None and nn > as_of_narrated:
                        continue
                kb.facts[fid] = Fact(fid, s, a, v, t, k, nm, narrated_in=ni, valid_to=vt)
            kb.aliases = snap["aliases"]
            kb.cannot_link = {frozenset(x) for x in snap["cl"]}
        for oid, parent, op_type, payload, vf, author in delta:
            # discourse-time スライス: 第N章までに語られた op だけ適用(narrated は op生成時に確定)
            if as_of_narrated is not None:
                n = json.loads(payload).get("narrated_in")  # 旧op/構造opは欠落→valid_from(vf)で代替
                if n is None:
                    n = vf
                if n is not None and n > as_of_narrated:
                    continue
            self._apply(kb, op_type, payload, vf)
        # valid-time スライスは全op適用後に区間でポストフィルタ(retag/supersede等の補正もまず反映してから判定)
        if as_of_valid is not None:
            kb.facts = {fid: f for fid, f in kb.facts.items() if f.holds_at(as_of_valid)}
        kb.constraints = self.materialize_constraints(branch, upto_op)
        return kb

    # ---------------- 自動ID ----------------
    def _new_fid(self):
        return "fct_" + uuid.uuid4().hex[:8]

    def _new_cid(self):
        return "con_" + uuid.uuid4().hex[:8]

    # ---------------- 制約(操作ログでversioned) ----------------
    def materialize_constraints(self, branch, upto_op=None):
        """ブランチ系譜の制約操作を再生し、有効な制約レコード集合を返す。"""
        head = upto_op if upto_op is not None else self._head(branch)
        if head is None:
            return []
        cmap = {}
        for _oid, _parent, op_type, payload, _vf, _author in self._ancestors(head):
            if op_type == "add_constraint":
                p = json.loads(payload)
                cmap[p["cid"]] = p
            elif op_type == "set_constraint":
                p = json.loads(payload)
                if p["cid"] in cmap:
                    cmap[p["cid"]]["enabled"] = p["enabled"]
            elif op_type == "remove_constraint":
                p = json.loads(payload)
                cmap.pop(p["cid"], None)
        return list(cmap.values())

    def list_constraints(self, branch):
        return self.materialize_constraints(branch)

    def _eager_cc(self):
        row = self.db.execute("SELECT v FROM meta WHERE k='eager_cc'").fetchone()
        return bool(row[0]) if row else False

    def set_constraint_check_eager(self, on):
        """充足性チェックの実行モード。既定lazy(オンデマンド)。onにすると add_constraint 時に自動実行。"""
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('eager_cc',?)", (1 if on else 0,))
        self.db.commit()
        return {"eager_constraint_check": bool(on)}

    def check_constraints(self, branch):
        """制約セットの構造的な矛盾・無効設定を検出(遅延チェック)。"""
        issues = check_consistency(self.materialize_constraints(branch))
        return {"branch": branch, "consistent": len(issues) == 0, "issues": issues}

    def add_constraint(self, branch, template, params, scope=None, note="", enabled=True):
        if template not in TEMPLATES and template != "release":
            return {"error": f"unknown template '{template}'. choices: {sorted(TEMPLATES) + ['release']}"}
        cid = self._new_cid()
        rec = {
            "cid": cid,
            "template": template,
            "params": params,
            "scope": scope or {},
            "note": note,
            "enabled": enabled,
        }
        op = self._commit(branch, "add_constraint", rec, 0)
        out = {"status": "added", "cid": cid, "op_id": op, "constraint": rec}
        if self._eager_cc():  # 設定でeagerなら追加直後に充足性を検査(警告のみ; 作者の自由は妨げない)
            issues = check_consistency(self.materialize_constraints(branch))
            if issues:
                out["consistency_warnings"] = issues
        return out

    def set_constraint_enabled(self, branch, cid, enabled):
        cur = {c["cid"] for c in self.materialize_constraints(branch)}
        if cid not in cur:
            return {"error": f"constraint {cid} not found on '{branch}'"}
        op = self._commit(branch, "set_constraint", {"cid": cid, "enabled": enabled}, 0)
        return {"status": "enabled" if enabled else "disabled", "cid": cid, "op_id": op}

    def remove_constraint(self, branch, cid):
        cur = {c["cid"] for c in self.materialize_constraints(branch)}
        if cid not in cur:
            return {"error": f"constraint {cid} not found on '{branch}'"}
        op = self._commit(branch, "remove_constraint", {"cid": cid}, 0)
        return {"status": "removed", "cid": cid, "op_id": op, "note": "操作ログは不変(ロールバックで復活可)"}

    # ---------------- 構築: add (gate付き) ----------------
    def add(
        self,
        branch,
        subject,
        attribute,
        value,
        chapter,
        kind="STATE",
        num=None,
        gate=True,
        author="author",
        narrated_in=None,
        valid_to=None,
    ):
        kb = self.materialize(branch)
        # ORDER入力検証: 'A<B'(単一'<'・両辺非空)。不正値は _acyclic をクラッシュさせるので構築時に弾く。
        order_new = None
        if attribute == "ORDER" and gate:
            order_new = self._validate_order(kb, value)
            if order_new is False:
                return {
                    "status": "rejected",
                    "conflict": [
                        {"type": "MALFORMED_ORDER", "facts": [], "detail": f"ORDER値は 'A<B' 形式が必要: {value!r}"}
                    ],
                }
        fid = self._new_fid()
        f = Fact(fid, subject, attribute, value, chapter, kind, num, narrated_in=narrated_in, valid_to=valid_to)
        # hard制約検査(影響部分グラフ)。検査は valid-time(chapter)基準で行う。
        scope = kb._affected(f)
        viol = kb._check_hard(f, scope)
        if viol and gate:
            return {"status": "rejected", "conflict": [{"type": t, "facts": c, "detail": d} for (t, c, d) in viol]}
        op = self._commit(
            branch,
            "add_fact",
            {
                "fid": fid,
                "subj": subject,
                "attr": attribute,
                "value": value,
                "kind": kind,
                "num": num,
                "narrated_in": narrated_in,
                "valid_to": valid_to,
            },
            chapter,
            author,
        )
        # ソフト: 別名曖昧 → 質問を永続化
        q = self._alias_question(branch, kb, subject)
        out = {"status": "committed", "fid": fid, "op_id": op}
        if viol and not gate:
            out["soft_violation"] = [{"type": t, "facts": c, "detail": d} for (t, c, d) in viol]
        if q:
            out["question_id"] = q
        if order_new:  # 新規ORDERトークンを advisory で可視化(タイポによる時系列分断の検知)
            out["new_order_tokens"] = order_new
        return out

    def _validate_order(self, kb, value):
        """ORDER値を検証。不正(単一'<'でない/空辺)なら False。正なら[このブランチで未出のトークン]を返す。"""
        parts = (value or "").split("<")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return False
        a_tok, b_tok = parts[0].strip(), parts[1].strip()
        known = set()
        for g in kb.facts.values():
            if g.attr == "ORDER" and g.value and g.value.count("<") == 1:
                xa, xb = g.value.split("<")
                known.add(xa.strip())
                known.add(xb.strip())
        return [t for t in (a_tok, b_tok) if t not in known]

    def add_many(self, branch, facts, atomic=False, gate=True, author="author"):
        """複数factをまとめて追加。
        atomic=False(既定): 従来通り逐次適用(1件矛盾しても他はcommitされ得る=部分適用)。
        atomic=True: 1件でも矛盾した時点でバッチ全体を巻き戻し、何も適用しない
        (head復元 + バッチ中に生んだalias質問の取消)。矛盾を直して再投入する運用向け。
        facts は [{subject, attribute, value, chapter, kind?, num?}, ...]。"""
        head_before = self._head(branch)
        qid_before = self.db.execute("SELECT COALESCE(MAX(qid),0) FROM open_questions").fetchone()[0]
        results = []
        rejected = False
        for fc in facts:
            r = self.add(
                branch,
                fc["subject"],
                fc["attribute"],
                fc.get("value"),
                fc["chapter"],
                fc.get("kind", "STATE"),
                fc.get("num"),
                gate=gate,
                author=author,
                narrated_in=fc.get("narrated_in"),
                valid_to=fc.get("valid_to"),
            )
            results.append(r)
            if r.get("status") == "rejected":
                rejected = True
                if atomic:
                    break
        if atomic and rejected:
            self.rollback(branch, head_before)
            self.db.execute("DELETE FROM open_questions WHERE branch=? AND qid>?", (branch, qid_before))
            self.db.commit()
            return {
                "results": results,
                "atomic": True,
                "applied": False,
                "rolled_back_to_op": head_before,
                "note": "atomic: 矛盾が1件あったためバッチ全体を巻き戻した(何も適用していない)。矛盾を直して再投入せよ。",
            }
        return {"results": results, "atomic": atomic, "applied": True}

    def assert_alias(self, branch, a, b, author="author"):
        """作者が明示的に呼称 a を b の別名(同一指示対象)として統合する(b が正準)。
        表層が似ていなくてよい(例: 偽名「ミスター・グレイ」=「マイケル・コール」)。
        ALIAS質問への answer_question('同一') と等価だが、質問を待たず能動宣言できる。
        統合で正体レベルの矛盾(故人の行為など)が表面化し得るので merge後のhard監査を併せて返す。"""
        op = self._commit(branch, "merge_alias", {"from": a, "to": b}, 0, author)
        hv = self.audit(branch)["hard_violations"]
        return {"status": "aliased", "alias": a, "canonical": b, "op_id": op, "hard_violations": hv}

    def assert_distinct(self, branch, a, b, author="author"):
        """作者が呼称 a と b を別人(別指示対象)として固定する(cannot_link)。同姓の別人など。
        以後この対で ALIAS 質問は出ず、自動別名統合もされない。answer_question('別物') と等価。"""
        op = self._commit(branch, "cannot_link", {"a": a, "b": b}, 0, author)
        return {"status": "distinct", "a": a, "b": b, "op_id": op}

    def _alias_question(self, branch, kb, subject):
        cs = kb._canon(subject)
        for g in kb.facts.values():
            gs = kb._canon(g.subj)
            if gs != cs and self._similar(cs, gs) and frozenset((cs, gs)) not in kb.cannot_link:
                dup = self._open_alias_qid(branch, cs, gs)  # 同一ペアの未解決質問があれば再利用(重複生成しない)
                if dup is not None:
                    return dup
                return self.create_question(
                    branch, "ALIAS", {"a": cs, "b": gs, "q": f"『{cs}』と既存の『{gs}』は同一指示対象か?"}
                )
        return None

    def _open_alias_qid(self, branch, a, b):
        """ブランチ上の open な ALIAS 質問で {a,b} ペアが既出ならその qid を返す(順不同照合)。"""
        rows = self.db.execute(
            "SELECT qid,payload FROM open_questions WHERE branch=? AND qtype='ALIAS' AND status='open'", (branch,)
        ).fetchall()
        for qid, payload in rows:
            p = json.loads(payload)
            if {p.get("a"), p.get("b")} == {a, b}:
                return qid
        return None

    def _similar(self, a, b):
        A, B = set(a), set(b)
        return (len(A & B) / len(A | B) if (A | B) else 0) >= 0.3

    def update(self, branch, fid, new_value, num=None, author="author"):
        kb = self.materialize(branch)
        if fid not in kb.facts:
            return {"error": f"fact {fid} not found"}
        old = kb.facts[fid]
        nf = Fact(fid, old.subj, old.attr, new_value, old.t, old.kind, num)
        scope = [g for g in kb._affected(nf) if g.fid != fid]
        viol = kb._check_hard(nf, scope)
        if viol:
            return {
                "status": "rejected(retcon)",
                "conflict": [{"type": t, "facts": c, "detail": d} for (t, c, d) in viol],
            }
        op = self._commit(branch, "supersede", {"fid": fid, "value": new_value, "num": num}, old.t, author)
        return {"status": "superseded", "fid": fid, "op_id": op}

    def retag(
        self,
        branch,
        fid,
        chapter=None,
        attribute=None,
        valid_to=None,
        narrated_in=None,
        value=None,
        num=None,
        author="author",
    ):
        """既存factの 章(chapter)/属性/区間終了(valid_to)/語り章(narrated_in)/値/num を、
        同じ fid のまま付け替える(delete+re-add 不要)。None の項目は据え置き。
        retcon 同様に hard 再検査が走り、矛盾すれば拒否(適用しない)。
        注: valid_to/narrated_in を ∞/既定(None)へ戻すのはこの操作では不可(rare; delete+addで)。"""
        kb = self.materialize(branch)
        if fid not in kb.facts:
            return {"error": f"fact {fid} not found"}
        o = kb.facts[fid]
        nf = Fact(
            fid,
            o.subj,
            attribute or o.attr,
            value if value is not None else o.value,
            chapter if chapter is not None else o.t,
            o.kind,
            num if num is not None else o.num,
            narrated_in=narrated_in if narrated_in is not None else o.narrated_in,
            valid_to=valid_to if valid_to is not None else o.valid_to,
        )
        scope = [g for g in kb._affected(nf) if g.fid != fid]
        viol = kb._check_hard(nf, scope)
        if viol:
            return {
                "status": "rejected(retag)",
                "conflict": [{"type": t, "facts": c, "detail": d} for (t, c, d) in viol],
            }
        payload = {"fid": fid}
        for key, val in (
            ("attr", attribute),
            ("value", value),
            ("t", chapter),
            ("num", num),
            ("narrated_in", narrated_in),
            ("valid_to", valid_to),
        ):
            if val is not None:
                payload[key] = val
        op = self._commit(branch, "retag", payload, nf.t, author)
        return {"status": "retagged", "fid": fid, "op_id": op}

    def delete(self, branch, fid):
        kb = self.materialize(branch)
        orphans = [g.fid for g in kb.facts.values() if fid in g.deps]
        if orphans:
            return {"status": "rejected(orphan)", "dependent_facts": orphans}
        op = self._commit(branch, "delete_fact", {"fid": fid}, 0)
        return {"status": "deleted", "fid": fid, "op_id": op}

    # ---------------- 取込: 既存作品(gateせず, 後で監査) ----------------
    def import_facts(self, branch, facts, author="import"):
        committed = []
        for fc in facts:
            fid = self._new_fid()
            self._commit(
                branch,
                "add_fact",
                {
                    "fid": fid,
                    "subj": fc["subject"],
                    "attr": fc["attribute"],
                    "value": fc.get("value"),
                    "kind": fc.get("kind", "STATE"),
                    "num": fc.get("num"),
                    "narrated_in": fc.get("narrated_in"),
                    "valid_to": fc.get("valid_to"),
                },
                fc["chapter"],
                author,
            )
            committed.append(fid)
        return {"imported": len(committed), "fids": committed}

    # ---------------- 検証 ----------------
    STATE_ATTRS = {"LIFE", "LOC", "RANK", "STATE", "ALLIANCE"}

    def _temporal_gate(self, a, b):
        """① soft監査をNLIに送る前の時間構造ゲート。共存し得ない対を除外。"""
        sa = a.attr in self.STATE_ATTRS
        sb = b.attr in self.STATE_ATTRS
        if not sa and not sb:  # 行為×行為
            return (a.t == b.t), "同時点の行為" if a.t == b.t else "別時点の行為(非矛盾)"
        if sa and sb and a.attr == b.attr:  # 同属性の状態
            return (a.t == b.t), "同章状態" if a.t == b.t else "supersession(遷移)"
        if sa and sb and a.attr != b.attr:  # 異属性の持続状態=跨ぎ候補
            return True, "異属性状態の共存(跨ぎ矛盾候補)"
        return False, "状態×行為(hard担当)"  # 状態×行為

    def audit(self, branch, as_of_valid=None, scorer=None):
        kb = self.materialize(branch, as_of_valid)
        hard = []
        seen = NarrativeKB()
        seen.aliases = kb.aliases
        seen.cannot_link = kb.cannot_link
        seen.constraints = kb.constraints
        for f in sorted(kb.facts.values(), key=lambda x: (x.t, x.fid)):
            scope = seen._affected(f)
            v = seen._check_hard(f, scope)
            for t, c, d in v:
                hard.append({"type": t, "facts": c, "detail": d})
            seen.facts[f.fid] = f
        soft_q = []
        nli_calls = 0
        if scorer is not None:
            from collections import defaultdict

            bysubj = defaultdict(list)
            for f in kb.facts.values():
                if f.value:
                    bysubj[kb._canon(f.subj)].append(f)
            for subj, fs in bysubj.items():
                fs = sorted(fs, key=lambda x: x.t)
                for i in range(len(fs)):
                    for j in range(i + 1, len(fs)):
                        a, b = fs[i], fs[j]
                        send, _r = self._temporal_gate(a, b)  # ① NLI前の時間ゲート
                        if not send:
                            continue
                        nli_calls += 1
                        if scorer(f"{subj}は{a.value}。", f"{subj}は{b.value}。") == "contradiction":
                            qid = self.create_question(
                                branch,
                                "SOFT_CONTRADICTION",
                                {
                                    "subj": subj,
                                    "a": [a.fid, a.t, a.value],
                                    "b": [b.fid, b.t, b.value],
                                    "q": f"「{subj}」: ch{a.t}「{a.value}」とch{b.t}「{b.value}」は両立するか?",
                                },
                            )
                            soft_q.append(qid)
        return {
            "branch": branch,
            "hard_violations": hard,
            "nli_calls": nli_calls,
            "soft_questions_created": soft_q,
            "consistent": len(hard) == 0,
        }

    # ---------------- マージ ----------------
    def _common_ancestor(self, b1, b2):
        a1 = {o[0] for o in self._ancestors(self._head(b1))}
        for o in reversed(self._ancestors(self._head(b2))):
            if o[0] in a1:
                return o[0]
        return None

    def _state_map(self, branch=None, op=None):
        kb = self.materialize(branch, upto_op=op) if op is None else self._materialize_at(op)
        return {(f.subj, f.attr): (f.fid, f.value) for f in kb.facts.values()}

    def _materialize_at(self, op):
        kb = NarrativeKB()
        for oid, parent, op_type, payload, vf, author in self._ancestors(op):
            self._apply(kb, op_type, payload, vf)
        return kb

    def merge(self, src, dst):
        base_op = self._common_ancestor(src, dst)
        base = (
            {(f.subj, f.attr): (f.fid, f.value) for f in self._materialize_at(base_op).facts.values()}
            if base_op
            else {}
        )
        S = {(f.subj, f.attr): (f.fid, f.value) for f in self.materialize(src).facts.values()}
        D = {(f.subj, f.attr): (f.fid, f.value) for f in self.materialize(dst).facts.values()}
        auto = []
        conflicts = []
        for k in set(S) | set(D):
            sv = S.get(k)
            dv = D.get(k)
            bv = base.get(k)
            if sv == dv:
                continue
            s_ch = sv != bv
            d_ch = dv != bv
            if s_ch and not d_ch:
                auto.append({"key": list(k), "take": "src", "value": sv[1]})
            elif d_ch and not s_ch:
                auto.append({"key": list(k), "take": "dst", "value": dv[1]})
            elif s_ch and d_ch:
                qid = self.create_question(
                    dst,
                    "MERGE_CONFLICT",
                    {
                        "subj": k[0],
                        "attr": k[1],
                        "base": bv[1] if bv else None,
                        "src": sv[1] if sv else None,
                        "dst": dv[1] if dv else None,
                        "src_fid": sv[0] if sv else None,
                        "dst_fid": dv[0] if dv else None,
                        "q": f"「{k[0]}」の{k[1]}: src「{sv[1] if sv else '-'}」/ dst「{dv[1] if dv else '-'}」どちらを正史にするか?",
                    },
                )
                conflicts.append({"key": list(k), "question_id": qid})
        return {"base_op": base_op, "auto_merged": auto, "conflicts": conflicts}

    # ---------------- ロールバック ----------------
    def rollback(self, branch, to_op):
        self.db.execute("UPDATE branches SET head_op=? WHERE name=?", (to_op, branch))
        self.db.commit()
        return {"branch": branch, "head_op": to_op, "note": "操作ログは不変(巻き戻しの巻き戻し可能)"}

    # ---------------- 質問(作者oracleチャネル) ----------------
    def create_question(self, branch, qtype, payload):
        ts = self._tick()
        cur = self.db.execute(
            "INSERT INTO open_questions(branch,qtype,payload,created_ts) VALUES(?,?,?,?)",
            (branch, qtype, json.dumps(payload, ensure_ascii=False), ts),
        )
        self.db.commit()
        return cur.lastrowid

    def list_questions(self, branch=None, status="open"):
        q = "SELECT qid,branch,qtype,payload,status FROM open_questions WHERE status=?"
        a = [status]
        if branch:
            q += " AND branch=?"
            a.append(branch)
        return [
            {"qid": r[0], "branch": r[1], "type": r[2], **json.loads(r[3]), "status": r[4]}
            for r in self.db.execute(q, a).fetchall()
        ]

    def answer_question(self, qid, answer):
        r = self.db.execute("SELECT branch,qtype,payload,status FROM open_questions WHERE qid=?", (qid,)).fetchone()
        if not r:
            return {"error": f"question {qid} not found"}
        branch, qtype, payload, status = r
        p = json.loads(payload)
        if status != "open":
            return {"error": f"question {qid} already {status}"}
        applied = None
        if qtype == "ALIAS":
            if answer in ("同一", "same", "yes"):
                self._commit(branch, "merge_alias", {"from": p["a"], "to": p["b"]}, 0, "author")
                applied = f"alias {p['a']}={p['b']}"
            else:
                self._commit(branch, "cannot_link", {"a": p["a"], "b": p["b"]}, 0, "author")
                applied = f"cannot_link {p['a']}|{p['b']}"
        elif qtype == "MERGE_CONFLICT":
            if answer in ("src", "B案"):
                keep = p["src"]
            elif answer in ("dst", "A案"):
                keep = p["dst"]
            else:
                keep = answer  # 値そのものを指定(別解)も可
            # 統合先(dst)の事実を常に書き換える。dstに無ければ新規追加。
            if p.get("dst_fid"):
                self._commit(branch, "supersede", {"fid": p["dst_fid"], "value": keep}, 0, "author")
            else:
                nf = self._new_fid()
                self._commit(
                    branch,
                    "add_fact",
                    {"fid": nf, "subj": p["subj"], "attr": p["attr"], "value": keep, "kind": "STATE"},
                    0,
                    "author",
                )
            applied = f"{p['subj']}.{p['attr']}={keep}"
        elif qtype == "SOFT_CONTRADICTION":
            applied = "acknowledged (作者判断; 自動操作なし)"
        ts = self._tick()
        self.db.execute(
            "UPDATE open_questions SET status='resolved',answer=?,resolved_ts=? WHERE qid=?",
            (json.dumps(answer, ensure_ascii=False), ts, qid),
        )
        self.db.commit()
        return {"qid": qid, "resolved": answer, "applied": applied}

    # ---------------- 読取 ----------------
    def get_state(self, branch="main", as_of_chapter=None, subject=None, as_of_narrated=None):
        kb = self.materialize(branch, as_of_chapter, as_of_narrated=as_of_narrated)
        fs = sorted(kb.facts.values(), key=lambda x: (x.t, x.fid))
        if subject:
            cs = kb._canon(subject)
            fs = [f for f in fs if kb._canon(f.subj) == cs]
        return {
            "branch": branch,
            "as_of_chapter": as_of_chapter,
            "as_of_narrated": as_of_narrated,
            "aliases": kb.aliases,
            "facts": [
                {
                    "fid": f.fid,
                    "subject": f.subj,
                    "attribute": f.attr,
                    "value": f.value,
                    "chapter": f.t,  # valid-time(物語内時間)の開始章
                    "valid_to": f.valid_to,  # valid-time の終了章(排他); None なら +∞(開区間)
                    "narrated_in": f.narrated,  # discourse-time(語りの章); 未指定なら chapter と同値
                    "kind": f.kind,
                    "num": f.num,
                }
                for f in fs
            ],
        }

    # ---------------- アウトライン(章ビート) ----------------
    def set_beat(self, branch, chapter, beat, author="author"):
        """章ビート(その章の設計=1段落)を登録/更新。attribute=BEAT, subject=chNN の fact。
        執筆前に置き、本文は『ビートを現在カノンに矛盾せず展開する』タスクに変える(アウトライン先行)。冪等。"""
        subj = f"ch{chapter}"
        kb = self.materialize(branch)
        existing = [f for f in kb.facts.values() if f.attr == "BEAT" and f.subj == subj]
        if existing:
            return self.retag(branch, existing[0].fid, value=beat)
        return self.add(branch, subj, "BEAT", beat, chapter, kind="STATE", gate=False, author=author)

    def get_outline(self, branch, from_chapter=None, to_chapter=None):
        """章ビート(プロット骨格)を章順で返す。範囲指定可。"""
        kb = self.materialize(branch)
        beats = [{"chapter": f.t, "beat": f.value, "fid": f.fid} for f in kb.facts.values() if f.attr == "BEAT"]
        if from_chapter is not None:
            beats = [b for b in beats if b["chapter"] >= from_chapter]
        if to_chapter is not None:
            beats = [b for b in beats if b["chapter"] <= to_chapter]
        return sorted(beats, key=lambda b: b["chapter"])

    # ---------------- 伏線(SETUP)と章ブリーフ ----------------
    def add_setup(self, branch, setup, chapter, payoff_by=None, subject="伏線", author="author"):
        """未回収にしたくない伏線(チェーホフの銃)を登録。attribute=SETUP の fact として持つ。
        payoff_by に回収期限の章を入れると open_setups で超過を overdue 表示。回収は resolve_setup(=delete)。"""
        return self.add(
            branch, subject, "SETUP", setup, chapter, kind="STATE", num=payoff_by, gate=False, author=author
        )

    def resolve_setup(self, branch, fid):
        """伏線を回収済みにする(delete=現在状態から外す。操作ログは不変なので履歴は残る)。"""
        return self.delete(branch, fid)

    def open_setups(self, branch, as_of_chapter=None):
        """未回収の伏線(現在状態に残る SETUP fact)。payoff_by(num) を過ぎていれば overdue。"""
        kb = self.materialize(branch)
        out = []
        for f in kb.facts.values():
            if f.attr != "SETUP":
                continue
            overdue = as_of_chapter is not None and f.num is not None and f.num < as_of_chapter
            out.append(
                {
                    "fid": f.fid,
                    "thread": f.subj,
                    "setup": f.value,
                    "narrated_in": f.narrated,
                    "payoff_by": f.num,
                    "overdue": overdue,
                }
            )
        return sorted(out, key=lambda x: (x["payoff_by"] is None, x["payoff_by"] or 0))

    def chapter_brief(self, branch, chapter):
        """第N章を書く前に要る正準を1発で束ねる(想起負担の軽減)。as_of_chapter=N の世界で:
        characters(各主体の生死/地位/位置/呼称) / constraints(有効制約) / open_questions /
        open_setups(未回収伏線・overdue付) / recent(直近[N-2,N]の出来事)。"""
        kb = self.materialize(branch, as_of_valid=chapter)
        from collections import defaultdict

        attrs_of = defaultdict(dict)
        dead = set()
        recent = []
        for f in sorted(kb.facts.values(), key=lambda x: (x.t, x.fid)):
            if f.attr == "SETUP":
                continue
            cs = kb._canon(f.subj)
            if f.attr == "LIFE" and f.value == "dead":
                dead.add(cs)
            if f.attr in ("LIFE", "RANK", "LOC", "STATE", "呼称"):
                attrs_of[cs][f.attr] = f.value
            if f.t >= chapter - 2 and f.attr in ("ACT", "ORDER", "LIFE"):
                recent.append({"subject": f.subj, "attribute": f.attr, "value": f.value, "chapter": f.t})
        # 人物(LIFE/RANK を持つ=動く実体) と 世界・設定(STATEのみ) を分ける
        characters, world = [], []
        for s, a in sorted(attrs_of.items()):
            if "LIFE" in a or "RANK" in a:
                characters.append({"subject": s, "alive": s not in dead, **a})
            else:
                world.append({"subject": s, **a})
        constraints = [
            {"cid": c["cid"], "template": c["template"], "note": c.get("note", "")}
            for c in self.materialize_constraints(branch)
        ]
        beat = next((f.value for f in kb.facts.values() if f.attr == "BEAT" and f.subj == f"ch{chapter}"), None)
        return {
            "branch": branch,
            "chapter": chapter,
            "beat": beat,  # この章の設計(アウトライン先行); set_beat 済みなら本文展開の指針
            "characters": characters,
            "world": world,
            "constraints": constraints,
            "open_questions": self.list_questions(branch),
            "open_setups": self.open_setups(branch, as_of_chapter=chapter),
            "recent": recent,
        }

    def get_log(self, branch, limit=50):
        bid = self._branch_id(branch)
        rows = self.db.execute(
            "SELECT op_id,op_type,payload,valid_from,author FROM operations WHERE branch_id=? ORDER BY op_id DESC LIMIT ?",
            (bid, limit),
        ).fetchall()
        return [
            {"op_id": o, "type": t, "payload": json.loads(p), "chapter": vf, "author": au} for o, t, p, vf, au in rows
        ]
