"""
gemma4_writer_harness.py — Gemma 4 E4B (Q4_0 GGUF, llama.cpp/CPU) を
noveletary の Store に直結し、ツール呼び出しループで短編を書かせる実験ハーネス。

MCP stdio 経由ではなく Store を直接ラップする(FastMCP の JSON-RPC は本質でない;
検証したいのはローカル小型モデルがツール呼び出し形式を守って執筆できるか)。

Gemma 4 のチャットテンプレートは独自のセンチネルトークンを使う:
  <|turn>role\n ... <turn|>            — ターン境界
  <|tool>declaration:NAME{...}<tool|>  — システムターン内のツール宣言
  <|tool_call>call:NAME{args}<tool_call|>   — モデルが発行するツール呼び出し
  <|tool_response>response:NAME{...}<tool_response|> — 実行結果の注入
テンプレート全体を jinja2 で辿るのではなく、上記トークンを手組みしてループする
(llama-cpp-python の高レベル chat API はこの独自トークン列を完全にはサポートしないため)。
"""

import json
import re
import sys
from pathlib import Path

from llama_cpp import Llama

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from noveletary.store import Store  # noqa: E402

MODEL_PATH = sys.argv[1] if len(sys.argv) > 1 else "/root/models/gemma-4-E4B-it-Q4_0.gguf"
DB_PATH = "/tmp/gemma4_novel_experiment.db"
BRANCH = "main"

TOOLS = [
    {
        "name": "chapter_brief",
        "description": "第N章を書く前に必要な正準情報(人物の生死・地位、世界設定、有効な制約、未解決の質問、未回収の伏線)を一括取得する。",
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


def render_system(system_text: str) -> str:
    return f"<|turn>system\n{system_text}\n{tool_decl_block()}<turn|>\n"


def render_user(text: str) -> str:
    return f"<|turn>user\n{text}\n<turn|>\n"


def parse_args(argstr: str) -> dict:
    # Gemma の独自引数フォーマット key:value,key:value を緩くJSON化して読む
    argstr = argstr.strip()
    if not argstr:
        return {}
    # 文字列値の <|"|> ... <|"|> を通常の " ... " に正規化してから key: を "key": に
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
            return store.add(
                BRANCH,
                args["subject"],
                args["attribute"],
                args["value"],
                int(args["chapter"]),
                gate=True,
            )
        if name == "add_setup":
            return store.add_setup(
                BRANCH,
                args["setup"],
                int(args["chapter"]),
                payoff_by=args.get("payoff_by"),
            )
        if name == "audit":
            return store.audit(BRANCH)
        return {"error": f"unknown tool {name}"}
    except Exception as e:
        return {"error": str(e)}


def main():
    print(f"loading model: {MODEL_PATH}", file=sys.stderr)
    llm = Llama(
        model_path=MODEL_PATH,
        n_ctx=8192,
        n_threads=4,
        n_gpu_layers=0,
        verbose=False,
    )

    Path(DB_PATH).unlink(missing_ok=True)
    store = Store(DB_PATH)

    system_text = (
        "あなたは短編小説家です。noveletary という物語整合性検証システムのツールを使い、"
        "第1章(400字程度、日本語)を書いてください。手順:\n"
        "1. chapter_brief ツールで chapter=1 の状況を確認する\n"
        "2. set_beat でこの章のビートを登録する\n"
        "3. 登場人物の生死・行為などの事実を add_fact で登録する(矛盾チェックのため)\n"
        "4. 必要なら add_setup で伏線を仕込む\n"
        "5. audit で矛盾がないか確認する\n"
        "6. 最後に本文を自然な日本語の地の文として出力する(ツール呼び出しは使わない)\n"
        "ツールを呼ぶときは必ず <|tool_call>call:NAME{key:value,...}<tool_call|> の形式のみで出力してください。"
    )
    user_text = (
        "舞台は日本の地方都市。主人公は「陽子」という名の古書店主。"
        "ある日、店に届いた一箱の古書の中から、20年前に失踪した友人の手記を見つける — "
        "という導入で第1章を書いてください。"
    )

    prompt = render_system(system_text) + render_user(user_text) + "<|turn>model\n"

    transcript = prompt
    max_rounds = 8
    final_prose = None

    for round_i in range(max_rounds):
        print(f"\n=== round {round_i} : generating ===", file=sys.stderr)
        out = llm.create_completion(
            transcript,
            max_tokens=600,
            temperature=0.7,
            stop=["<tool_call|>", "<turn|>"],
        )
        text = out["choices"][0]["text"]
        print(text, file=sys.stderr)

        transcript += text

        if "<|tool_call>call:" in text:
            # 出力は stop トークンで切れているので閉じタグを補って再パース
            call_text = text.split("<|tool_call>call:", 1)[1]
            close_idx = call_text.rfind("}")
            name = call_text.split("{", 1)[0].strip()
            argstr = call_text[call_text.find("{") + 1 : close_idx] if close_idx != -1 else ""
            args = parse_args(argstr)
            transcript += "<tool_call|>"
            print(f"  -> tool_call: {name}({args})", file=sys.stderr)
            result = call_tool(store, name, args)
            print(f"  <- result: {result}", file=sys.stderr)
            resp_json = json.dumps(result, ensure_ascii=False)
            transcript += f"<|tool_response>response:{name}{{result:{resp_json}}}<tool_response|>"
            transcript += "<turn|>\n<|turn>model\n"
            continue

        # ツール呼び出しではない = 最終本文とみなす
        final_prose = text.strip()
        break

    print("\n\n========== FINAL PROSE ==========\n", file=sys.stderr)
    print(final_prose or "(生成されずタイムアウト)")

    print("\n\n========== STORE STATE (chapter_brief ch1) ==========", file=sys.stderr)
    print(json.dumps(store.chapter_brief(BRANCH, 1), ensure_ascii=False, indent=2), file=sys.stderr)

    print("\n========== AUDIT ==========", file=sys.stderr)
    print(json.dumps(store.audit(BRANCH), ensure_ascii=False, indent=2), file=sys.stderr)

    with open("/tmp/gemma4_novel_transcript.txt", "w") as f:
        f.write(transcript)
    print("\nfull transcript saved to /tmp/gemma4_novel_transcript.txt", file=sys.stderr)


if __name__ == "__main__":
    main()
