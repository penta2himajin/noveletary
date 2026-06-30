# noveletary

> Source: README.md @ (initial commit)
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

初期（v0.1）。コアのエンジン・ストア・ブランチ・マージ・監査・MCPサーバーは実装・テスト済み。日本語NLP抽出層（GiNZA照合／KWJAゼロ照応アダプタ）は任意かつadvisoryで、KWJA経路は配信元モデルサーバの復旧待ち。リモート未デプロイ（Cloudflare Workers + D1 が既知の移行先）。

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

| ツール | 用途 |
|---|---|
| `get_state` / `get_log` | 書く前の状態（章スライス・主体絞り込み）、履歴 |
| `add_fact` / `add_facts` | 事実登録（hard gate）＝0から執筆 |
| `import_facts` | 既存作品の一括取込（gateせず）→ `audit` で矛盾表面化 |
| `update_fact` / `delete_fact` | supersession(+retcon検査) / 削除(孤児化防止) |
| `audit` | hard違反は常時、`include_soft=True` でNLIベースの作者質問を追加 |
| `create_branch` / `merge_branches` / `rollback_branch` | 並行案、構造マージ、非破壊ロールバック |
| `list_open_questions` / `answer_question` | 作者oracleチャネル |
| `extract_facts` / `reconcile_facts` | 散文の独立抽出、LLM自己申告との突き合わせ |

## License（ライセンス）

MIT。[LICENSE](./LICENSE) を参照。
