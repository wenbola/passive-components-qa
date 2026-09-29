# -*- coding: utf-8 -*-
"""
qa_server.py —— 作业A 第四步：问答页面（后端）

【这个脚本能做什么】
起一个本地网页服务：浏览器里输入问题 → 本地双路检索（向量 + BM25）
→ 把命中的原文块交给 DeepSeek 生成答案 → 页面显示「答案 + 出处」。

【检索逻辑】（每一步都在终端/日志里打印进度）
1. 向量检索：本机 Qwen3-Embedding-0.6B 把问题编码成向量（查询侧加指令前缀
   Instruct/Query、last-token 池化、L2 归一化 —— 和 build_index.py 完全一致），
   与 vectors.npy 做点积（= 余弦相似度），取前 10。
2. BM25 检索：bm25.pkl（rank_bm25）+ text_tokenize.tokenize（和建索引共用），取前 10。
3. 融合去重：RRF（score = Σ 1/(60+名次)），同一块被两路命中分数叠加（天然去重）；
   按融合分排序取最终前 6 块交给大模型。

【密钥怎么走】
DeepSeek 密钥只从环境变量 DEEPSEEK_API_KEY 读：
- 不写进任何文件；不下发浏览器；终端与日志都不打印密钥本身。
- 没配置也不崩：检索照常跑，页面答案区会提示未配置。
- 兜底：进程环境里没有时，再读一次注册表里的用户级/机器级环境变量 ——
  用 setx 刚设完、本服务不用重启也能读到。
- 可选覆盖：DEEPSEEK_BASE_URL（默认 https://api.deepseek.com）、
  DEEPSEEK_MODEL（默认 deepseek-chat）。

【产出】（写入 D 盘项目目录）
  logs/qa_server_YYYY-MM-DD.log   终端打印过的进度同步写一份，方便事后查

【用法】
  python scripts/qa_server.py              # 或双击 scripts/run_qa_server.cmd
  python scripts/qa_server.py --port 8765  # 换端口
  浏览器打开 http://127.0.0.1:8765
  页面支持 ?q=问题 —— 打开就自动提问（演示、截图用）

【这个脚本不能做什么】
- 不改索引、不改语料：只读 data/clean/ 下的索引与块文件。
- 不做账号体系、不做生产部署：定位是本机/局域网演示（Flask dev server）。
"""

import argparse
import datetime
import json
import os
import pickle
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TAG = "[qa]"

ROOT = Path(__file__).resolve().parent.parent
IDX_DIR = ROOT / "data" / "clean" / "index"
CHUNKS_FILE = ROOT / "data" / "clean" / "chunks" / "chunks.jsonl"
META_FILE = IDX_DIR / "index_meta.json"
VEC_FILE = IDX_DIR / "vectors.npy"
BM25_FILE = IDX_DIR / "bm25.pkl"
PAGE_FILE = Path(__file__).resolve().parent / "qa_page.html"
LOG_DIR = ROOT / "logs"

HOST = "0.0.0.0"      # 监听所有网卡（同一局域网也能访问）
PORT = 8765           # 已确认没被占用
ROUTE_K = 10          # 每路取前几块
FINAL_K = 6           # 融合后给大模型看几块
RRF_K = 60            # RRF 平滑常数（经典取值）
CACHE_MAX = 50        # 问题→答案缓存条数（重复提问直接回放，演示/截图更快）

SYS_PROMPT = (
    "你是一个 A 股年报问答助手。只依据用户提供的【材料】回答，不要使用材料之外的任何知识。"
    "【材料】是 15 家被动元件上市公司 2025 年年度报告的原文块，每块都带出处标签（公司·页）。"
    "要求：1) 直接回答问题，引用材料里的原始数字（如需换算单位，换算过程要说明）；"
    "2) 每条关键信息后面用【公司简称·第X页】标注出处；"
    "3) 材料里找不到答案就明确说“材料中未找到”，绝对不要编造。"
)

_log_fp = None
STATE = {"chunks": None, "vecs": None, "bm25": None, "tok": None, "model": None,
         "tokenize": None, "instruct": "", "dim": 0}
MODEL_LOCK = threading.Lock()   # torch 模型并发前向不稳，加锁串行
CACHE = {}
CACHE_LOCK = threading.Lock()


def say(msg):
    print(f"{TAG} {msg}", flush=True)
    if _log_fp is not None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        _log_fp.write(f"[{ts}] {msg}\n")
        _log_fp.flush()


def plain_error(exc):
    say(f"出错：{type(exc).__name__}")
    say(f"原始信息：{exc}")


def get_api_key():
    """DeepSeek 密钥：进程环境 → 注册表用户级 → 注册表机器级。
    只取值，不打印值。返回 (key, 来源说明)。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key, "进程环境变量"
    try:
        import winreg
        locations = [
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ]
        for root, sub in locations:
            try:
                with winreg.OpenKey(root, sub) as k:
                    v, _ = winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")
                    v = str(v or "").strip()
                    if v:
                        return v, "注册表环境变量（setx 后无需重启本服务）"
            except OSError:
                continue
    except Exception:
        pass
    return "", ""


def load_everything():
    import numpy as np

    say(f"读块文件 {CHUNKS_FILE.name}…")
    chunks = []
    with CHUNKS_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    say(f"  {len(chunks):,} 块")

    meta = json.loads(META_FILE.read_text(encoding="utf-8"))

    say("读向量表 vectors.npy…（整读，不用 mmap：Windows 上会锁文件）")
    vecs = np.load(VEC_FILE)
    say(f"  {vecs.shape[0]:,} 行 × {vecs.shape[1]} 列 {vecs.dtype}")

    say("读 BM25…")
    with BM25_FILE.open("rb") as f:
        bm25 = pickle.load(f)["bm25"]
    say("  就绪")

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from text_tokenize import tokenize   # 建索引和查询共用同一个分词

    say("加载本机模型（CPU，强制 fp32）…")
    import torch
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(os.cpu_count() or 8)
    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(meta["模型目录"], padding_side="left")
    model = AutoModel.from_pretrained(meta["模型目录"], dtype=torch.float32)
    model.eval()
    say(f"  模型就绪（{time.time() - t0:.1f}s）")

    STATE.update(chunks=chunks, vecs=vecs, bm25=bm25, tok=tok, model=model,
                 tokenize=tokenize, instruct=meta["查询指令前缀"],
                 dim=meta["向量维度"])


def encode_query(question):
    import torch
    q = f"Instruct: {STATE['instruct']}\nQuery: {question}"
    enc = STATE["tok"]([q], padding=True, truncation=True, max_length=4096,
                       return_tensors="pt")
    with MODEL_LOCK, torch.inference_mode():
        res = STATE["model"](**enc, use_cache=False)
        h = getattr(res, "last_hidden_state", res[0])[:, -1].float()
        h = torch.nn.functional.normalize(h, p=2, dim=1)
        return h[0].numpy().astype("float32")


def retrieve(question):
    import numpy as np

    t0 = time.time()
    qv = encode_query(question)
    sims = STATE["vecs"] @ qv
    v_top = [int(i) for i in np.argsort(-sims)[:ROUTE_K]]
    t_vec = time.time() - t0
    c = STATE["chunks"][v_top[0]]
    say(f"向量检索 {ROUTE_K} 条，用时 {t_vec:.2f}s ｜ top1：{c['公司']} 第{c['物理页']}页 "
        f"cos={float(sims[v_top[0]]):.3f}")

    t0 = time.time()
    b_scores = STATE["bm25"].get_scores(STATE["tokenize"](question))
    b_top = [int(i) for i in np.argsort(-b_scores)[:ROUTE_K]]
    t_bm = time.time() - t0
    c = STATE["chunks"][b_top[0]]
    say(f"BM25 检索 {ROUTE_K} 条，用时 {t_bm:.2f}s ｜ top1：{c['公司']} 第{c['物理页']}页 "
        f"score={float(b_scores[b_top[0]]):.2f}")

    # —— 融合去重：RRF，同一块两路都中会叠加；按块下标天然去重 ——
    fused = {}

    def add(i, rank, route, score):
        d = fused.setdefault(i, {"rrf": 0.0, "vec": None, "bm": None})
        d["rrf"] += 1.0 / (RRF_K + rank)
        d[route] = (rank, float(score))

    for rank, i in enumerate(v_top, 1):
        add(i, rank, "vec", sims[i])
    for rank, i in enumerate(b_top, 1):
        add(i, rank, "bm", b_scores[i])
    order = sorted(fused.items(), key=lambda kv: -kv[1]["rrf"])[:FINAL_K]
    both = sum(1 for _, d in order if d["vec"] and d["bm"])
    say(f"两路融合去重 → 取 {len(order)} 块（其中 {both} 块两路都命中）")

    sources = []
    for i, d in order:
        c = STATE["chunks"][i]
        sources.append({
            "索引": i,
            "公司": c["公司"],
            "文件": c["文件"],
            "报告期": c["报告期"],
            "物理页": c["物理页"],
            "印刷页码": c["印刷页码"],
            "面包屑": c["面包屑"],
            "文本": c["文本"],
            "向量名次": d["vec"][0] if d["vec"] else None,
            "向量相似度": round(d["vec"][1], 4) if d["vec"] else None,
            "BM25名次": d["bm"][0] if d["bm"] else None,
            "BM25得分": round(d["bm"][1], 2) if d["bm"] else None,
        })
    return sources, t_vec, t_bm


def ask_deepseek(question, sources):
    """返回 (答案, 错误说明)。没配密钥不算崩溃：检索已完成，答案区给提示。"""
    key, src = get_api_key()
    if not key:
        return None, "未配置 DEEPSEEK_API_KEY（检索已完成，见下方出处；配置密钥后即可生成答案）"
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

    material = []
    for n, s in enumerate(sources, 1):
        material.append(f"[材料{n}] {s['面包屑']}\n{s['文本']}")
    user_msg = "【材料】\n" + "\n\n".join(material) + f"\n\n【问题】{question}"

    import requests
    say(f"调 DeepSeek（密钥来源：{src}，模型 {model}）…")
    t0 = time.time()
    resp = None
    for attempt in (1, 2):
        try:
            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "system", "content": SYS_PROMPT},
                                   {"role": "user", "content": user_msg}],
                      "temperature": 0.2, "max_tokens": 1200, "stream": False},
                timeout=120)
            break
        except requests.exceptions.RequestException as e:
            if attempt == 2:
                return None, f"连接 DeepSeek 失败：{type(e).__name__}: {e}"
            say(f"  DeepSeek 连接失败（{type(e).__name__}），2 秒后重试一次")
            time.sleep(2)

    dt = time.time() - t0
    if resp.status_code != 200:
        return None, f"DeepSeek 接口返回 HTTP {resp.status_code}：{resp.text[:300]}"
    data = resp.json()
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None, f"DeepSeek 返回结构异常：{str(data)[:300]}"
    usage = data.get("usage") or {}
    say(f"DeepSeek 生成完成，用时 {dt:.1f}s（prompt {usage.get('prompt_tokens', '?')} tok / "
        f"completion {usage.get('completion_tokens', '?')} tok）")
    return answer, None


def build_app():
    from flask import Flask, jsonify, request, send_file

    app = Flask(__name__)
    try:
        app.json.ensure_ascii = False
    except Exception:
        pass

    @app.get("/")
    def page():
        return send_file(PAGE_FILE, mimetype="text/html")

    @app.get("/api/health")
    def health():
        key, _ = get_api_key()
        return jsonify({"ok": True, "块数": len(STATE["chunks"]),
                        "向量维度": STATE["dim"], "密钥已配置": bool(key)})

    @app.post("/api/ask")
    def api_ask():
        q = str((request.get_json(silent=True) or {}).get("question", "")).strip()
        if not q:
            return jsonify({"error": "问题为空"}), 400
        say(f"收到问题：{q}")
        with CACHE_LOCK:
            cached = CACHE.get(q)
        if cached is not None:
            say("  这个问题刚问过，缓存直接回放（不再花 API 调用）")
            return jsonify(cached)

        t0 = time.time()
        sources, t_vec, t_bm = retrieve(q)
        answer, err = ask_deepseek(q, sources)
        payload = {
            "问题": q,
            "答案": answer,
            "答案错误": err,
            "出处": sources,
            "用时": {"向量检索秒": round(t_vec, 2), "BM25秒": round(t_bm, 2),
                    "合计秒": round(time.time() - t0, 2)},
        }
        with CACHE_LOCK:
            if len(CACHE) >= CACHE_MAX:
                CACHE.pop(next(iter(CACHE)))
            CACHE[q] = payload
        if err:
            say(f"完成（答案未生成）：{err}")
        say(f"完成，合计 {time.time() - t0:.1f}s")
        return jsonify(payload)

    return app


def main():
    global _log_fp
    ap = argparse.ArgumentParser(description="作业A 问答页面（本地检索 + DeepSeek 生成答案）")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    log_name = f"qa_server_{datetime.date.today():%Y-%m-%d}.log"
    _log_fp = (LOG_DIR / log_name).open("a", encoding="utf-8")

    say("=" * 52)
    say(f"启动命令：python scripts/qa_server.py --port {args.port}")
    say(f"进度同步写入 logs/{log_name}")
    load_everything()
    key, src = get_api_key()
    if key:
        say(f"DeepSeek 密钥：已读到（来源：{src}）")
    else:
        say("DeepSeek 密钥：未配置 —— 检索仍可用，答案区会提示；设 setx DEEPSEEK_API_KEY 后无需重启")
    app = build_app()
    say(f"服务就绪，监听 {args.host}:{args.port}（局域网可访问）")
    say(f"访问地址：http://127.0.0.1:{args.port}")
    say("提示：URL 加 ?q=你的问题 —— 打开页面就自动提问（演示/截图用）")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("收到 Ctrl+C，已停止")
    except Exception as e:
        traceback.print_exc()
        plain_error(e)
        sys.exit(1)
