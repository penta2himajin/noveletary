"""
server.py — 物語整合性検証 MCP サーバー (FastMCP / stdio)

LLMドライバ向けツール設計。想定ワークフロー:

 [A] 0から執筆する
   1. create_branch で作業ブランチ(任意。mainでも可)
   2. 章を書く前に get_state(branch, as_of_chapter=N) で「その時点までに確定した事実」を取得
   3. 書いた後 add_fact / add_facts で新事実を登録(hard制約で矛盾を即gate)
   4. list_open_questions を確認 → 別名等の未解決を answer_question で作者が解消

 [B] 既存作品を取り込む
   1. 章から抽出した事実を import_facts で一括登録(gateせず丸ごと読込)
   2. audit(branch, include_soft=True) で既存の矛盾を表面化(hard=確定 / soft=要確認の質問)
   3. list_open_questions → answer_question で別名統合・矛盾判断を作者が解消

 [C] 並行プロット(if文/別エンディング)を検討する
   1. create_branch で A案/B案 に分岐(状態コピー無し)
   2. 各ブランチで add して audit → ブランチごとに整合性が独立に出る
   3. merge_branches で統合 → 競合は作者質問として出る → answer_question で正史決定

設計原則(LLM視点):
 - 事実IDは自動採番。呼ぶ側はsubject/attribute/value/chapterだけ渡せばよい。
 - 書込結果は committed / rejected(矛盾fact集合つき) / question を構造化で返す。
 - 未解決はLLMが推測せず list_open_questions → 作者(oracle)へ。
"""

import os

from mcp.server.fastmcp import FastMCP

from .store import Store

DB = os.environ.get("NARRATIVE_DB", "data/narrative.db")  # リポジトリ直下 data/ に永続化(repo rootから起動)
store = Store(DB)
mcp = FastMCP("narrative-consistency")

# --- NLI scorer (任意; 意味的矛盾検出に使用。未導入なら soft監査はスキップ) ---
_scorer = None


def _get_scorer():
    global _scorer
    if _scorer is None:
        try:
            from transformers import pipeline

            clf = pipeline("text-classification", "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli", device=-1, top_k=None)

            def score(a, b):
                r = clf(f"{a} [SEP] {b}")
                rr = r[0] if isinstance(r[0], list) else r
                d = {x["label"].lower(): x["score"] for x in rr}
                return max(d, key=d.get)

            _scorer = score
        except Exception:
            _scorer = False
    return _scorer or None


# ===================== ブランチ =====================
@mcp.tool()
def list_branches() -> dict:
    """全ブランチ(物語の版/プロット案)を列挙する。"""
    return {"branches": store.list_branches()}


@mcp.tool()
def create_branch(name: str, from_branch: str = "main", at_op: int = None) -> dict:
    """新しいブランチを作る。並行プロット(A案/B案)やif展開の検討に使う。状態コピーは起きない(ポインタのみ)。
    from_branch の現在地(または at_op で指定した操作)から分岐する。"""
    return store.create_branch(name, from_branch, at_op)


@mcp.tool()
def rollback_branch(branch: str, to_op: int) -> dict:
    """ブランチを過去の操作IDまで巻き戻す。操作ログは不変なので巻き戻しの巻き戻しも可能。
    LLMの一連の編集で矛盾が入った時の安全網。"""
    return store.rollback(branch, to_op)


# ===================== 読取(書く前に呼ぶ) =====================
@mcp.tool()
def get_state(branch: str = "main", as_of_chapter: int = None, subject: str = None) -> dict:
    """ブランチで現在有効な事実を返す。
    as_of_chapter を指定すると「その章時点の世界」に時間スライス(retcon後でも正しい)。
    subject を指定すると特定エンティティだけに絞る(文脈節約)。章を書く前の状態確認に使う。"""
    return store.get_state(branch, as_of_chapter, subject)


@mcp.tool()
def get_log(branch: str = "main", limit: int = 50) -> dict:
    """ブランチの操作履歴(新しい順)。op_id はロールバック先の指定に使える。"""
    return {"branch": branch, "log": store.get_log(branch, limit)}


# ===================== 構築(執筆) =====================
@mcp.tool()
def add_fact(
    branch: str, subject: str, attribute: str, value: str, chapter: int, kind: str = "STATE", num: int = None
) -> dict:
    """事実を1件登録(hard制約でgate)。0から執筆する時の基本操作。
    attribute例: LIFE(生死: value=alive/dead) / ACT(行為) / LOC(位置) / RANK(地位) / LEDGER(台帳: numに数値, kind=COUNTER) / ORDER(時間順序: value='A<B') / STATE(一般)。
    矛盾(死後の行為・台帳の減少・時間循環等)があれば status=rejected と矛盾fact集合を返す。
    別名の疑い等が生じると question_id を返す(list_open_questions で確認)。"""
    return store.add(branch, subject, attribute, value, chapter, kind, num, gate=True)


@mcp.tool()
def add_facts(branch: str, facts: list) -> dict:
    """複数の事実をまとめて登録(各々hard制約でgate)。1シーン分の事実を一括投入する時に。
    facts は [{subject, attribute, value, chapter, kind?, num?}, ...]。
    返り値は各factの結果(committed/rejected)のリスト。"""
    results = []
    for fc in facts:
        results.append(
            store.add(
                branch,
                fc["subject"],
                fc["attribute"],
                fc.get("value"),
                fc["chapter"],
                fc.get("kind", "STATE"),
                fc.get("num"),
                gate=True,
            )
        )
    return {"results": results}


@mcp.tool()
def update_fact(branch: str, fid: str, new_value: str, num: int = None) -> dict:
    """既存事実を更新(supersession; 旧版は履歴に残る)。retcon検査が走り、過去版と矛盾すれば拒否。"""
    return store.update(branch, fid, new_value, num)


@mcp.tool()
def delete_fact(branch: str, fid: str) -> dict:
    """事実を削除。他factが依存していれば孤児化を防ぐため拒否。"""
    return store.delete(branch, fid)


# ===================== 取込(既存作品) =====================
@mcp.tool()
def import_facts(branch: str, facts: list) -> dict:
    """既存作品から抽出した事実を一括登録(hard制約でgateしない=矛盾も含め丸ごと読込む)。
    取込後に audit を呼ぶと、既存の矛盾が表面化する。0からの執筆ではなく既存原稿の取込に使う。
    facts は [{subject, attribute, value, chapter, kind?, num?}, ...]。"""
    return store.import_facts(branch, facts)


# ===================== 検証 =====================
@mcp.tool()
def audit(branch: str = "main", as_of_chapter: int = None, include_soft: bool = False) -> dict:
    """ブランチ全体を監査する。
    hard_violations: 決定論的な矛盾(死後の行為/台帳減少/時間循環など)。確実。
    include_soft=True にすると意味的矛盾(回収↔破壊など)をNLIで検出し open-question を生成(モデル未導入なら自動skip)。
    取込直後の健全性チェックや、章を書いた後の確認に使う。"""
    scorer = _get_scorer() if include_soft else None
    res = store.audit(branch, as_of_chapter, scorer)
    if include_soft and scorer is None:
        res["soft_note"] = "NLIモデル未導入のため意味的監査はskip(pip install '.[nlp]' で有効化)"
    return res


# ===================== マージ =====================
@mcp.tool()
def merge_branches(src: str, dst: str) -> dict:
    """src ブランチを dst へ統合(3-way)。片側のみ変更した事実は自動統合。
    両側が同一事実を別の値にした箇所は競合として作者質問(MERGE_CONFLICT)を生成する。
    競合は answer_question で正史を決める。"""
    return store.merge(src, dst)


# ===================== 質問(作者oracleチャネル) =====================
@mcp.tool()
def list_open_questions(branch: str = None, status: str = "open") -> dict:
    """未解決の質問を列挙する。種別: ALIAS(別名同一性) / MERGE_CONFLICT(マージ競合) / SOFT_CONTRADICTION(意味的矛盾の要確認)。
    LLMは推測で解決せず、これを作者に提示して answer_question に回す。"""
    return {"questions": store.list_questions(branch, status)}


@mcp.tool()
def answer_question(qid: int, answer: str) -> dict:
    """作者の回答で質問を解決し、対応する構築操作を確定する。
    ALIAS: answer='同一'で別名統合 / それ以外で別物(cannot_link)。
    MERGE_CONFLICT: answer='src'/'dst'(または値そのもの)で正史を選択。
    SOFT_CONTRADICTION: 作者の判断を記録(自動操作なし)。
    回答は永続化され、以後の整合検査に反映される(別名は検査を貫通する)。"""
    return store.answer_question(qid, answer)


# ===================== 散文→事実 抽出/照合 =====================
def _build_records(chapter_text, chapter, pov_character=None):
    """KWJA(ゼロ照応解決済み)優先、無ければGiNZA(degraded)で述語-項レコードを作る。"""
    try:
        from .kwja_extract import extract_kwja

        return extract_kwja(chapter_text, chapter, pov_character=pov_character)
    except Exception:
        from .extract import ginza_records

        return ginza_records(chapter_text, chapter, pov_character=pov_character)


@mcp.tool()
def extract_facts(chapter_text: str, chapter: int, pov_character: str = None) -> dict:
    """章の散文から述語-項レコードを機構が独立に抽出する(KWJA優先/GiNZA退避)。
    物語固有の属性に畳まず、subject(ゼロ照応解決済み)/predicate/modality(state|event)/
    arguments(ガ/ヲ/ニ)/tense を汎用形で返す。LLMの読み取りと突き合わせる第二の観測。
    抽出は不完全なのでauthoritativeでなく候補。通常は reconcile_facts で差分を見るとよい。
    pov_character を渡すと語り手(著者)に解決された主語をそのキャラに割り当てる。"""
    return _build_records(chapter_text, chapter, pov_character)


@mcp.tool()
def reconcile_facts(chapter: int, llm_facts: list, chapter_text: str, pov_character: str = None) -> dict:
    """LLMが章から抽出した事実(llm_facts)と、機構が独立抽出した述語-項レコードを(主語,述語)軸で突き合わせる。
    llm_facts は [{subject, predicate}, ...]。
    返り値: agreement(一致=確証) / llm_only_check_grounding(本文に根拠が薄い=捏造の疑い) /
    mechanism_only_state_possible_omission(状態の申告漏れ・高シグナル) /
    mechanism_only_event_possible_omission(行為の申告漏れ・死亡等を含む)。
    既知実体(KB)で対象を絞り、ゼロ照応解決済みの主語のみ照合。差分は確定でなく要確認。"""
    from .reconcile import reconcile_records

    known = [f["subject"] for f in store.get_state("main")["facts"]]
    recs = _build_records(chapter_text, chapter, pov_character)["records"]
    return reconcile_records(chapter, llm_facts, recs, known_entities=known)


@mcp.tool()
def import_extracted(branch: str, chapter_text: str, chapter: int, pov_character: str = None) -> dict:
    """章の散文から機構抽出した述語-項レコードを、汎用マッピングでKBに取り込む(gateせず)。
    state→STATE / event→ACT に畳み、述語をvalueにする。取込後 audit で矛盾を表面化できる。
    物語固有の型付け(LIFE=dead等)が要る箇所は、取込後に作者が add_fact で精緻化する。"""
    from .kwja_extract import records_to_facts

    recs = _build_records(chapter_text, chapter, pov_character)["records"]
    facts = records_to_facts(recs)
    return store.import_facts(branch, facts)


def main():
    mcp.run()  # stdio transport (Claude Code / Claude Desktop からローカル起動)


if __name__ == "__main__":
    main()
