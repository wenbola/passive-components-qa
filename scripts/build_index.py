# -*- coding: utf-8 -*-
"""
build_index.py —— 给切好的块建两套索引：本地向量 + BM25

【这个脚本能做什么】
读 data/clean/chunks/chunks.jsonl（7,470 块），建两套能互补的索引：

① **向量索引**：本机 Qwen3-Embedding-0.6B（CPU 推理，不联网、不调 API）逐块编码。
   中文语义检索靠它 —— 用户问「哪家公司的研发投入最多」，
   原文写的是「研发费用」，字面不重合但语义能对上，这是 BM25 做不到的。
② **BM25 索引**：jieba 分词后的词面索引。财务问答里大量是精确匹配
   （「600563」「23,194,230.30」「2025 年营业收入」），
   这类查询字面命中就够准，而且比向量快、可解释。两套混合用取长补短。

Qwen3-Embedding 的用法要点：
- 池化用**最后一个 token**，不是平均池化 —— 这个模型是 decoder 结构，
  官方指定的就是 last-token pooling。配 left padding 时 `[:, -1]` 才对。
- 向量 **L2 归一化**，之后余弦相似度就等于内积，检索时一次矩阵乘法搞定。
- 查询侧要加指令前缀（`Instruct: ... \\nQuery: ...`），文档侧不加。
  这个指令串写进 index_meta.json，查询时从那里读，保证两边一致。

【产出】（都在 data/clean/index/）
  emb_shards/emb_00000.npy  向量分片，每片 2,048 块，float32
  vectors.npy               全部分片拼起来的总表（N × 1024）
  bm25.pkl                  BM25 模型
  index_meta.json           模型名、维度、块数、参数、耗时（查询脚本的配置来源）

【断点续跑】（两层）
  分片级：已存在且形状对的 emb_xxxxx.npy 直接认，只补没做的。
  批级：  片内每编完一批就把半成品写进 partial_emb_xxxxx.npy/.json，
          段错误/断电最多丢一批，重跑自动从半成品接着做。
  BM25：bm25.pkl 存在且块数与当前 jsonl 一致就跳过。
  `--stage vec` / `--stage bm25` 可只跑其中一个。

【保护】
  跑前：可用提交内存低于 6 GB 直接不跑（起跑线体检）。
  跑中：每批盯内存，低于 2 GB 先强制 gc，低于 1.2 GB 主动干净退出 ——
        宁可有秩序地停，也不要段错误；已完成/半成品都在盘上，重跑接着做。

【诊断】每次跑生成带时间戳的文件，不覆盖历史：
  logs/build_index_vec_batches_<时间戳>.log   逐批日志：每批的块数/字符/token/
                                              用时/实时内存。崩了就找最后一条
                                              只有 START 没有 OK 的批，那是现场。
  logs/build_index_faulthandler_<时间戳>.txt  native 崩溃（段错误）时的 Python
                                              栈转储；全程没崩的话是空文件。

【这个脚本不能做什么】
- 不联网：模型是本地目录，路径写死在下面。
- 不改块：只读 chunks.jsonl。
- 不做混合排序：这里只建索引。怎么融合两路分数是查询脚本的事。

【用法】
  python scripts/build_index.py              # 两套都建（各自断点续跑）
  python scripts/build_index.py --stage vec  # 只建向量
  python scripts/build_index.py --stage bm25 # 只建 BM25
  python scripts/build_index.py --force      # 忽略已有分片，全部重算
  python scripts/build_index.py --stage vec --attn eager   # 换注意力实现（备用）
  调试：--limit N --index-dir 临时目录（必须成对出现，防止污染正式索引）
"""

import gc
import hashlib
import json
import os
import pickle
import re
import sys
import time
import datetime
import traceback
import faulthandler
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 段错误（如 oneDNN/SDPA 原生代码崩）时把 Python 栈打进 stderr，别让崩溃无声无息
faulthandler.enable()

TAG = "[index]"


def say(msg):
    print(f"{TAG} {msg}", flush=True)


def plain_error(exc):
    say(f"出错：{type(exc).__name__}")
    say(f"原始信息：{exc}")


def mem_avail():
    """(可用物理 GB, 可用提交 GB)；取不到返回 None。提交内存=物理+页面文件，
    大分配失败往往是它先见底。"""
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = _MS()
        m.dwLength = ctypes.sizeof(_MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 1024 ** 3, m.ullAvailPageFile / 1024 ** 3
    except Exception:
        return None


# ---- 本轮日志（逐批日志 + 崩溃转储）---------------------------------------
RUN = {"stamp": "", "batch_fp": None, "fault_fp": None,
       "batch_path": None, "fault_path": None}


def _hms(sec):
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec % 60:02d}s"


def blog(msg):
    """逐批日志只写文件不刷屏；每行都 flush，进程被段错误带走也不丢。"""
    fp = RUN["batch_fp"]
    if fp is None:
        return
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    fp.write(f"[{ts}] {msg}\n")
    fp.flush()


def setup_run_logs():
    """本轮专属的逐批日志和 faulthandler 转储文件（带时间戳，不覆盖历史）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    RUN["stamp"] = stamp
    RUN["batch_path"] = LOG_DIR / f"build_index_vec_batches_{stamp}.log"
    RUN["fault_path"] = LOG_DIR / f"build_index_faulthandler_{stamp}.txt"
    RUN["batch_fp"] = RUN["batch_path"].open("a", encoding="utf-8")
    RUN["fault_fp"] = RUN["fault_path"].open("w", encoding="utf-8")
    faulthandler.enable(file=RUN["fault_fp"])   # 段错误时栈直接写进文件
    say(f"逐批日志 → logs/{RUN['batch_path'].name}")
    say(f"崩溃转储 → logs/{RUN['fault_path'].name}（为空 = 全程没崩）")
    blog(f"=== 本轮开始 pid={os.getpid()} 参数 {' '.join(sys.argv[1:]) or '（无）'} ===")


def _log_exc():
    """异常退出时把完整栈写进逐批日志。"""
    blog("！异常退出：\n" + traceback.format_exc())


# ---- 配置 ----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CHUNKS = ROOT / "data" / "clean" / "chunks" / "chunks.jsonl"
IDX_DIR = ROOT / "data" / "clean" / "index"
SHARD_DIR = IDX_DIR / "emb_shards"
VEC_FILE = IDX_DIR / "vectors.npy"
BM25_FILE = IDX_DIR / "bm25.pkl"
META_FILE = IDX_DIR / "index_meta.json"
LOG_DIR = ROOT / "logs"

MODEL_DIR = Path(r"D:\AI_Workspace\shared_data\models\Qwen3-Embedding-0.6B")
MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
QUERY_INSTRUCT = ("Given a question about a Chinese A-share annual report, "
                  "retrieve passages that answer it")

SHARD = 2048          # 每片块数
MAX_LEN = 4096        # 单块最大 token 数（Qwen3-Embedding 支持 32k，这里留够用）
# 批次上限从 16 块/12000 字符减半到 8 块/6000 字符：注意力内存随 token 数平方增长，
# 减半后瞬时峰值约降为 1/4。本机页面文件已从 3 GB 调到 8 GB，但前两次都在片 0
# 段错误过，保护版继续按小批次跑，稳妥优先（代价是速度略降）。
BATCH_ITEMS = 8       # 一批最多几块
BATCH_CHARS = 6000    # 一批最多几字符（控制内存和 padding 浪费）

# —— 保护阈值 ——
START_MIN_COMMIT_GB = 6.0   # 起跑线：可用提交内存低于这个值先不跑
GC_COMMIT_GB = 2.0          # 低于此值先强制 gc
STOP_COMMIT_GB = 1.2        # gc 后仍低于此值，主动干净退出
HEARTBEAT_SECS = 60         # 控制台心跳间隔：够看进度，又不刷屏

sys.path.insert(0, str(Path(__file__).resolve().parent))
from text_tokenize import tokenize   # noqa: E402  建索引和查询共用这一个


def load_chunks():
    chunks = []
    with CHUNKS.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks


# ---- 向量索引 ------------------------------------------------------------
def _sha1_texts(texts):
    """本片文本指纹：续跑时核对半成品存档是否还对得上当前 chunks。"""
    h = hashlib.sha1()
    for t in texts:
        h.update(t.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _load_partial(sid, texts, dim, n_total):
    """读上次没写完的本片半成品；任何一处对不上就返回 None（本片从头做）。
    宁可多算一点，也不拼出错位的向量。"""
    import numpy as np
    pn = SHARD_DIR / f"partial_emb_{sid:05d}.npy"
    pj = SHARD_DIR / f"partial_emb_{sid:05d}.json"
    if not (pn.exists() and pj.exists()):
        return None
    try:
        stj = json.loads(pj.read_text(encoding="utf-8"))
        arr = np.load(pn)
        ok = (stj.get("shard") == sid and stj.get("n_total") == n_total
              and stj.get("dim") == dim and stj.get("texts_sha1") == _sha1_texts(texts)
              and arr.shape == (len(texts), dim))
        if not ok:
            raise ValueError("存档和当前数据对不上")
        done = [int(i) for i in stj.get("done", [])]
        if not done:
            raise ValueError("存档是空的")
        v = np.asarray(arr[done], dtype=np.float32)
        if not np.isfinite(v).all() or (np.linalg.norm(v, axis=1) < 0.5).any():
            raise ValueError("存档里的向量不完整")
        return arr, done
    except Exception as e:
        say(f"  片 {sid + 1} 的半成品存档不可用（{type(e).__name__}：{e}），本片从头做")
        return None


def build_vectors(chunks, force=False, attn="sdpa"):
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    if not MODEL_DIR.exists():
        say(f"模型目录不存在：{MODEL_DIR}")
        return None, 0.0, None, None

    n_total = len(chunks)
    n_shard_expect = (n_total + SHARD - 1) // SHARD
    SHARD_DIR.mkdir(parents=True, exist_ok=True)

    if force:
        for p in list(SHARD_DIR.glob("emb_*.npy")) + list(SHARD_DIR.glob("partial_emb_*")):
            p.unlink()
        say("--force：已清空旧分片和半成品存档")

    # —— 起跑前体检：可用提交内存 ——
    mm = mem_avail()
    if mm is None:
        say("！读不到内存信息，起跑体检跳过（跑中仍会逐批盯内存）")
        blog("体检：读不到内存信息")
    else:
        pa, ca = mm
        say(f"起跑体检：可用物理 {pa:.2f} GB ｜ 可用提交 {ca:.2f} GB")
        blog(f"体检 物理可用 {pa:.2f} GB 提交可用 {ca:.2f} GB")
        if ca < START_MIN_COMMIT_GB:
            say(f"！可用提交内存 {ca:.2f} GB 低于起跑线 {START_MIN_COMMIT_GB:.1f} GB，先不跑。")
            say("  关掉些占内存的程序后重跑（已完成的片不受影响）。")
            blog("！未起跑：提交内存低于起跑线")
            raise SystemExit(3)

    nthread = os.cpu_count() or 8
    torch.set_num_threads(nthread)
    say(f"加载模型 {MODEL_DIR}（CPU {nthread} 线程 ｜ 注意力 {attn}）")
    t0 = time.time()
    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR), padding_side="left")
    # 强制 fp32：权重文件是 bf16，但本机（12 代酷睿，无 bf16 硬件加速）会走软件模拟，
    # 实测编码慢 5.4 倍（同样 16 块：bf16 47.4 秒 vs fp32 8.8 秒）
    model = AutoModel.from_pretrained(str(MODEL_DIR), attn_implementation=attn,
                                      dtype=torch.float32)
    model.eval()
    dim = model.config.hidden_size
    say(f"模型就绪，维度 {dim}，dtype {model.dtype}，加载用时 {time.time() - t0:.1f} 秒")
    blog(f"模型就绪 dim={dim} dtype={model.dtype} 批次 {BATCH_ITEMS}块/{BATCH_CHARS}字符")

    # 待做分片：已存在且形状对的直接认
    todo = []
    for sid in range(n_shard_expect):
        p = SHARD_DIR / f"emb_{sid:05d}.npy"
        want = min((sid + 1) * SHARD, n_total) - sid * SHARD
        if p.exists():
            try:
                # 这里刻意不用 mmap_mode：Windows 上 mmap 不关，同一进程后面
                # 就写不了这个文件（踩过一次 OSError 22）。整读再放掉最稳。
                a = np.load(p)
                shp = tuple(a.shape)
                del a
                if shp == (want, dim):
                    continue
                say(f"  片 {sid + 1} 已有文件形状 {shp} ≠ 应有 {(want, dim)}，当作没做")
            except Exception:
                say(f"  片 {sid + 1} 已有文件读不出来，当作没做")
        todo.append(sid)
    if not todo:
        say("向量分片已齐，跳过编码")
        return None, 0.0, None, None
    n_done = n_shard_expect - len(todo)
    if n_done:
        say(f"断点续跑：已有 {n_done}/{n_shard_expect} 片，待做第 {[s + 1 for s in todo]} 片")

    # 抽 200 块粗估总量，只为给个 ETA 参考
    step = max(n_total // 200, 1)
    sample = chunks[::step][:200]
    est_tok = (sum(min(len(tok.encode(c["文本"], add_special_tokens=True)), MAX_LEN)
                   for c in sample) / len(sample) * n_total)
    say(f"抽查 {len(sample)} 块，粗估全量约 {est_tok / 1e4:.0f} 万 token（仅供参考）")

    todo_blocks = sum(min((s + 1) * SHARD, n_total) - s * SHARD for s in todo)
    done_blocks = 0            # 本轮已编码块数（含从半成品接上的）
    tok_total = 0.0            # 本轮真实 token 累计（不含 padding）
    # 剩余字符数：ETA 用「剩余字符 ÷ 本进程字符速度」外推。断点续跑时不会把
    # 已经做完的部分又算一遍（按全量 token 外推会失真好几小时）
    chars_left = sum(len(c["文本"]) for s in todo
                     for c in chunks[s * SHARD:min((s + 1) * SHARD, n_total)])
    chars_done = 0.0
    min_pa = min_ca = 1e9      # 本轮最低可用内存
    last_hb = 0.0
    t_run0 = time.time()

    for sid in todo:
        lo, hi = sid * SHARD, min((sid + 1) * SHARD, n_total)
        texts = [c["文本"] for c in chunks[lo:hi]]
        sha = _sha1_texts(texts)
        part_n = SHARD_DIR / f"partial_emb_{sid:05d}.npy"
        part_j = SHARD_DIR / f"partial_emb_{sid:05d}.json"

        out = np.zeros((len(texts), dim), dtype=np.float32)
        done = []
        got = _load_partial(sid, texts, dim, n_total)
        if got:
            out, done = got
            done_blocks += len(done)
            chars_left -= sum(len(texts[i]) for i in done)
            say(f"片 {sid + 1}/{n_shard_expect} 发现半成品：上次做到 {len(done):,}/{len(texts):,} 块，接着做")
            blog(f"S{sid + 1} 续跑：已有 {len(done)} 块")
        done_set = set(done)
        remaining = [i for i in range(len(texts)) if i not in done_set]
        # 片内按长度排序再组批：一批里长度接近，padding 浪费最少。
        # 结果按原下标散回，保证 vectors 行序与 chunks.jsonl 严格对齐。
        order = sorted(remaining, key=lambda i: len(texts[i]))
        batch, bchars = [], 0
        st = {"n_batch": 0, "n_blocks": len(done)}
        t_shard = time.time()
        say(f"片 {sid + 1}/{n_shard_expect} 开始 ｜ 共 {len(texts):,} 块，本轮做 {len(remaining):,} 块")
        blog(f"=== S{sid + 1} START 块区间 [{lo},{hi}) 本轮 {len(remaining)} 块 ===")

        def flush():
            nonlocal batch, bchars, done_blocks, tok_total, chars_left, chars_done
            nonlocal min_pa, min_ca, last_hb
            if not batch:
                return
            items = [texts[i] for i in batch]
            enc = tok(items, padding=True, truncation=True,
                      max_length=MAX_LEN, return_tensors="pt")
            bno = st["n_batch"] + 1
            n_real = int(enc["attention_mask"].sum().item())
            blog(f"S{sid + 1} B{bno:03d} START ｜ {len(batch)} 块 ｜ {bchars:,} 字符"
                 f" ｜ pad后 {int(enc['input_ids'].numel()):,} tok ｜ 最长 {max(len(t) for t in items):,} 字符")
            tb = time.time()
            with torch.inference_mode():
                # use_cache=False：嵌入是单次前向，用不上 KV cache。默认 True 时
                # 每批会白分配一大块（fp32 下 16 块 × 430 token 要 1.6 GB），
                # 实测会随机段错误崩溃（两次探测分别崩在 1415 / 430 token 的批上）。
                res = model(**enc, use_cache=False)
            h = getattr(res, "last_hidden_state", None)
            if h is None:
                h = res[0]
            v = h[:, -1].float()              # left padding → 最后一个位置就是真末尾；
            # 先转 fp32：权重是 bf16 时 numpy() 不支持 bf16，且归一化放 fp32 做更准
            v = torch.nn.functional.normalize(v, p=2, dim=1)
            out[batch] = v.numpy()
            done.extend(batch)
            dt = time.time() - tb
            st["n_batch"] += 1
            st["n_blocks"] += len(batch)
            done_blocks += len(batch)
            tok_total += n_real
            chars_left -= bchars
            chars_done += bchars

            # 半成品先落盘再干别的：崩在这批之后，重跑最多丢一批
            np.save(part_n, out)
            part_j.write_text(json.dumps({
                "shard": sid, "n_total": n_total, "dim": dim, "texts_sha1": sha,
                "done": done,
                "updated": datetime.datetime.now().isoformat(timespec="seconds"),
            }, ensure_ascii=False), encoding="utf-8")

            pa, ca = mem_avail() or (0.0, 0.0)
            if pa:
                min_pa = min(min_pa, pa)
            if ca:
                min_ca = min(min_ca, ca)
            blog(f"S{sid + 1} B{bno:03d} OK    ｜ {dt:.1f}s ｜ {n_real / max(dt, 1e-9):.0f} tok/s"
                 f" ｜ 物理 {pa:.2f}/提交 {ca:.2f} GB ｜ 本片 {st['n_blocks']:,}/{len(texts):,}")

            now = time.time()
            if now - last_hb >= HEARTBEAT_SECS:
                last_hb = now
                el = max(now - t_run0, 1e-9)
                # ETA 按「剩余字符 ÷ 本进程字符速度」外推：片内按长度排序、
                # 短块先做，早期任何外推都偏乐观，这个至少会随实测自我校正
                eta = el * max(chars_left, 0.0) / max(chars_done, 1.0)
                say(f"  ⌛ 片 {sid + 1}/{n_shard_expect} ｜ 批 {st['n_batch']:,}"
                    f" ｜ 本片 {st['n_blocks']:,}/{len(texts):,} 块"
                    f" ｜ 全局 {done_blocks:,}/{todo_blocks:,} 块"
                    f" ｜ {tok_total / el:.0f} tok/s ｜ 已跑 {_hms(el)} ｜ 预计还需 {_hms(eta)}"
                    f" ｜ 可用 物理 {pa:.1f}/提交 {ca:.1f} GB")

            if ca and ca < GC_COMMIT_GB:
                gc.collect()
                pa2, ca2 = mem_avail() or (0.0, 0.0)
                say(f"  ！可用提交内存 {ca:.2f} GB 偏低，强制 gc 后 {ca2:.2f} GB")
                blog(f"S{sid + 1} 低内存 gc：{ca:.2f} → {ca2:.2f} GB")
                if ca2 and ca2 < STOP_COMMIT_GB:
                    say(f"！可用提交内存 {ca2:.2f} GB 低于 {STOP_COMMIT_GB:.1f} GB，主动干净退出，"
                        "避免段错误。半成品已存盘，重跑接着做。")
                    blog("！主动退出：提交内存不足")
                    raise SystemExit(2)

            batch, bchars = [], 0

        for i in order:
            if batch and (len(batch) >= BATCH_ITEMS or bchars + len(texts[i]) > BATCH_CHARS):
                flush()
            batch.append(i)
            bchars += len(texts[i])
        flush()

        if len(done) != len(texts):
            raise RuntimeError(f"片 {sid + 1} 没编码完：{len(done)}/{len(texts)}")
        np.save(SHARD_DIR / f"emb_{sid:05d}.npy", out)
        for p in (part_n, part_j):
            if p.exists():
                p.unlink()
        shard_secs = time.time() - t_shard
        left = (time.time() - t_run0) * max(chars_left, 0.0) / max(chars_done, 1.0)
        say(f"  片 {sid + 1}/{n_shard_expect} 完成 ｜ 累计 {hi:,}/{n_total:,} 块"
            f" ｜ 本片用时 {_hms(shard_secs)} ｜ 全程预计还需 {_hms(left)}")

    if min_ca > 1e8:
        mem_txt = "未知（取不到内存信息）"
        out_min_pa = out_min_ca = None
    else:
        mem_txt = f"物理 {min_pa:.2f} / 提交 {min_ca:.2f} GB"
        out_min_pa, out_min_ca = min_pa, min_ca
    say(f"编码结束 ｜ 本轮用时 {_hms(time.time() - t_run0)} ｜ 最低可用内存 {mem_txt}")
    blog(f"=== 全部待做分片完成 ｜ 用时 {_hms(time.time() - t_run0)} ｜ 最低可用 {mem_txt} ===")
    return dim, time.time() - t_run0, out_min_pa, out_min_ca


def merge_shards(n_total, force=False):
    import numpy as np
    if VEC_FILE.exists() and not force:
        try:
            a = np.load(VEC_FILE, mmap_mode="r")
            if a.shape[0] == n_total:
                return a.shape
        except Exception:
            pass
    n_shard = (n_total + SHARD - 1) // SHARD
    parts = [np.load(SHARD_DIR / f"emb_{i:05d}.npy") for i in range(n_shard)]
    a = np.concatenate(parts, axis=0)[:n_total]
    np.save(VEC_FILE, a)
    return a.shape


# ---- BM25 索引 -----------------------------------------------------------
def build_bm25(chunks, force=False):
    from rank_bm25 import BM25Okapi
    n = len(chunks)
    if BM25_FILE.exists() and not force:
        try:
            with BM25_FILE.open("rb") as f:
                obj = pickle.load(f)
            if obj.get("n_chunks") == n:
                say(f"bm25.pkl 已是 {n:,} 块，跳过")
                return 0.0
            say(f"bm25.pkl 是 {obj.get('n_chunks')} 块，与当前 {n} 块不符，重建")
        except Exception:
            say("bm25.pkl 读不出来，重建")

    t0 = time.time()
    say(f"分词 {n:,} 块…")
    corpus = [tokenize(c["文本"]) for c in chunks]
    stats = {
        "n_chunks": n,
        "平均词数": round(sum(len(t) for t in corpus) / max(n, 1), 1),
        "空词块": sum(1 for t in corpus if not t),
    }
    say(f"  平均每块 {stats['平均词数']} 个词，空词块 {stats['空词块']} 个")
    say("建 BM25（Okapi）…")
    bm = BM25Okapi(corpus)
    with BM25_FILE.open("wb") as f:
        pickle.dump({"bm25": bm, "n_chunks": n, "stats": stats}, f)
    return time.time() - t0


# ---- 主流程 --------------------------------------------------------------
def main():
    global IDX_DIR, SHARD_DIR, VEC_FILE, BM25_FILE, META_FILE

    args = sys.argv[1:]
    force = "--force" in args
    stage = "both"
    attn = "sdpa"
    limit = 0
    idx_override = ""
    for i, a in enumerate(args):
        if a == "--stage" and i + 1 < len(args):
            stage = args[i + 1]
        elif a.startswith("--stage="):
            stage = a.split("=", 1)[1]
        elif a == "--attn" and i + 1 < len(args):
            attn = args[i + 1]
        elif a.startswith("--attn="):
            attn = a.split("=", 1)[1]
        elif a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
        elif a == "--index-dir" and i + 1 < len(args):
            idx_override = args[i + 1]

    if attn not in ("sdpa", "eager"):
        say(f"！--attn 只支持 sdpa / eager，收到：{attn}")
        return 1
    if limit and not idx_override:
        say("！--limit 是调试参数，必须同时给 --index-dir 指到临时目录，防止污染正式索引")
        return 1
    if idx_override:
        IDX_DIR = Path(idx_override)
        if not IDX_DIR.is_absolute():
            IDX_DIR = ROOT / IDX_DIR
        SHARD_DIR = IDX_DIR / "emb_shards"
        VEC_FILE = IDX_DIR / "vectors.npy"
        BM25_FILE = IDX_DIR / "bm25.pkl"
        META_FILE = IDX_DIR / "index_meta.json"

    say("=" * 72)
    say(f"建索引 ｜ 块 {CHUNKS}")
    say(f"       输出 {IDX_DIR}")
    say(f"       阶段 {stage} ｜ {'强制重算' if force else '断点续跑'}"
        f" ｜ 注意力 {attn}" + (f" ｜ 调试只做前 {limit:,} 块" if limit else ""))
    say("=" * 72)

    if not CHUNKS.exists():
        say(f"找不到 chunks.jsonl：{CHUNKS}")
        say("先跑 scripts/chunk_corpus.py")
        return 1
    chunks = load_chunks()
    if limit:
        chunks = chunks[:limit]
    say(f"读到 {len(chunks):,} 块")

    IDX_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    setup_run_logs()
    stamp = datetime.date.today().isoformat()
    meta = {}
    if META_FILE.exists():
        try:
            meta = json.loads(META_FILE.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    t_all = time.time()

    # ---- 向量 ----
    if stage in ("both", "vec"):
        say("")
        say("---- ① 向量索引（Qwen3-Embedding-0.6B，CPU）----")
        if not MODEL_DIR.exists():
            say(f"！模型目录不存在：{MODEL_DIR}")
            say("  模型下载：设 HF_ENDPOINT=https://hf-mirror.com 后从 "
                "Qwen/Qwen3-Embedding-0.6B 拉取")
        else:
            dim, secs, min_pa, min_ca = build_vectors(chunks, force, attn=attn)
            say("合并分片 → vectors.npy …")
            shape = merge_shards(len(chunks), force)
            say(f"合并完成：vectors.npy {shape[0]:,} 行 × {shape[1]} 列")
            import numpy as np
            a = np.load(VEC_FILE, mmap_mode="r")
            norms = np.linalg.norm(np.asarray(a[:2000], dtype=np.float32), axis=1)
            say(f"  抽检前 2000 行 L2 范数：最小 {norms.min():.4f} 最大 {norms.max():.4f}"
                f"（应都≈1.0）")
            meta.update({
                "向量模型": MODEL_NAME, "模型目录": str(MODEL_DIR),
                "向量维度": int(shape[1]), "向量行数": int(shape[0]),
                "池化": "last_token", "归一化": "L2",
                "查询指令前缀": QUERY_INSTRUCT,
                "编码耗时秒": round(secs, 1),
            })
            if min_ca is not None:
                meta["编码中最低可用物理GB"] = round(min_pa, 2)
                meta["编码中最低可用提交GB"] = round(min_ca, 2)

    # ---- BM25 ----
    if stage in ("both", "bm25"):
        say("")
        say("---- ② BM25 索引（jieba 分词）----")
        secs = build_bm25(chunks, force)
        meta.update({"BM25": "rank_bm25.BM25Okapi", "BM25耗时秒": round(secs, 1),
                     "分词器": "scripts/text_tokenize.py::tokenize"})

    # ---- 元信息 ----
    meta.update({
        "块数": len(chunks),
        "来源": str(CHUNKS.relative_to(ROOT)),
        "建索引日期": stamp,
        "切块参数来源": "scripts/chunk_corpus.py",
    })
    META_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 统计 ----
    def mb(p):
        return p.stat().st_size / 1024 / 1024 if p.exists() else 0.0

    shard_mb = sum(mb(p) for p in SHARD_DIR.glob("emb_*.npy"))
    total_mb = mb(VEC_FILE) + mb(BM25_FILE) + shard_mb
    say("")
    say("=" * 72)
    say("索引统计")
    say("=" * 72)
    say(f"  总块数        {len(chunks):,}")
    say(f"  向量维度      {meta.get('向量维度', '（未建）')}")
    say(f"  向量文件      vectors.npy  {mb(VEC_FILE):.2f} MB"
        f"（{meta.get('向量行数', 0):,} 行）")
    say(f"  向量分片      {len(list(SHARD_DIR.glob('emb_*.npy')))} 片，共 {shard_mb:.2f} MB")
    say(f"  BM25 文件     bm25.pkl  {mb(BM25_FILE):.2f} MB")
    say(f"  元信息        index_meta.json  {mb(META_FILE):.2f} MB")
    say(f"  ---- 索引合计 {total_mb:.2f} MB")
    say(f"  总耗时        {time.time() - t_all:.1f} 秒")

    logf = LOG_DIR / f"build_index_{stamp}.log"
    with logf.open("w", encoding="utf-8") as f:
        f.write(f"# 建索引日志 {stamp}\n")
        f.write(json.dumps(meta, ensure_ascii=False, indent=2))
        f.write(f"\n\n向量文件 {mb(VEC_FILE):.2f} MB ｜ BM25 {mb(BM25_FILE):.2f} MB "
                f"｜ 分片 {shard_mb:.2f} MB ｜ 总 {total_mb:.2f} MB\n")
    say(f"  日志 → logs/{logf.name}")
    if RUN["fault_path"] is not None:
        try:
            n_bytes = RUN["fault_path"].stat().st_size
        except Exception:
            n_bytes = -1
        if n_bytes == 0:
            fd_txt = "空文件（全程没崩）"
        elif n_bytes > 0:
            fd_txt = f"{n_bytes} 字节，非空，看一眼！"
        else:
            fd_txt = "读不到"
        say(f"  崩溃转储 logs/{RUN['fault_path'].name}：{fd_txt}")
    blog(f"=== 本轮正常结束，总用时 {time.time() - t_all:.1f} 秒 ===")
    for k in ("batch_fp", "fault_fp"):
        fp = RUN.get(k)
        if fp is not None:
            try:
                fp.flush()
                fp.close()
            except Exception:
                pass
    RUN["batch_fp"] = RUN["fault_fp"] = None
    say("")
    say(f"完成，用时 {time.time() - t_all:.1f} 秒")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:
        plain_error(e)
        _log_exc()
        raise
