"""
gemma4_writer_harness_longform.py — 長編版。gemma4_writer_harness.py の単章デモを
複数章・目標文字数(既定10,000字)まで拡張し、コンテキスト超過に備えた簡易compactionを持つ。

前提: Gemma 4 E4B Q4_0 は n_ctx を大きく取れる(GGUFメタデータ上は131072)が、
CPU/メモリの都合でここでは 8192 に据え置く。原稿と事実(chapter_brief の登場人物/世界/
直近ログ)が章を追うごとに肥大するため、8192では数章でtranscriptが溢れる — これは
意図的な設定で、compactionが実際に発火する状況を作っている。

Compaction方針(単純化):
  - 各ラウンド生成前に現transcriptのトークン数を数え、閾値(n_ctx - 生成用リザーブ)を
    超えていたら、今の会話ターン列を破棄し「新しいセッション」を開始する。
  - 新セッションの system/user ターンには、(a) 直近原稿の末尾(継続性のための再掲)、
    (b) その時点の chapter_brief をハーネス側が直接呼んで埋め込んだ正準情報、
    (c) 「この続きから章Nを書け」という指示、を含める。
  - つまり compaction = 「会話履歴を捨てて、正準(canon)をツールから再取得し土台に
    据え直す」。noveletary の chapter_brief がまさにこの再取得のために設計された
    ツールなので、それをcompactionの再グラウンディング手段として使う。
  - 章の区切りはモデルに <<CHAPTER_END>> という明示マーカーを地の文の末尾に出力させて
    検出する(ツール呼び出しではない自由文の終端をパースで頼りにするより頑健)。
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


def new_session_prompt(llm: Llama, store: Store, chapter: int, manuscript: list[str], recap_chars: int) -> str:
    """compaction: 会話履歴を捨て、正準をツールから直接取り直して土台に据え直す。"""
    brief = store.chapter_brief(BRANCH, chapter)
    recap = "".join(manuscript)[-recap_chars:] if manuscript else ""
    extra_system = (
        f"(セッション再構築: これまでに書いた原稿の直近抜粋と、第{chapter}章時点の正準情報を"
        "以下に与える。これを土台に矛盾なく続きを書くこと。)\n"
        f"直近原稿の抜粋:\n{recap}\n\n"
        f"第{chapter}章時点の正準情報(chapter_brief結果):\n{json.dumps(brief, ensure_ascii=False)}"
    )
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
    a = ap.parse_args()

    print(f"loading model: {a.model_path} (n_ctx={a.n_ctx})", file=sys.stderr)
    llm = Llama(model_path=a.model_path, n_ctx=a.n_ctx, n_threads=4, n_gpu_layers=0, verbose=False)

    Path(a.db).unlink(missing_ok=True)
    store = Store(a.db)

    intro = (
        "舞台は日本の地方都市。主人公は「陽子」という名の古書店主。ある日、店に届いた一箱の古書の中から、"
        "20年前に失踪した友人の手記を見つける — という導入で第1章から始めて、"
        f"合計で{a.target_chars}字程度になるまで複数章を書き進めてください(1章あたり目安{a.chapter_char_target}字)。"
    )

    chapter = 1
    manuscript: list[str] = []  # 完成した章のプレーンテキストのみ(compactionの外側で保持=モデルのcontextは食わない)
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
            transcript = new_session_prompt(llm, store, chapter, manuscript, recap_chars=600)
            tc = token_count(llm, transcript)
            print(f"[compaction] rebuilt transcript = {tc} tok", file=sys.stderr)

        print(
            f"\n=== round {round_i} (chapter {chapter}, transcript={tc} tok, "
            f"manuscript={total_chars()}/{a.target_chars} chars) ===",
            file=sys.stderr,
        )
        out = llm.create_completion(
            transcript,
            max_tokens=700,
            temperature=0.7,
            stop=["<tool_call|>", "<turn|>"],
        )
        text = out["choices"][0]["text"]
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
            chapter += 1
            chapter_buffer = ""
            if total_chars() >= a.target_chars or chapter > a.max_chapters:
                break
            transcript += render_user(f"続けて第{chapter}章を書いてください。") + "<|turn>model\n"
        else:
            # マーカーが出ないまま生成が終わった(max_tokens到達など)。続きを生成させる。
            transcript += "<turn|>\n<|turn>model\n"

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
