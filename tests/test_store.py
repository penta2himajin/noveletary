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


# ---- Phase B: valid-time を区間 [valid_from, valid_to) に ----
def test_valid_to_defaults_open_ended(s):
    s.add("main", "ハル", "STATE", "在室", 3)  # valid_to 未指定 = ∞
    assert s.get_state("main")["facts"][0]["valid_to"] is None


def test_bounded_fluent_invisible_after_valid_to(s):
    s.add("main", "セバスチャン", "LOC", "工房", 0, valid_to=1)  # [0,1)
    assert len(s.get_state("main", as_of_chapter=0)["facts"]) == 1  # 第0章は在る
    assert s.get_state("main", as_of_chapter=5)["facts"] == []  # 第1章以降は無い


def test_background_fluent_bounded_at_death_ok(s):
    # 経歴/居所を死で畳めば(clockmakerケース)、終端と衝突しない
    s.add("main", "セバスチャン", "LIFE", "dead", 1)
    r = s.add("main", "セバスチャン", "LOC", "工房", 0, valid_to=1)  # 生前の居所、死で終了
    assert r["status"] == "committed"


def test_reinitiation_after_death_still_forbidden(s):
    # 死後の新規initiation(valid_from>=死)は valid_to に関係なく弾く(回帰)
    s.add("main", "セバスチャン", "LIFE", "dead", 1)
    r = s.add("main", "セバスチャン", "LOC", "酒場", 5, valid_to=9)  # [5,9) 死後に動く
    assert r["status"] == "rejected"


def test_valid_to_survives_snapshot(s):
    for i in range(26):  # スナップショット(op25)跨ぎ
        s.add("main", f"E{i}", "STATE", "x", 0, valid_to=2)  # 全部 [0,2)
    assert s.get_state("main", as_of_chapter=5)["facts"] == []  # 第5章には全て終了済み


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


def test_snapshot_does_not_leak_across_branches(s):
    # スナップショットはグローバルop_idで保存されるが、ブランチ境界を越えて混入してはならない
    s.create_branch("A", "main")
    for i in range(30):  # A をスナップショット閾値(25op)超で進める
        s.add("A", f"A{i}", "STATE", "x", 1)
    s.create_branch("B", "main")  # 初期点から分岐(A の fact は B の祖先ではない)
    s.add("B", "唯一", "STATE", "y", 1)
    subs = {f["subject"] for f in s.get_state("B")["facts"]}
    assert subs == {"唯一"}  # A の 30 fact が混入しないこと


def test_alias_ignores_outline_metadata(s):
    # BEAT/SETUP(chN/伏線)はアウトラインmetadataでALIAS検出に参加しない(ストレステストの誤発火源)
    s.set_beat("main", 1, "章1の設計")
    s.set_beat("main", 11, "章11の設計")  # ch1 と文字近いが BEAT 同士でALIASしない
    r = s.add("main", "ch1侍", "RANK", "武士", 2)  # 実体だが BEAT 主語 ch1/ch11 とは照合しない
    assert "question_id" not in r
    r2 = s.add("main", "セバスチャンの伏線メモ", "SETUP", "x", 1)  # SETUP追加側もALIASを出さない
    assert "question_id" not in r2
    assert [q for q in s.list_questions("main") if q["type"] == "ALIAS"] == []


def test_alias_latin_names_no_false_fire(s):
    # ラテン名の別人が偶然の字母一致でALIAS誤発火しない(fantasy枝の93件の主因)
    s.add("main", "King Aldric", "RANK", "king", 1)
    r = s.add("main", "General Kessik", "RANK", "general", 2)
    assert "question_id" not in r


def test_alias_question_deduped(s):
    # 同一ペアの open ALIAS 質問は重複生成しない(同じ主体の fact を複数足しても1つ)
    s.add("main", "セバスチャン・コール", "RANK", "時計師", 0)
    qids = set()
    for _ in range(3):
        r = s.add("main", "マイケル・コール", "STATE", "甥", 3)
        if "question_id" in r:
            qids.add(r["question_id"])
    aliasqs = [q for q in s.list_questions("main") if q["type"] == "ALIAS"]
    assert len(aliasqs) == 1  # 同一ペアは1つ
    assert len(qids) == 1  # 各 add は同じ qid を指す


def test_retag_moves_chapter_keeping_fid(s):
    # 章の付け替えを delete+re-add なしで(同じ fid のまま)
    fid = s.add("main", "セバスチャン", "RANK", "時計師", 1)["fid"]
    r = s.retag("main", fid, chapter=0)
    assert r["status"] == "retagged"
    f = s.get_state("main")["facts"][0]
    assert f["fid"] == fid and f["chapter"] == 0


def test_retag_rechecks_constraints(s):
    # retag は retcon 同様に再検査が走る: 死後の章へ動かすと弾く
    s.add("main", "X", "LIFE", "dead", 1)
    fid = s.add("main", "X", "LOC", "工房", 0, valid_to=1)["fid"]  # [0,1) は ok
    bad = s.retag("main", fid, chapter=5)  # 死後へ移動 = 矛盾
    assert bad["status"].startswith("rejected")
    loc = [f for f in s.get_state("main", subject="X")["facts"] if f["fid"] == fid][0]
    assert loc["chapter"] == 0  # 元のまま(適用されない)


def test_retag_can_bound_valid_to(s):
    # 既存の開区間 fluent を死で畳む(valid_to を後付け)
    s.add("main", "X", "LIFE", "dead", 1)
    fid = s.add("main", "X", "LOC", "工房", 0)["fid"]  # [0,∞)
    assert s.retag("main", fid, valid_to=1)["status"] == "retagged"
    locs = [f for f in s.get_state("main", as_of_chapter=5, subject="X")["facts"] if f["attribute"] == "LOC"]
    assert locs == []  # 第5章には LOC は無い(死で畳まれた)


def test_order_malformed_rejected(s):
    # ORDER は 'A<B' 形式。'<' が無い/空辺は reject(_acyclic のクラッシュ防止)
    r = s.add("main", "事件", "ORDER", "AB", 1)
    assert r["status"] == "rejected" and r["conflict"][0]["type"] == "MALFORMED_ORDER"
    assert s.add("main", "事件", "ORDER", "A<B<C", 1)["status"] == "rejected"  # '<'が複数も不正


def test_order_new_token_surfaced(s):
    # 新規イベントトークンを advisory で可視化(タイポによる時系列分断の検知)
    s.add("main", "事件", "ORDER", "来訪<死亡", 1)
    r = s.add("main", "事件", "ORDER", "死亡<発見", 1)  # 死亡=既知, 発見=新規
    assert set(r.get("new_order_tokens", [])) == {"発見"}


def test_order_typo_surfaces_as_new_token(s):
    s.add("main", "事件", "ORDER", "死亡<発見", 1)
    r = s.add("main", "事件", "ORDER", "死亡<発覚", 1)  # 発覚=発見のタイポ → 新規として出る
    assert "発覚" in r.get("new_order_tokens", [])


def test_audit_robust_to_malformed_order_from_import(s):
    # import は gate しないので不正ORDERが入り得るが、audit(_acyclic)はクラッシュしない
    s.import_facts("main", [{"subject": "事件", "attribute": "ORDER", "value": "こわれた値", "chapter": 1}])
    r = s.audit("main")
    assert "consistent" in r


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


def test_set_beat_get_outline_and_brief(s):
    s.set_beat("main", 1, "イオ、解雇され海溝へ")
    s.set_beat("main", 2, "黒い廃艦と遭遇")
    s.set_beat("main", 1, "イオ、最後の任務で海溝へ向かう")  # 同章は更新(冪等)
    outline = s.get_outline("main")
    assert [b["chapter"] for b in outline] == [1, 2]  # 重複せず章順
    assert outline[0]["beat"] == "イオ、最後の任務で海溝へ向かう"  # 更新が反映
    assert s.chapter_brief("main", 2)["beat"] == "黒い廃艦と遭遇"  # briefに当該章ビート同梱


def test_open_setups_overdue_and_resolve(s):
    fid = s.add_setup("main", "時計のゼンマイに伏線", chapter=2, payoff_by=5)["fid"]
    s.add_setup("main", "灰色の紳士の正体", chapter=2)  # 期限なし
    opn = s.open_setups("main", as_of_chapter=7)
    assert {o["setup"] for o in opn} == {"時計のゼンマイに伏線", "灰色の紳士の正体"}
    assert next(o for o in opn if o["payoff_by"] == 5)["overdue"] is True  # 第7章時点で期限超過
    s.resolve_setup("main", fid)  # 回収
    assert {o["setup"] for o in s.open_setups("main")} == {"灰色の紳士の正体"}


def test_chapter_brief_assembles_context(s):
    s.add("main", "ハル", "RANK", "潜水士", 1)
    s.add("main", "ハル", "LOC", "港", 1)
    s.add("main", "モロー", "RANK", "社長", 1)
    s.add("main", "モロー", "LIFE", "dead", 3)  # 第3章で死亡
    s.add("main", "ハル", "ACT", "出航", 4, kind="EVENT")
    s.add("main", "テネブラエ港", "STATE", "辺境の宙港", 1)  # 世界・設定(STATEのみ)
    s.add_setup("main", "未回収の手紙", chapter=2, payoff_by=4)
    b = s.chapter_brief("main", 5)
    chars = {c["subject"]: c for c in b["characters"]}
    assert chars["ハル"]["alive"] is True and chars["ハル"]["RANK"] == "潜水士"
    assert chars["モロー"]["alive"] is False  # 第5章時点では故人
    world = {w["subject"] for w in b["world"]}
    assert "テネブラエ港" in world and "ハル" not in world  # 人物と世界が分離
    assert any(c["template"] == "forbid_after_state" for c in b["constraints"])
    assert any(o["setup"] == "未回収の手紙" and o["overdue"] for o in b["open_setups"])  # 期限4<5
    assert any(r["value"] == "出航" for r in b["recent"])  # 直近[3,5]の行為


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
