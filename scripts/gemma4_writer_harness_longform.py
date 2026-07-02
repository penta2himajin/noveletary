"""
gemma4_writer_harness_longform.py — 長編版。gemma4_writer_harness.py の単章デモを
複数章・目標文字数(既定10,000字)まで拡張し、コンテキスト超過に備えた簡易compactionを持つ。

前提: Gemma 4 E4B Q4_0 は n_ctx を大きく取れる(GGUFメタデータ上は131072)が、
CPU/メモリの都合でここでは 8192 に据え置く。原稿と事実(chapter_brief の登場人物/世界/
直近ログ)が章を追うごとに肥大するため、8192では数章でtranscriptが溢れる — これは
意図的な設定で、compactionが実際に発火する状況を作っている。

Compaction方針(3層: 要約 + 抜粋 + 正準):
  - 各ラウンド生成前に現transcriptのトークン数を数え、閾値(n_ctx - 生成用リザーブ)を
    超えていたら、今の会話ターン列を破棄し「新しいセッション」を開始する。
  - 新セッションの system/user ターンには次の3つを与える:
    (a) ローリング要約 — 章が1つ完了するたびに(compaction時ではなく)専用の短い生成で
        作る2〜3文の要約を積み上げたもの。抜粋の窓の外にある長期の展開・モチーフを
        保持するための soft な長期記憶。
    (b) 直近原稿の抜粋 — 直前の生テキスト数百字。文体・トーン・語彙選択を継続させる
        ためのアンカー(要約からは再現できない)。
    (c) chapter_brief をハーネス側が直接呼んで埋め込んだ正準情報 — 矛盾しないことを
        保証する唯一の hard な権威。要約や抜粋と食い違う場合はこちらを優先させる。
  - つまり compaction = 「会話履歴を捨てて、(soft長期記憶=要約, soft直近文体=抜粋,
    hard真実=正準) の3層で土台を作り直す」。noveletary の chapter_brief が(c)の再取得に
    設計されたツールなので、それをcompactionの再グラウンディング手段として使う。
    要約自体はこのモデル自身が生成する不正確な圧縮なので、hard制約のgateには一切使わない
    (noveletary本体の hard/soft 分離をharness側にも延長した設計)。
  - 章の区切りはモデルに <<CHAPTER_END>> という明示マーカーを地の文の末尾に出力させて
    検出する(ツール呼び出しではない自由文の終端をパースで頼りにするより頑健)。
  - 生成継続: max_tokens到達で文が物理的に途中打ち切りになった場合(finish_reason=="length")は
    ターン境界を挟まずそのまま続きを生成させる(単純に completion を続ける)。ターン境界
    <|turn>model を挟んでしまうと、モデルが「新しいターン」と誤認して直前の展開を書き直し、
    文章が重複する不具合があったため。モデルが自発的にターンを終えた(finish_reason=="stop"
    かつマーカー未検出)場合のみ、ユーザーターンで続行を明示的に促す。
"""

import argparse
import json
import sys
from pathlib import Path

from llama_cpp import Llama

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from noveletary.store import Store  # noqa: E402

BRANCH = "main"
CHAPTER_END_MARKER = "<<CHAPTER_END>>"

TOOLS = [
    {
        "name": "chapter_brief",
        "description": "第N章を書く前に必要な正準情報(人物の生死・地位、世界設定、有効な制約、未解決の質問、未回収の伏線、直近の出来事)を一括取得する。",
        "parameters": {"chapter": "int"},
    },
    {
        "name": "set_beat",
        "description": "章のビート(誰が出て何が起き何が変わり何を仕込む/回収するか、1段落)をアウトラインとして登録する。",
        "parameters": {"chapter": "int", "beat": "string"},
    },
    {
        "name": "add_fact",
        "description": "物語内の事実を1件登録する。attribute例: LIFE(alive/dead)/ACT(行為)/LOC(位置)/RANK(地位)/STATE(一般)。矛盾があれば拒否される。",
        "parameters": {
            "subject": "string",
            "attribute": "string",
            "value": "string",
            "chapter": "int",
        },
    },
    {
        "name": "add_setup",
        "description": "伏線(チェーホフの銃)を登録する。あとで resolve_setup で回収する。",
        "parameters": {"setup": "string", "chapter": "int", "payoff_by": "int"},
    },
    {
        "name": "resolve_setup",
        "description": "open_setups にある伏線を回収済みにする。fid は chapter_brief の open_setups[].fid を使う。",
        "parameters": {"fid": "string"},
    },
    {
        "name": "audit",
        "description": "ブランチ全体の矛盾を監査する(hard_violations があれば直す必要がある)。",
        "parameters": {},
    },
]


def tool_decl_block() -> str:
    parts = []
    for t in TOOLS:
        props = ",".join(f'{k}:{{type:"{v.upper()}"}}' for k, v in t["parameters"].items())
        parts.append(
            f'<|tool>declaration:{t["name"]}{{description:"{t["description"]}"'
            f',parameters:{{properties:{{{props}}},type:"OBJECT"}}}}<tool|>'
        )
    return "".join(parts)


SYSTEM_PREAMBLE = (
    "あなたは長編小説家です。noveletary という物語整合性検証システムのツールを使い、"
    "複数章からなる短編小説を書いています。各章の手順:\n"
    "1. chapter_brief でその章の状況(既出の事実・矛盾しないための制約・未回収の伏線)を確認する\n"
    "2. set_beat でこの章のビートを登録する\n"
    "3. 必要な事実を add_fact で登録する(矛盾チェックのため)\n"
    "4. 必要なら add_setup で新しい伏線を仕込み、期限が来た伏線は resolve_setup で回収する\n"
    "5. 最後に本文(800〜1400字程度、日本語の地の文)をツール呼び出しなしで出力し、"
    f"本文の一番最後に必ず {CHAPTER_END_MARKER} という文字列だけを付け加えて章を終える\n"
    "ツールを呼ぶときは必ず <|tool_call>call:NAME{key:value,...}<tool_call|> の形式のみで出力してください。"
)


def render_system(extra: str = "") -> str:
    body = SYSTEM_PREAMBLE + (("\n\n" + extra) if extra else "")
    return f"<|turn>system\n{body}\n{tool_decl_block()}<turn|>\n"


def render_user(text: str) -> str:
    return f"<|turn>user\n{text}\n<turn|>\n"


def parse_args_str(argstr: str) -> dict:
    import re

    argstr = argstr.strip()
    if not argstr:
        return {}
    argstr = argstr.replace('<|"|>', '"')
    argstr = re.sub(r"(\w+):", r'"\1":', argstr)
    try:
        return json.loads("{" + argstr + "}")
    except Exception as e:
        print(f"  [warn] failed to parse tool args {argstr!r}: {e}", file=sys.stderr)
        return {}


def call_tool(store: Store, name: str, args: dict) -> dict:
    try:
        if name == "chapter_brief":
            return store.chapter_brief(BRANCH, int(args.get("chapter", 1)))
        if name == "set_beat":
            return store.set_beat(BRANCH, int(args["chapter"]), args["beat"])
        if name == "add_fact":
            return store.add(BRANCH, args["subject"], args["attribute"], args["value"], int(args["chapter"]), gate=True)
        if name == "add_setup":
            return store.add_setup(BRANCH, args["setup"], int(args["chapter"]), payoff_by=args.get("payoff_by"))
        if name == "resolve_setup":
            return store.resolve_setup(BRANCH, args["fid"])
        if name == "audit":
            return store.audit(BRANCH)
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": str(e)}


def summarize_chapter(llm: Llama, chapter: int, text: str) -> str:
    """章が完成するたびに(compaction時ではなく)呼ぶ。抜粋の窓の外に出た長期の展開・
    モチーフを、ローリング要約として保持するための短い専用生成。soft(不正確な圧縮の
    可能性あり)なので、hard制約のgateには一切使わない。"""
    prompt = (
        f"<|turn>user\n次は第{chapter}章の本文です。後で続きを書く際に思い出せるよう、"
        "固有名詞・具体的な出来事・雰囲気やモチーフを優先して2〜3文の日本語で要約してください。"
        "前置きなしで要約文だけを出力してください。\n\n"
        f"{text}\n<turn|>\n<|turn>model\n"
    )
    out = llm.create_completion(prompt, max_tokens=200, temperature=0.3, stop=["<turn|>"])
    return out["choices"][0]["text"].strip()


def new_session_prompt(
    llm: Llama,
    store: Store,
    chapter: int,
    manuscript: list[str],
    chapter_summaries: list[str],
    chapter_buffer: str,
    recap_chars: int,
) -> str:
    """compaction: 会話履歴を捨て、(要約=長期soft記憶, 抜粋=直近の文体soft記憶,
    chapter_brief=hard正準)の3層で土台を作り直す。

    chapter_buffer(現在の章で、まだ <<CHAPTER_END>> を出す前に書かれた地の文)が
    非空の場合は「章の途中でcompactionが発火した」ケース: その書きかけの本文をそのまま
    モデルの手番の続きとして与え、そのまま生成を継続させる(章を書き直す指示を出すと、
    モデルが既に書いた分を知らずに似た内容を再度書いてしまうため)。"""
    brief = store.chapter_brief(BRANCH, chapter)
    summary_block = "\n".join(chapter_summaries) if chapter_summaries else "(まだなし)"
    extra_system = (
        f"(セッション再構築: これまでの章のローリング要約・第{chapter}章時点の正準情報を以下に与える。"
        "要約は文体とおおまかな流れを思い出すための手がかりに過ぎない(不正確な場合がある)。"
        "正準情報は矛盾しないための唯一の権威であり、食い違う場合は正準情報を優先すること。"
        "これらを土台に矛盾なく続きを書くこと。)\n"
        f"これまでの章のローリング要約:\n{summary_block}\n\n"
        f"第{chapter}章時点の正準情報(chapter_brief結果):\n{json.dumps(brief, ensure_ascii=False)}"
    )
    if chapter_buffer.strip():
        user_text = f"上記を踏まえ、以下に示す書きかけの第{chapter}章の続きをそのまま書いてください。"
        return render_system(extra_system) + render_user(user_text) + "<|turn>model\n" + chapter_buffer[-recap_chars:]

    recap = "".join(manuscript)[-recap_chars:] if manuscript else ""
    extra_system += f"\n\n直近原稿(前章末尾)の抜粋:\n{recap}"
    user_text = f"上記の続きとして、第{chapter}章を書いてください。"
    return render_system(extra_system) + render_user(user_text) + "<|turn>model\n"


def token_count(llm: Llama, text: str) -> int:
    return len(llm.tokenize(text.encode("utf-8"), add_bos=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model_path")
    ap.add_argument("--target-chars", type=int, default=10000)
    ap.add_argument("--chapter-char-target", type=int, default=1200)
    ap.add_argument("--max-chapters", type=int, default=14)
    ap.add_argument("--n-ctx", type=int, default=8192)
    ap.add_argument("--compact-reserve-tokens", type=int, default=1400)
    ap.add_argument("--db", default="/tmp/gemma4_novel_longform.db")
    ap.add_argument("--out", default="/tmp/gemma4_novel_longform.txt")
    ap.add_argument("--transcript-out", default="/tmp/gemma4_novel_longform_transcript.txt")
    ap.add_argument("--n-threads", type=int, default=4)
    ap.add_argument(
        "--dump-compaction-dir",
        default=None,
        help="set to dump the verbatim pre/post-compaction transcript text to files in this dir (debugging aid)",
    )
    ap.add_argument(
        "--stop-after-compactions",
        type=int,
        default=None,
        help="exit right after this many compaction events have been observed (fast repro of the compaction path)",
    )
    ap.add_argument(
        "--premise",
        default=(
            "舞台は日本の地方都市。主人公は「陽子」という名の古書店主。ある日、店に届いた一箱の古書の中から、"
            "20年前に失踪した友人の手記を見つける"
        ),
        help="第1章の導入(一文で状況設定)。--target-chars と --chapter-char-target は自動で付記される。",
    )
    a = ap.parse_args()

    if a.dump_compaction_dir:
        Path(a.dump_compaction_dir).mkdir(parents=True, exist_ok=True)

    print(f"loading model: {a.model_path} (n_ctx={a.n_ctx})", file=sys.stderr)
    llm = Llama(model_path=a.model_path, n_ctx=a.n_ctx, n_threads=a.n_threads, n_gpu_layers=0, verbose=False)

    Path(a.db).unlink(missing_ok=True)
    store = Store(a.db)

    intro = (
        f"{a.premise} — という導入で第1章から始めて、"
        f"合計で{a.target_chars}字程度になるまで複数章を書き進めてください(1章あたり目安{a.chapter_char_target}字)。"
    )

    chapter = 1
    manuscript: list[str] = []  # 完成した章のプレーンテキストのみ(compactionの外側で保持=モデルのcontextは食わない)
    chapter_summaries: list[str] = []  # ローリング要約(章完了ごとに追記; compactionの土台の一部)
    compaction_log = []
    transcript = render_system() + render_user(intro) + "<|turn>model\n"
    budget = a.n_ctx - a.compact_reserve_tokens
    chapter_buffer = ""  # 現在の章でツール呼び出し以外に出力された地の文の蓄積

    total_chars = lambda: sum(len(c) for c in manuscript)  # noqa: E731

    round_i = 0
    while total_chars() < a.target_chars and chapter <= a.max_chapters:
        round_i += 1
        tc = token_count(llm, transcript)
        if tc > budget:
            compaction_log.append(
                {"round": round_i, "chapter": chapter, "pre_compaction_tokens": tc, "manuscript_chars": total_chars()}
            )
            print(
                f"\n[compaction] round {round_i}: transcript={tc} tok > budget={budget} -> rebuilding session, "
                f"re-grounding via chapter_brief(chapter={chapter})",
                file=sys.stderr,
            )
            if a.dump_compaction_dir:
                n = len(compaction_log)
                Path(a.dump_compaction_dir, f"compaction_{n:02d}_pre.txt").write_text(transcript, encoding="utf-8")
            transcript = new_session_prompt(
                llm, store, chapter, manuscript, chapter_summaries, chapter_buffer, recap_chars=600
            )
            tc = token_count(llm, transcript)
            print(f"[compaction] rebuilt transcript = {tc} tok", file=sys.stderr)
            if a.dump_compaction_dir:
                n = len(compaction_log)
                Path(a.dump_compaction_dir, f"compaction_{n:02d}_post.txt").write_text(transcript, encoding="utf-8")
            if a.stop_after_compactions and len(compaction_log) >= a.stop_after_compactions:
                print(
                    f"[compaction] reached --stop-after-compactions={a.stop_after_compactions}, exiting early",
                    file=sys.stderr,
                )
                break

        print(
            f"\n=== round {round_i} (chapter {chapter}, transcript={tc} tok, "
            f"manuscript={total_chars()}/{a.target_chars} chars) ===",
            file=sys.stderr,
        )
        out = llm.create_completion(
            transcript,
            max_tokens=800,
            temperature=0.7,
            stop=["<tool_call|>", "<turn|>"],
        )
        text = out["choices"][0]["text"]
        finish_reason = out["choices"][0]["finish_reason"]
        print(text, file=sys.stderr)
        transcript += text

        if "<|tool_call>call:" in text:
            call_text = text.split("<|tool_call>call:", 1)[1]
            close_idx = call_text.rfind("}")
            name = call_text.split("{", 1)[0].strip()
            argstr = call_text[call_text.find("{") + 1 : close_idx] if close_idx != -1 else ""
            args = parse_args_str(argstr)
            transcript += "<tool_call|>"
            print(f"  -> tool_call: {name}({args})", file=sys.stderr)
            result = call_tool(store, name, args)
            print(f"  <- result: {result}", file=sys.stderr)
            resp_json = json.dumps(result, ensure_ascii=False)
            transcript += f"<|tool_response>response:{name}{{result:{resp_json}}}<tool_response|>"
            transcript += "<turn|>\n<|turn>model\n"
            continue

        # ツール呼び出しでない = 地の文
        chapter_buffer += text
        if CHAPTER_END_MARKER in chapter_buffer:
            prose, _, _rest = chapter_buffer.partition(CHAPTER_END_MARKER)
            prose = prose.strip()
            manuscript.append(prose)
            print(
                f"\n[chapter {chapter} done] {len(prose)} chars (total {total_chars()}/{a.target_chars})",
                file=sys.stderr,
            )
            summary = summarize_chapter(llm, chapter, prose)
            chapter_summaries.append(f"第{chapter}章: {summary}")
            print(f"[summary] chapter {chapter}: {summary}", file=sys.stderr)
            chapter += 1
            chapter_buffer = ""
            if total_chars() >= a.target_chars or chapter > a.max_chapters:
                break
            transcript += render_user(f"続けて第{chapter}章を書いてください。") + "<|turn>model\n"
        elif finish_reason == "length":
            # max_tokensで物理的に途中打ち切られただけ。ターン境界を挟まず、そのまま
            # completion を続けさせる(ターンを挟むとモデルが新しいターンと誤認して直前の
            # 展開を書き直し、文章が重複する不具合があったため)。
            continue
        else:
            # モデルが(マーカーなしで)自発的にターンを終えた。ユーザーターンで続行を促す。
            transcript += render_user("まだ本文が終わっていません。続きを書いてください。") + "<|turn>model\n"

    final_manuscript = "\n\n".join(f"## 第{i + 1}章\n\n{c}" for i, c in enumerate(manuscript))
    Path(a.out).write_text(final_manuscript, encoding="utf-8")
    Path(a.transcript_out).write_text(transcript, encoding="utf-8")

    print("\n\n========== MANUSCRIPT ==========\n", file=sys.stderr)
    print(final_manuscript)

    print("\n\n========== SUMMARY ==========", file=sys.stderr)
    print(
        json.dumps(
            {
                "chapters_written": len(manuscript),
                "total_chars": total_chars(),
                "rounds": round_i,
                "compactions": len(compaction_log),
                "compaction_log": compaction_log,
                "chapter_summaries": chapter_summaries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        file=sys.stderr,
    )

    print("\n========== FINAL AUDIT (whole branch) ==========", file=sys.stderr)
    print(json.dumps(store.audit(BRANCH), ensure_ascii=False, indent=2), file=sys.stderr)

    print(f"\nmanuscript saved to {a.out}", file=sys.stderr)
    print(f"final transcript saved to {a.transcript_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
