# -*- coding: utf-8 -*-
"""
chunk_corpus.py —— 把清洗后的 15 份 2025 年报切成检索块

【这个脚本能做什么】
把 data/clean/text/ 里的 md 切成一块块可以直接喂给向量模型和 BM25 的文本。

切块规则（按重要性排序）：

1. **一块只属于一页。** 绝不跨页。这样每块都能精确说出「哪家公司、哪份报告、
   第几页」，落实方法卡铁律「每个数字都要能说出出处」。代价是页边的小块多，
   但页码可归因比块大小重要得多。

2. **表格整表不切。** 财务表格是一个语义整体：把「营业收入」那行从表头切走，
   那块就失去了「这是哪张表、单位是什么、哪一列是哪一年」的全部信息，
   检索出来也没法用。所以 ≤3000 字的表整块保留（实测 4,956 个表里 99.4% 都在这以内）。
   更大的表才按行切，**每一片都重复表头行和分隔行**，标题带「（续表 n/m）」。
   表头怎么认：**第一条 `|---|` 分隔行的前一行**才是表头 —— 实测只有 15.1% 的表
   分隔行在第 1 行，其余前面还黏着「释义」「单位：元」这类行。
   万一整块里找不到分隔行，那就**一行都不重复**：宁可续表缺表头，
   也不能把一行数据复制进每一片、被当成多条记录读。

3. **单位行和表标题黏在表上。** 「单位：元」「合并资产负债表」这类短行
   紧挨着表格，切走就等于把数字的口径弄丢了。所以表格块会往回吸收
   同一页里它前面 ≤300 字的短文本，一起成块。

4. **长段落按句子切。** 单个段落超过 900 字时，在 900 字附近最近的句号／分号处
   断开，并保留 100 字重叠，避免一句话被拦腰截断后两边都检索不到。

5. **每块带一个面包屑前缀**（公司、报告期、页码、章节），
   形如 `【风华高科·2025年报·第45页·第三节 管理层讨论与分析】`。
   作用有两个：一是让「□适用√不适用」这种本身没意义的短块变得可检索，
   二是下游大模型拿到块时一眼知道这是谁家的哪一页。

【产出】
  data/clean/chunks/chunks.jsonl        一行一个块（向量的行序与它严格对齐）
  logs/chunk_<日期>.log                 逐份进度
  logs/chunk_summary_<日期>.csv         每份的块数 / 字符数 / 耗时

【断点续跑】
  按**文件**续跑：chunks.jsonl 里已经出现过的文件整份跳过，只补没做的。
  jsonl 是追加写的，所以中断时已写完的行不会丢。
  `--force` 忽略已完成的，全部重来（会先备份旧的 jsonl）。

【这个脚本不能做什么】
- 不改语料：只读 data/clean/text/，一个字都不回写。
- 不丢页码：每块都记物理页和印刷页码。
- 不猜章节：识别不出章节标题就留空，不硬套。

【用法】
  python scripts/chunk_corpus.py            # 正式跑（断点续跑）
  python scripts/chunk_corpus.py --force    # 全部重来
  python scripts/chunk_corpus.py --stats    # 只统计已有 jsonl，不重新切
"""

import csv
import json
import re
import sys
import time
import shutil
import datetime
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TAG = "[chunk]"


def say(msg):
    print(f"{TAG} {msg}", flush=True)


def plain_error(exc):
    say(f"出错：{type(exc).__name__}")
    say(f"原始信息：{exc}")


# ---- 配置 ----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
TEXT_DIR = ROOT / "data" / "clean" / "text"
OUT_DIR = ROOT / "data" / "clean" / "chunks"
OUT_JSONL = OUT_DIR / "chunks.jsonl"
LOG_DIR = ROOT / "logs"

TARGET_TEXT = 600        # 文本块目标字数
MAX_TEXT = 900           # 单个段落超过它就按句子切开
OVERLAP = 100            # 长段落切开时的重叠字数
TABLE_ATOMIC_MAX = 3000  # 表格整块保留的上限（实测 4,956 个表里只有 32 个超）
TABLE_PART_MAX = 1500    # 超大表切开后每片的上限
GLUE_PREFIX_MAX = 300    # 表格往回吸收的前缀上限
GLUE_TOTAL_MAX = 2500    # 黏前缀后整块上限

RE_MARK = re.compile(r"^\[页码:\s*(\d+)\s*(?:\|\s*印刷页码:\s*(\d+)\s*)?\]\s*$")
RE_FILE = re.compile(r"^(\d{6}\.[A-Z]{2})_([^_]+)_(.+)\.md$")
RE_SEC1 = re.compile(r"^第[一二三四五六七八九十]+节\s*\S")
RE_SEC2 = re.compile(r"^[一二三四五六七八九十]+、\s*\S")
RE_SEC3 = re.compile(r"^（[一二三四五六七八九十]+）\s*\S")


# ---- 块切分 --------------------------------------------------------------
def split_long_text(text, cap=MAX_TEXT, overlap=OVERLAP):
    """长文本按句末标点切开，保留 overlap 字重叠。"""
    parts, start, n = [], 0, len(text)
    while start < n:
        if n - start <= cap:
            parts.append(text[start:])
            break
        end = start + cap
        best = -1
        for m in re.finditer(r"[。；！？]", text[start:end]):
            best = start + m.end()          # 最后一个句末标点之后
        if best <= start:
            best = end                       # 没有标点，硬切
        parts.append(text[start:best])
        start = max(best - overlap, start + 1)
    return [p for p in parts if p.strip()]


RE_TBL_SEP = re.compile(r"[\|\-\s:]+")


def split_table(rows, cap=TABLE_PART_MAX):
    """超大表按行切，每片重复表头 + 分隔行。

    表头定位：**第一条 |---| 分隔行**，它的**前一行**才是表头行。
    实测只有 15.1% 的表分隔行在第 1 行，其余前面还黏着标题／说明行
    （「释义」「单位：元」），所以不能假设 rows[0] 就是表头。
    找不到分隔行时**一行都不重复** —— 宁可续表缺表头，
    也不能把一行数据复制到每一片里被误读成多条记录。
    前缀（标题／单位行）只放第一片，那是指代整张表的上下文，不是表格数据。
    """
    si = next((i for i, r in enumerate(rows) if RE_TBL_SEP.fullmatch(r or "")), None)
    if si is None:
        head, prefix, body = [], [], rows
    elif si == 0:
        head, prefix, body = rows[:1], [], rows[1:]
    else:
        head, prefix, body = rows[si - 1:si + 1], rows[:si - 1], rows[si + 1:]
    hlen = sum(len(h) + 1 for h in head)
    parts, cur, curlen = [], [], hlen
    for r in body:
        if cur and curlen + len(r) + 1 > cap:
            parts.append(list(cur))
            cur, curlen = [], hlen
        cur.append(r)
        curlen += len(r) + 1
    if cur:
        parts.append(list(cur))
    if not parts:
        parts = [[]]
    parts[0] = prefix + head + parts[0]
    return [head + p if i else p for i, p in enumerate(parts)]


def parse_page_blocks(lines, start, end):
    """把一页的行切成块序列：[(类型, 内容)]，类型是 'text' 或 'table'。"""
    body = [l for l in lines[start:end] if l.strip() and l.strip() != "---"]
    blocks, cur, cur_tbl = [], [], None
    for l in body:
        is_tbl = l.lstrip().startswith("|")
        if cur_tbl is not None and is_tbl != cur_tbl:
            blocks.append(("table" if cur_tbl else "text", cur))
            cur = []
        cur_tbl = is_tbl
        cur.append(l)
    if cur:
        blocks.append(("table" if cur_tbl else "text", cur))
    return blocks


def build_chunks_for_page(blocks, base):
    """把一页的块序列组装成检索块。base 是公共元数据 dict。"""
    chunks = []

    def emit(kind, text, note=""):
        t = text.strip()
        if not t:
            return
        crumb = base["面包屑"] + (f"·{note}" if note else "")
        chunks.append({**base, "类型": kind, "序号": len(chunks) + 1,
                       "文本": f"{crumb}\n{t}", "正文": t,
                       "字符数": len(t), "_note": note})

    buf, buflen = [], 0

    def flush():
        nonlocal buf, buflen
        if buf:
            emit("text", "\n".join(buf))
            buf, buflen = [], 0

    for kind, rows in blocks:
        if kind == "text":
            for para in split_long_text("\n".join(rows)):
                n = len(para)
                if buf and buflen + n > TARGET_TEXT:
                    flush()
                buf.append(para)
                buflen += n + 1
            continue

        # 表格
        blob = "\n".join(rows)
        n = len(blob)
        if n <= TABLE_ATOMIC_MAX:
            if buflen and buflen <= GLUE_PREFIX_MAX and buflen + n <= GLUE_TOTAL_MAX:
                flush_with_table = "\n".join(buf) + "\n" + blob   # 单位/标题黏上
                buf, buflen = [], 0
                emit("table", flush_with_table)
            else:
                flush()
                emit("table", blob)
        else:
            flush()
            parts = split_table(rows)
            for i, pt in enumerate(parts, 1):
                emit("table", "\n".join(pt), note=f"续表{i}/{len(parts)}")
    flush()
    return chunks


# ---- 主流程 --------------------------------------------------------------
def load_done(path):
    """读已有 jsonl：返回 {文件名: 块数} 和总行数。"""
    done, total = Counter(), 0
    if not path.exists():
        return done, 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            done[o.get("文件", "?")] += 1
            total += 1
    return done, total


def chunk_one(p, sec_state):
    """切一份，返回 (chunks, 统计)。sec_state 跨页保存章节标题。"""
    raw = p.read_text(encoding="utf-8")
    lines = raw.splitlines()
    m = RE_FILE.match(p.name)
    if not m:
        raise ValueError(f"{p.name}：文件名不符合「代码_公司_报告期.md」")
    code, name, period = m.group(1), m.group(2), m.group(3)

    marks = []
    for i, l in enumerate(lines):
        mm = RE_MARK.match(l.strip())
        if mm:
            marks.append((i, int(mm.group(1)),
                          int(mm.group(2)) if mm.group(2) else None))
    if not marks:
        raise ValueError(f"{p.name}：没有 [页码:] 标记")

    chunks, stat = [], Counter(页数=len(marks))
    for k, (mi, phys, printed) in enumerate(marks):
        e = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        blocks = parse_page_blocks(lines, mi + 1, e)

        # 章节标题状态：只认非表格行、且是短行
        for kind, rows in blocks:
            if kind != "text":
                continue
            for l in rows:
                t = l.strip()
                if len(t) > 40:
                    continue
                if RE_SEC1.match(t):
                    sec_state["节"], sec_state["小节"] = t, ""
                elif RE_SEC2.match(t):
                    sec_state["小节"] = t
        sec = "·".join(x for x in (sec_state["节"], sec_state["小节"]) if x)

        base = {
            "文件": p.name, "股票代码": code, "公司": name, "报告期": period,
            "物理页": phys, "印刷页码": printed,
            "章节": sec,
            "面包屑": f"【{name}·2025年报·第{phys}页"
                      f"{'（印刷页 ' + str(printed) + '）' if printed else ''}"
                      f"{'·' + sec if sec else ''}】",
        }
        chunks.extend(build_chunks_for_page(blocks, base))

    stat["块数"] = len(chunks)
    stat["表块"] = sum(1 for c in chunks if c["类型"] == "table")
    stat["字符"] = sum(c["字符数"] for c in chunks)
    return chunks, stat


def main():
    args = set(sys.argv[1:])
    force = "--force" in args
    stats_only = "--stats" in args

    say("=" * 72)
    say(f"切块 ｜ 语料 {TEXT_DIR}")
    say(f"     输出 {OUT_JSONL}")
    say("=" * 72)

    if stats_only:
        done, total = load_done(OUT_JSONL)
        say(f"已有 {total:,} 块，覆盖 {len(done)} 份文件")
        for k, v in sorted(done.items()):
            say(f"  {k[:40]:42s} {v:>5,} 块")
        return 0

    files = sorted(TEXT_DIR.glob("*.md"))
    if not files:
        say(f"语料目录里没有 md：{TEXT_DIR}")
        return 1
    say(f"语料 {len(files)} 份")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if force and OUT_JSONL.exists():
        bak = OUT_JSONL.with_suffix(f".jsonl.bak{int(time.time())}")
        shutil.move(str(OUT_JSONL), str(bak))
        say(f"--force：旧 jsonl 已备份为 {bak.name}")

    done, total = load_done(OUT_JSONL)
    if done:
        say(f"断点续跑：已有 {total:,} 块／{len(done)} 份文件，这些整份跳过")
    todo = [p for p in files if p.name not in done]
    say(f"本次要切 {len(todo)} 份")

    stamp = datetime.date.today().isoformat()
    t0 = time.time()
    rows = []
    with OUT_JSONL.open("a", encoding="utf-8") as f:
        for p in todo:
            t1 = time.time()
            sec_state = {"节": "", "小节": ""}
            chunks, st = chunk_one(p, sec_state)
            for c in chunks:
                c.pop("_note", None)
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
            f.flush()                       # 每份切完就落盘，中断不丢
            dt = round(time.time() - t1, 2)
            rows.append((p.name, st["页数"], st["块数"], st["表块"],
                         st["块数"] - st["表块"], st["字符"], dt))
            say(f"  {p.name[:32]:34s} {st['页数']:>4}页 → {st['块数']:>5,} 块"
                f"（表 {st['表块']:>4,}）{st['字符']:>8,} 字  {dt:>5.2f}s")

    done, total = load_done(OUT_JSONL)
    elapsed = time.time() - t0
    sz = OUT_JSONL.stat().st_size / 1024 / 1024

    say("")
    say("=" * 72)
    say("合计")
    say("=" * 72)
    say(f"  本次切 {len(rows)} 份，用时 {elapsed:.1f} 秒")
    say(f"  jsonl 共 {total:,} 块 ｜ 覆盖 {len(done)} 份文件 ｜ 文件大小 {sz:.2f} MB")
    if rows:
        say(f"  本次新切 {sum(r[2] for r in rows):,} 块"
            f"（表格块 {sum(r[3] for r in rows):,}）"
            f" ｜ 正文 {sum(r[5] for r in rows):,} 字")

    if rows and not stats_only:
        logf = LOG_DIR / f"chunk_{stamp}.log"
        with logf.open("w", encoding="utf-8") as f:
            f.write(f"# 切块日志 {stamp}\n# 参数 TARGET_TEXT={TARGET_TEXT} "
                    f"MAX_TEXT={MAX_TEXT} OVERLAP={OVERLAP} "
                    f"TABLE_ATOMIC_MAX={TABLE_ATOMIC_MAX} "
                    f"TABLE_PART_MAX={TABLE_PART_MAX}\n\n")
            for r in rows:
                f.write(f"{r[0]}\t页数 {r[1]}\t块 {r[2]}\t表块 {r[3]}\t"
                        f"文本块 {r[4]}\t字符 {r[5]}\t{r[6]}s\n")
        say(f"  日志 → logs/{logf.name}")
        csvf = LOG_DIR / f"chunk_summary_{stamp}.csv"
        with csvf.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["文件", "页数", "块数", "表格块", "文本块", "字符数", "耗时秒"])
            w.writerows(rows)
        say(f"  统计 → logs/{csvf.name}")

    say("")
    say(f"完成，用时 {elapsed:.1f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
