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
from mcp.types import ToolAnnotations

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
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "branch"})
def list_branches() -> dict:
    """[branch] 全ブランチ(物語の版/プロット案)を列挙する。"""
    return {"branches": store.list_branches()}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "branch"})
def create_branch(name: str, from_branch: str = "main", at_op: int = None) -> dict:
    """[branch] 新しいブランチを作る。並行プロット(A案/B案)やif展開の検討に使う。状態コピーは起きない(ポインタのみ)。
    from_branch の現在地(または at_op で指定した操作)から分岐する。"""
    return store.create_branch(name, from_branch, at_op)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True), meta={"category": "branch"})
def delete_branch(name: str) -> dict:
    """[branch] ブランチを削除する(不要になった実験/デモ枝の掃除)。操作ログは不変なので残り、
    ポインタ・未解決質問・スナップショット(派生キャッシュ)のみ消える。main は削除不可。"""
    return store.delete_branch(name)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True), meta={"category": "branch"})
def rollback_branch(branch: str, to_op: int) -> dict:
    """[branch] ブランチを過去の操作IDまで巻き戻す。操作ログは不変なので巻き戻しの巻き戻しも可能。
    LLMの一連の編集で矛盾が入った時の安全網。"""
    return store.rollback(branch, to_op)


# ===================== 読取(書く前に呼ぶ) =====================
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "read"})
def get_state(branch: str = "main", as_of_chapter: int = None, subject: str = None, as_of_narrated: int = None) -> dict:
    """[read] ブランチで現在有効な事実を返す。各factは chapter(=valid-time/物語内時間)と narrated_in(=discourse-time/語りの章)を持つ。
    as_of_chapter: valid-time スライス=「その章時点の世界」(retcon後でも正しい)。
    as_of_narrated: discourse-time スライス=「第N章まで読んだ読者が知っている事実」(伏線/叙述トリックの検証用)。
      両者は独立軸。回想(物語内は過去・語りは後の章)は chapter と narrated_in が食い違う。
    subject を指定すると特定エンティティだけに絞る(文脈節約)。章を書く前の状態確認に使う。"""
    return store.get_state(branch, as_of_chapter, subject, as_of_narrated=as_of_narrated)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "read"})
def chapter_brief(branch: str = "main", chapter: int = 1) -> dict:
    """[read] 第N章を書く前に要る正準を1発で束ねる(想起負担の軽減)。返り値:
    characters(LIFE/RANKを持つ人物の生死alive/地位/位置/呼称) / world(STATEのみの世界・設定) / constraints(有効なhard制約) /
    open_questions(未解決) / open_setups(未回収の伏線; payoff_by超過は overdue) / recent(直近[N-2,N]の行為・順序・生死)。
    執筆ループの先頭で呼ぶと、get_state を何度も引かずに文脈を再構成できる。"""
    return store.chapter_brief(branch, chapter)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "outline"})
def set_beat(branch: str, chapter: int, beat: str) -> dict:
    """[outline] 章ビート(その章の設計=1段落: 誰が出て何が起き何が変わり何を仕込む/回収するか)を登録/更新する(アウトライン先行)。
    執筆前にビートを置けば、本文生成は『ビートを現在カノンに矛盾せず展開する』低負荷タスクになり、漂流が減る。
    同章への再登録は更新(冪等)。chapter_brief に当該章の beat が同梱される。"""
    return store.set_beat(branch, chapter, beat)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "outline"})
def get_outline(branch: str = "main", from_chapter: int = None, to_chapter: int = None) -> dict:
    """[outline] 章ビート(プロット骨格)を章順で返す。range 指定可。各部の頭でビートを並べて整合を俯瞰するのに使う。"""
    return {"branch": branch, "outline": store.get_outline(branch, from_chapter, to_chapter)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "outline"})
def add_setup(branch: str, setup: str, chapter: int, payoff_by: int = None, thread: str = "伏線") -> dict:
    """[outline] 伏線(チェーホフの銃)を登録して未回収を追跡する。setup=仕込みの説明, chapter=仕込んだ(語った)章,
    payoff_by=回収すべき期限の章(任意; 超過すると chapter_brief/open_setups で overdue 表示), thread=伏線の識別名。
    回収したら resolve_setup で閉じる。100章規模で「張ったが回収し忘れ」を防ぐ台帳。"""
    return store.add_setup(branch, setup, chapter, payoff_by=payoff_by, subject=thread)


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True), meta={"category": "outline"})
def resolve_setup(branch: str, fid: str) -> dict:
    """[outline] 伏線を回収済みにする(現在の未回収一覧から外す。操作ログは不変なので履歴は残る)。fid は open_setups の値。"""
    return store.resolve_setup(branch, fid)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "read"})
def get_log(branch: str = "main", limit: int = 50) -> dict:
    """[read] ブランチの操作履歴(新しい順)。op_id はロールバック先の指定に使える。"""
    return {"branch": branch, "log": store.get_log(branch, limit)}


# ===================== 構築(執筆) =====================
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "fact"})
def add_fact(
    branch: str,
    subject: str,
    attribute: str,
    value: str,
    chapter: int,
    kind: str = "STATE",
    num: int = None,
    narrated_in: int = None,
    valid_to: int = None,
) -> dict:
    """[fact] 事実を1件登録(hard制約でgate)。0から執筆する時の基本操作。
    attribute例: LIFE(生死: value=alive/dead) / ACT(行為) / LOC(位置) / RANK(地位) / LEDGER(台帳: numに数値, kind=COUNTER) / ORDER(時間順序: value='A<B') / STATE(一般)。
    chapter は valid-time(物語内時間)の開始章。フルーエントは区間 [chapter, valid_to) で保持。制約検査はこの軸で行う。
    valid_to は valid-time の終了章(排他)。未指定なら +∞(開区間; supersession で暗黙終了)。
      生前の経歴/居所を死で畳む等に使う。例: LOC=工房 chapter=0 valid_to=1(第1章の死で終了→以後は不可視・死後行為と衝突しない)。
    narrated_in は discourse-time(語りの章)=原稿のどの章で開示されるか。未指定なら chapter と同値(順送り)。
      回想/倒叙で「物語内は過去・語りは後」を表す。例: chapter=1, narrated_in=10(第10章で明かす第1章の真実)。
    矛盾(死後の行為・台帳の減少・時間循環等)があれば status=rejected と矛盾fact集合を返す。
    別名の疑い(ALIAS質問)は、subject が既存主体と表層的に近い時に自動発火する
    (判定: 複数語名=タイトル除く共有語 / 単一語名=文字集合Jaccard≥0.3。アウトライン(BEAT/SETUP)は対象外)。同一ペアの未解決質問は1つに集約(重複しない)。
    発火すると question_id を返す(list_open_questions→answer_question)。
    先回りするなら link_entities(same=False で別人固定 / same=True で同一固定) を使う。"""
    return store.add(
        branch, subject, attribute, value, chapter, kind, num, gate=True, narrated_in=narrated_in, valid_to=valid_to
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "fact"})
def add_facts(branch: str, facts: list, atomic: bool = False) -> dict:
    """[fact] 複数の事実をまとめて登録(各々hard制約でgate)。1シーン分の事実を一括投入する時に。
    facts は [{subject, attribute, value, chapter, kind?, num?, narrated_in?, valid_to?}, ...]。
    chapter=valid-time開始(物語内時間), valid_to=valid-time終了(排他, 未指定なら+∞), narrated_in=discourse-time(語りの章, 未指定なら chapter と同値; 回想/伏線用)。
    atomic=False(既定): 逐次適用。1件矛盾しても他はcommitされ得る(部分適用が残る)。
    atomic=True: 1件でも矛盾したらバッチ全体を巻き戻し何も適用しない(中途半端な状態を残さない)。
    返り値: {results:[committed/rejected,...], applied: 適用されたか, (atomicで巻戻時)rolled_back_to_op}。"""
    return store.add_many(branch, facts, atomic=atomic, gate=True)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "fact"})
def retag_fact(
    branch: str,
    fid: str,
    chapter: int = None,
    attribute: str = None,
    valid_to: int = None,
    narrated_in: int = None,
    value: str = None,
    num: int = None,
) -> dict:
    """[fact] 既存事実を同じ fid のまま付け替える/更新する(delete+re-add 不要)。指定しない項目(None)は据え置き。
    用途: 値の更新(value/num)、章の移動(chapter)、属性の付け替え(attribute)、生前の経歴/居所を死で畳む(valid_to)、開示章の修正(narrated_in)。
    retcon 同様に hard 再検査が走り、矛盾すれば status=rejected(retag) で適用しない(操作ログは不変なので過去版は履歴に残る)。
    注: valid_to/narrated_in を ∞/既定へ戻すのは不可(rare; delete_fact + add_fact で)。"""
    return store.retag(
        branch,
        fid,
        chapter=chapter,
        attribute=attribute,
        valid_to=valid_to,
        narrated_in=narrated_in,
        value=value,
        num=num,
    )


@mcp.tool(annotations=ToolAnnotations(destructiveHint=True), meta={"category": "fact"})
def delete_fact(branch: str, fid: str) -> dict:
    """[fact] 事実を削除。他factが依存していれば孤児化を防ぐため拒否。"""
    return store.delete(branch, fid)


# ===================== 取込(既存作品) =====================
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "fact"})
def import_facts(branch: str, facts: list) -> dict:
    """[fact] 既存作品から抽出した事実を一括登録(hard制約でgateしない=矛盾も含め丸ごと読込む)。
    取込後に audit を呼ぶと、既存の矛盾が表面化する。0からの執筆ではなく既存原稿の取込に使う。
    facts は [{subject, attribute, value, chapter, kind?, num?}, ...]。"""
    return store.import_facts(branch, facts)


# ===================== 検証 =====================
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "verify"})
def audit(branch: str = "main", as_of_chapter: int = None, include_soft: bool = False) -> dict:
    """[verify] ブランチ全体を監査する。
    hard_violations: 決定論的な矛盾(死後の行為/台帳減少/時間循環など)。確実。
    include_soft=True にすると意味的矛盾(回収↔破壊など)をNLIで検出し open-question を生成(モデル未導入なら自動skip)。
    取込直後の健全性チェックや、章を書いた後の確認に使う。"""
    scorer = _get_scorer() if include_soft else None
    res = store.audit(branch, as_of_chapter, scorer)
    if include_soft and scorer is None:
        res["soft_note"] = "NLIモデル未導入のため意味的監査はskip(pip install '.[nlp]' で有効化)"
    return res


# ===================== マージ =====================
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "branch"})
def merge_branches(src: str, dst: str) -> dict:
    """[branch] src ブランチを dst へ統合(3-way)。片側のみ変更した事実は自動統合。
    両側が同一事実を別の値にした箇所は競合として作者質問(MERGE_CONFLICT)を生成する。
    競合は answer_question で正史を決める。"""
    return store.merge(src, dst)


# ===================== 質問(作者oracleチャネル) =====================
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "question"})
def list_open_questions(branch: str = None, status: str = "open") -> dict:
    """[question] 未解決の質問を列挙する。種別と発火条件:
    - ALIAS(別名同一性): add系で新subjectが既存主体と表層的に近い時に自動発火(複数語=共有語/単一語=文字Jaccard≥0.3; BEAT/SETUPは対象外)。同一ペアは集約(重複しない)。
    - MERGE_CONFLICT(マージ競合): merge_branches で両ブランチが同一(subj,attr)を別値にした時。
    - SOFT_CONTRADICTION(意味的矛盾の要確認): audit(include_soft=True) のNLIが contradiction 判定した時(モデル未導入ならskip)。
    LLMは推測で解決せず、これを作者に提示して answer_question に回す。"""
    return {"questions": store.list_questions(branch, status)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "question"})
def link_entities(branch: str, a: str, b: str, same: bool) -> dict:
    """[question] 2つの呼称の同一性を作者が明示宣言する(ALIAS質問を待たず能動的に)。
    same=True : a を b の別名として統合(bが正準)。偽名・あだ名・正体判明など「実は同一人物」を一級事実化。
      統合後はエンジンが両者を1実体として検査するので正体レベルの矛盾(故人の行為など)も検出。hard_violations を返す。
    same=False: a と b を別人(別指示対象)として固定(cannot_link)。同姓の別人など。以後この対で ALIAS 質問は出ない。
    answer_question の '同一'/'別物' と等価。"""
    return store.assert_alias(branch, a, b) if same else store.assert_distinct(branch, a, b)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "question"})
def answer_question(qid: int, answer: str) -> dict:
    """[question] 作者の回答で質問を解決し、対応する構築操作を確定する。
    ALIAS: answer='同一'で別名統合 / それ以外で別物(cannot_link)。
    MERGE_CONFLICT: answer='src'/'dst'(または値そのもの)で正史を選択。
    SOFT_CONTRADICTION: 作者の判断を記録(自動操作なし)。
    回答は永続化され、以後の整合検査に反映される(別名は検査を貫通する)。"""
    return store.answer_question(qid, answer)


# ===================== 制約(作品別ルール / 操作ログでversioned) =====================
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "constraint"})
def list_constraints(branch: str = "main") -> dict:
    """[constraint] ブランチで有効な制約(hard規則)を列挙する。各制約は cid / template / params / enabled / note を持つ。
    制約はコード直書きでなくデータで、ブランチ単位でversion管理される(分岐で継承・ロールバックで巻戻る)。"""
    return {"branch": branch, "constraints": store.list_constraints(branch)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "constraint"})
def add_constraint(branch: str, template: str, params: dict, scope: dict = None, note: str = "") -> dict:
    """[constraint] 制約を1件追加する(作者の指示でルールを微調整)。template:
    - forbid_after_state: 終端状態の後に特定属性を禁止(EC慣性)。params={terminal_attr, terminal_value, forbidden_attrs}。例: 死後の行為禁止。
    - monotone: 数値の単調性。params={attr, direction("nondecreasing"|"nonincreasing")}。例: 台帳の増加。
    - acyclic: 順序の無循環。params={order_attr}。例: 時間順序。
    - release: 終端状態の解放(EC Release)=forbid_after_stateの例外。params={terminal_attr, terminal_value, subject?}。例: 派生作で死者復活を許可。
    scope={subject:..} で対象主体を限定可。"""
    return store.add_constraint(branch, template, params, scope, note)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), meta={"category": "constraint"})
def set_constraint(branch: str, cid: str, enabled: bool = None, remove: bool = False) -> dict:
    """[constraint] 制約のライフサイクル操作を1つに集約。remove=True で削除(操作ログは不変なのでロールバックで復活/デフォルトも消せる)。
    enabled=False で無効化(一時停止)、enabled=True で再有効化。両方指定時は remove を優先。"""
    if remove:
        return store.remove_constraint(branch, cid)
    if enabled is None:
        return {"error": "enabled(true/false) か remove=true のいずれかを指定してください"}
    return store.set_constraint_enabled(branch, cid, enabled)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "constraint"})
def check_constraints(branch: str = "main", eager: bool = None) -> dict:
    """[constraint] 制約セットの構造的な矛盾・無効設定を検査する(遅延/オンデマンド)。
    検出: contradictory_monotone(増減両立) / duplicate(重複) / orphan_release(対応forbid無し) /
    shadowed_forbid(全体releaseで死蔵)。consistent=Trueなら設定上の問題なし。
    eager を渡すと実行モードも切替: True=add_constraint 時に自動検査して警告を添える / False=明示呼びのみ(既定)。"""
    if eager is not None:
        store.set_constraint_check_eager(eager)
    return store.check_constraints(branch)


# ===================== 散文→事実 抽出/照合 =====================
def _build_records(chapter_text, chapter, pov_character=None):
    """KWJA(ゼロ照応解決済み)優先、無ければGiNZA(degraded)で述語-項レコードを作る。"""
    try:
        from .kwja_extract import extract_kwja

        return extract_kwja(chapter_text, chapter, pov_character=pov_character)
    except Exception:
        from .extract import ginza_records

        return ginza_records(chapter_text, chapter, pov_character=pov_character)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "nlp"})
def reconcile_facts(chapter: int, llm_facts: list, chapter_text: str, pov_character: str = None) -> dict:
    """[nlp] LLMが章から抽出した事実(llm_facts)と、機構が独立抽出した述語-項レコードを(主語,述語)軸で突き合わせる。
    llm_facts は [{subject, predicate}, ...]。
    返り値: agreement(一致=確証) / llm_only_check_grounding(本文に根拠が薄い=捏造の疑い) /
    mechanism_only_state_possible_omission(状態の申告漏れ・高シグナル) /
    mechanism_only_event_possible_omission(行為の申告漏れ・死亡等を含む)。
    既知実体(KB)で対象を絞り、ゼロ照応解決済みの主語のみ照合。差分は確定でなく要確認。"""
    from .reconcile import reconcile_records

    known = [f["subject"] for f in store.get_state("main")["facts"]]
    recs = _build_records(chapter_text, chapter, pov_character)["records"]
    return reconcile_records(chapter, llm_facts, recs, known_entities=known)


# ===================== NLP 起動時セットアップ(抽出を標準に) =====================
def _nlp_modules_present() -> bool:
    """NLP抽出スタック(KWJA優先 + GiNZA)が import 可能かを軽量チェック(モデルはロードしない)。"""
    import importlib.util

    return all(importlib.util.find_spec(m) is not None for m in ("kwja", "spacy", "ja_ginza"))


def _nlp_extra_requirements() -> list:
    """インストール済みメタデータから nlp extra の依存を取得(pyproject とのドリフトを避ける)。"""
    try:
        from importlib.metadata import requires

        reqs = requires("noveletary") or []
        nlp = [r.split(";")[0].strip() for r in reqs if 'extra == "nlp"' in r]
        if nlp:
            return nlp
    except Exception:  # noqa: BLE001
        pass
    # メタデータが無い場合のフォールバック(pyproject の nlp extra と同期)
    return [
        "ginza>=5.2",
        "ja-ginza>=5.2",
        "kwja>=2.5",
        "rhoknp>=1.6",
        "transformers>=4.50,<4.51",
        "huggingface_hub>=0.26,<0.31",
        "torch>=2.0",
    ]


def ensure_nlp(verbose: bool = True) -> bool:
    """NLP抽出を標準とするための起動時セットアップ。
    未導入なら nlp extra を自動インストールする(NOVELETARY_NLP_AUTOSETUP=0 で無効化)。
    pip出力は stdout(MCPプロトコルchannel)を汚さないよう stderr に流す。
    戻り値: セットアップ後にNLPが利用可能か。"""
    import sys

    if _nlp_modules_present():
        return True
    if os.environ.get("NOVELETARY_NLP_AUTOSETUP", "1").lower() not in ("1", "true", "yes", "on"):
        if verbose:
            print(
                "[noveletary] NLP未導入・自動セットアップ無効(NOVELETARY_NLP_AUTOSETUP=0)。"
                "extract/reconcile/import_extracted と soft監査はskipされます。",
                file=sys.stderr,
            )
        return False

    import subprocess

    cmd = [sys.executable, "-m", "pip", "install", *_nlp_extra_requirements()]
    if verbose:
        print(f"[noveletary] NLP抽出スタック未導入。起動時セットアップを開始: {' '.join(cmd)}", file=sys.stderr)
    try:
        subprocess.run(cmd, check=True, stdout=sys.stderr, stderr=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(
            f"[noveletary] NLP自動セットアップ失敗: {e}\n"
            "  手動導入: pip install '.[nlp]'(KWJAはPython<3.14が必要)。NLPなしでも core 機能は動作します。",
            file=sys.stderr,
        )
        return False
    ok = _nlp_modules_present()
    if verbose:
        print(f"[noveletary] NLPセットアップ{'完了' if ok else '未完(要確認)'}。", file=sys.stderr)
    return ok


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), meta={"category": "nlp"})
def propose_canon_facts(branch: str, chapter_text: str, chapter: int, pov_character: str = None) -> dict:
    """[nlp] 章の散文から記帳の下書きを生成する(記帳自動化)。機構抽出(KWJA優先/GiNZA退避)→正準スキーマへ写像
    →既存カノンと差分→採否しやすく仕分けて返す。**コミットしない**(候補)。
    返り値: high_new(状態/既知実体の行為=採用候補) / low_new(未知主語の瑣末行為=要確認) / existing(既出=除外) / summary。
    使い方: high_new を確認・取捨して add_facts(atomic) で確定。本文を書いた直後に呼べば記帳の二重労働が消える。
    注: 値は複合名詞句を復元済みだが物語型(LIFE/RANK等)には畳まないので、必要なら採用後に retag_fact で精緻化する。"""
    from .kwja_extract import records_to_facts
    from .reconcile import triage_candidates

    recs = _build_records(chapter_text, chapter, pov_character)["records"]
    candidates = records_to_facts(recs)
    st = store.get_state(branch)
    out = triage_candidates(candidates, st["facts"], st.get("aliases"))
    out["chapter"] = chapter
    return out


def main():
    ensure_nlp()  # NLP抽出を標準に: 未導入なら起動時に自動セットアップ(NOVELETARY_NLP_AUTOSETUP=0 で無効化)
    mcp.run()  # stdio transport (Claude Code / Claude Desktop からローカル起動)


if __name__ == "__main__":
    main()
