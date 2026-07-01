# noveletary

> Source: README.md @ ddc0b1e
>
> [English](./README.md)

**novel + secretary** — 小説の内的整合性を検証する、制約維持された物語知識ベース兼MCPサーバー。日本語散文を第一級でサポートする。

## What（これは何か）

執筆中・取込中の小説に対して、LLM（Claude Code、Claude.ai Projects）が呼び出すローカル [MCP](https://modelcontextprotocol.io) サーバー。物語の事実を追記専用の操作ログで追跡し、書込時に矛盾をgateし、並行プロット案を分岐させ、機構が決定できない問いは作者へ回す。

- **構築＝検査つき書込**。事実の追加時にhard制約（死者の行為・台帳の減少・時間順序の循環・削除時の孤児化）を検査し、矛盾は矛盾fact集合つきで拒否。
- **検証＝同じエンジンの一括モード**。ブランチ全体を監査。hard違反は確実、任意の意味的検査（NLI）は作者質問になる。
- **物語ブランチ**が第一級。並行案（A案/B案）を独立に監査し、構造的競合検出つきでマージ、履歴を失わずロールバック。
- **作者がoracle**。未解決の別名・マージ競合・意味的疑念はLLMの推測でなく作者へ。回答は永続化し、以後の検査を貫通する。

## Why（なぜ）

LLMが執筆と自己採点を兼ねる整合性検査は、無駄が多く信頼できない。noveletaryは決定論的な制約エンジンと作者を信頼の核に据え、LLMは権限を持たない可謬な翻訳器として扱う。小説の「矛盾」の多くは構造的に決定可能（状態機械・数値不変条件・時間制約）で意味理解を要さない。意味的残余だけを言語モデルが判定し、それでも結論は「gate」でなく「質問」である。

## Status（現状）

初期（v0.1）。コアのエンジン・ストア・三時制の事実（valid区間 / discourse / transaction）・ブランチ・マージ・監査・アウトラインビート＋伏線台帳・MCPサーバーは実装・テスト済み。日本語NLP抽出層（KWJAゼロ照応＋名詞句復元、GiNZA退避）は標準経路だがadvisoryのまま——`propose_canon_facts` が散文からカノン候補を下書きし作者が採否する（gateしない）。KWJAはPython<3.14が必要で、初回利用時にチェックポイントを自己充填する。リモート未デプロイ（Cloudflare Workers + D1 が既知の移行先）。

## Install（導入）

```bash
pip install -e ".[dev]"          # コア + テスト
pip install -e ".[dev,nlp]"      # 日本語NLP(GiNZA, KWJA)を追加
```

## MCPサーバーとして起動

```bash
noveletary-mcp                                   # stdio
claude mcp add noveletary -- noveletary-mcp      # Claude Codeに登録
```

SQLite状態は `data/narrative.db` に永続化（repo rootから起動。`NARRATIVE_DB` で上書き可）。

## ツール一覧（LLM向け）

事実は**三時制**：`chapter` は valid-time を区間 `[chapter, valid_to)` で表す（物語内で真になる時点）、`narrated_in` は discourse-time（どの章で開示するか＝伏線/回想）、操作ログが transaction-time。各ツールの説明は先頭にカテゴリが付き、read-only / destructive の注釈を持つ。

| カテゴリ | ツール | 用途 |
|---|---|---|
| **read** | `get_state`, `chapter_brief`, `get_log` | 書く前の状態（valid- / discourse-time スライス）。`chapter_brief` は 登場人物 / 世界 / 制約 / 未解決質問 / 未回収伏線 / 直近＋章ビート を1発で束ねる |
| **fact** | `add_fact`, `add_facts`, `retag_fact`, `delete_fact`, `import_facts` | 事実登録（hard gate・atomicバッチ）/ 同fidのまま付替 / 削除（孤児化防止）/ 既存作品の一括取込（gateせず→`audit`） |
| **branch** | `create_branch`, `delete_branch`, `rollback_branch`, `merge_branches`, `list_branches` | 並行案、構造マージ、非破壊ロールバック、掃除 |
| **constraint** | `list_constraints`, `add_constraint`, `set_constraint`, `check_constraints` | 作品固有のhard規則をデータとして、ブランチ単位でversion管理 |
| **question** | `list_open_questions`, `answer_question`, `link_entities` | 作者oracleチャネル。`link_entities` は2つの呼称を同一/別人と宣言 |
| **verify** | `audit` | hard違反は常時、`include_soft=True` でNLIベースの作者質問を追加 |
| **outline** | `set_beat`, `get_outline`, `add_setup`, `resolve_setup` | アウトライン先行のビートと、チェーホフの銃の台帳（回収期限つき伏線追跡） |
| **nlp** | `reconcile_facts`, `propose_canon_facts` | 機構による散文抽出——章からカノン候補を下書き、またはLLM自己申告との突き合わせ |

未監視のエージェント / Claude Code のサブエージェントから駆動するには、ツール個別でなくサーバ全体（`mcp__noveletary`）を許可リストへ——詳細は `AGENTS.md`。

## License（ライセンス）

MIT。[LICENSE](./LICENSE) を参照。
