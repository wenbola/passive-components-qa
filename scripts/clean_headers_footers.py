# -*- coding: utf-8 -*-
"""
clean_headers_footers.py —— 删掉每页重复的页眉和页脚孤立页码

【这个脚本能做什么】
年报 PDF 每页顶部印着「XXX公司2025 年年度报告全文」，底部印着孤立的页码。
这两样每页都出现、对检索和问答毫无价值，纯粹是噪音，这里把它们删掉。

判定规则（先按位置、再按内容，两条都要满足才删）：
- **页眉**：页块里**第一个非空行**，且整行匹配「…20XX 年年度报告(全文)」。
  位置限定在块首，内容限定成这种短行 —— 两个条件叠加，正文里提到
  「详见公司 2025 年年度报告全文」这类句子不会被误删（它不在块首，也不以报告名结尾）。
- **页脚**：页块里**最后一个非空行**，两种格式都认：
  - 孤立数字：`36` —— 深市 10 份都是这种（实测 1,915 页）。
  - 「页码 / 总页数」：`50 / 213` —— 上交所 5 份是这种（实测 1,030 页）。
    这种额外要求分母**正好等于该文件的总页数**，等于白送一个交叉校验。
  数字还要**等于该页的印刷页码**（标记里没记印刷页码时，用「物理页 − 该文件统一偏移量」推）。
  单行数字在正文里是存在的（商络电子第 126 页就有一行「1」，顺络电子有页以
  「2026 年2 月」结尾），所以「位置在块尾 + 数值对得上页码」两个条件缺一不可。
  两个条件都不满足就**只记录不删**。

【顺带补一件事】
有 1,143 页的页脚明明印着页码，但原提取脚本没认出来、标记里只有 `[页码: N]`。
这些页的页码信息本来只存在于那行页脚里，直接删就丢了。
所以删的同时把它补进标记：`[页码: N]` → `[页码: N | 印刷页码: M]`。
用户要求「页码信息保留在标记里」，这就是落实。

【产出】
  data/clean/text/<股票代码>_<公司名>_<报告期>.md    清洗后的语料（切块和索引的输入）
  logs/clean_hf_<日期>.log                          逐份进度 + 所有告警
  logs/clean_hf_summary_<日期>.csv                  每份的页数 / 删页眉 / 删页脚 / 字符数
  logs/clean_hf_removed_<日期>.csv                  **每一行被删掉的原文**（可逐条复核）

【输入】
  data/clean/text_raw/   提取脚本的原样输出，本脚本只读不动，作为回溯依据。

【断点续跑】
  输出文件已存在、且比输入新、且非空 → 跳过（`--force` 强制全部重跑）。

【这个脚本不能做什么】
- 不动表格：`|---|---|` 分隔行、`<br>`、转义竖线一概不碰。
- 不碰 `---`：它是提取脚本插的**页块分隔符**（实测每份正好等于页数），不是 PDF 里的噪音。
- 不删块尾对不上页码的数字行 —— 只记日志报出来，宁可留着。

【用法】
  python scripts/clean_headers_footers.py             # 正式跑
  python scripts/clean_headers_footers.py --dry-run   # 只统计，不写文件
  python scripts/clean_headers_footers.py --force     # 忽略已完成，全部重跑
"""

import csv
import re
import sys
import time
import datetime
from collections import Counter
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TAG = "[clean_hf]"


def say(msg):
    print(f"{TAG} {msg}", flush=True)


def plain_error(exc):
    say(f"出错：{type(exc).__name__}")
    say(f"原始信息：{exc}")


# ---- 配置 ----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "data" / "clean" / "text_raw"
DST_DIR = ROOT / "data" / "clean" / "text"
LOG_DIR = ROOT / "logs"

RE_MARK = re.compile(r"^\[页码:\s*(\d+)\s*(?:\|\s*印刷页码:\s*(\d+)\s*)?\]\s*$")
RE_BARE = re.compile(r"^\d{1,4}$")                      # 页脚格式一：孤立数字 36
RE_SLASH = re.compile(r"^(\d{1,4})\s*/\s*(\d{1,4})$")    # 页脚格式二：50 / 213
RE_HEADER = re.compile(r"^.{2,40}?20\d\d\s*年年度报告(全文)?$")   # 页眉短行
SEP = "---"                                             # 页块分隔符，不动


def find_pages(lines):
    """返回 [(标记行号, 物理页, 印刷页码或None)]"""
    out = []
    for i, l in enumerate(lines):
        m = RE_MARK.match(l.strip())
        if m:
            out.append((i, int(m.group(1)), int(m.group(2)) if m.group(2) else None))
    return out


def block_of(lines, marks, k):
    """第 k 页的正文行索引区间（不含标记行本身）"""
    start = marks[k][0] + 1
    end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
    return start, end


def file_offset(marks):
    """该文件的统一偏移量：物理页 − 印刷页码。只认出现最多的那个值。"""
    offs = Counter(p - pr for _, p, pr in marks if pr is not None)
    if not offs:
        return None, None
    top, cnt = offs.most_common(1)[0]
    odd = {v: c for v, c in offs.items() if v != top}
    return top, (odd if odd else None)


def clean_one(path, dry_run):
    """清洗一份，返回统计 dict。dry_run 时不写文件。"""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    marks = find_pages(lines)
    if not marks:
        raise ValueError(f"{path.name}：一个 [页码:] 标记都没有，不是提取脚本的输出？")

    off, odd_offsets = file_offset(marks)
    stat = {
        "file": path.name, "页数": len(marks), "偏移量": "" if off is None else off,
        "删页眉": 0, "删页脚": 0, "补印刷页码": 0, "保留可疑行": 0,
        "_fmt": Counter(),
    }
    drop = set()              # 要删的行号
    removed = []              # (物理页, 类型, 原文)
    marker_fix = {}           # 行号 -> 新的标记行
    hdr_texts = Counter()     # 本文件出现过的页眉文本
    suspect = []              # 保留的可疑行

    for k, (mi, phys, printed) in enumerate(marks):
        s, e = block_of(lines, marks, k)
        body = [(j, lines[j]) for j in range(s, e) if lines[j].strip()]
        content = [(j, t) for j, t in body if t.strip() != SEP]
        if not content:
            continue

        # ① 页眉：块首非空行
        hj, ht = content[0]
        if RE_HEADER.match(ht.strip()):
            drop.add(hj)
            stat["删页眉"] += 1
            hdr_texts[ht.strip()] += 1
            removed.append((phys, "页眉", ht))

        # ② 页脚：块尾非空行，两种格式，且数值等于该页印刷页码
        fj, ft = content[-1]
        if fj in drop:            # 整页只有页眉一行，没有别的了
            continue
        v = ft.strip()
        val = fmt = None
        if RE_BARE.match(v):
            val, fmt = int(v), "裸数字"
            is_footer = (printed is not None and val == printed) \
                or (off is not None and val == phys - off)
        else:
            m2 = RE_SLASH.match(v)
            if m2 and int(m2.group(2)) == len(marks):
                # 格式二：分母正好等于本文件总页数，白送的交叉校验，不通过就不认
                val, fmt = int(m2.group(1)), "页码/总页数"
                is_footer = (printed is not None and val == printed) \
                    or (val == phys - (off or 0))
            else:
                is_footer = False
        if is_footer:
            drop.add(fj)
            stat["删页脚"] += 1
            stat["_fmt"][fmt] += 1
            removed.append((phys, "页脚", ft))
            if printed is None:
                # 页码信息只在页脚里，删之前补进标记，别丢
                marker_fix[mi] = f"[页码: {phys} | 印刷页码: {val}]"
                stat["补印刷页码"] += 1
        elif val is not None or (RE_SLASH.match(v) and len(v) < 20):
            stat["保留可疑行"] += 1
            suspect.append((phys, ft))

    # ---- 生成输出 --------------------------------------------------------
    out = []
    for i, l in enumerate(lines):
        if i in drop:
            continue
        out.append(marker_fix.get(i, l))

    # 在头部的「- 工具：」行后插入清洗说明，保留可追溯性
    stamp = datetime.date.today().isoformat()
    note = (f"- 清洗：{stamp} 删除页眉 {stat['删页眉']} 行、页脚 {stat['删页脚']} 行"
            f"（其中 {stat['补印刷页码']} 页的印刷页码由页脚补进标记）；"
            f"脚本 scripts/clean_headers_footers.py")
    for i, l in enumerate(out[:20]):
        if l.startswith("- 工具："):
            out.insert(i + 1, note)
            break
    else:
        say(f"  ！{path.name}：没找到「- 工具：」行，清洗说明没插进去")

    # 字符账目（必须严格对得上，见下面的恒等式校验）
    # 注意单位统一成**非空白字符**：新标记 `[页码: 50 | 印刷页码: 50]` 比旧的
    # `[页码: 50]` 多 3 个空格，空格不计入，按 raw 长度算会凭空多出 3 字/页。
    nw = lambda s: len(re.sub(r"\s+", "", s))
    stat["删字符"] = sum(len(lines[j].strip()) for j in drop)          # 含行内空格
    stat["删非空白"] = sum(nw(lines[j]) for j in drop)
    stat["补标记字符"] = sum(nw(v) - nw(lines[k]) for k, v in marker_fix.items())
    stat["插说明字符"] = nw(note)
    stat["原字符"] = nw(raw)
    stat["新字符"] = nw("\n".join(out))

    if not dry_run:
        DST_DIR.mkdir(parents=True, exist_ok=True)
        (DST_DIR / path.name).write_text("\n".join(out) + "\n", encoding="utf-8")

    stat["_hdr_texts"] = hdr_texts
    stat["_removed"] = removed
    stat["_suspect"] = suspect
    stat["_odd_offsets"] = odd_offsets
    return stat


def main():
    args = set(sys.argv[1:])
    dry_run = "--dry-run" in args
    force = "--force" in args

    say("=" * 72)
    say(f"删页眉页脚 ｜ 输入 {SRC_DIR}")
    say(f"            输出 {DST_DIR}")
    say(f"            模式 {'干跑（不写文件）' if dry_run else ('强制全跑' if force else '断点续跑')}")
    say("=" * 72)

    if not SRC_DIR.exists():
        say(f"输入目录不存在：{SRC_DIR}")
        say("提示：原样提取的 md 应当在 text_raw/，清洗结果写进 text/。")
        return 1

    files = sorted(SRC_DIR.glob("*.md"))
    if not files:
        say("输入目录里没有 .md 文件")
        return 1
    say(f"找到 {len(files)} 份 md")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    t0 = time.time()

    stats, skipped = [], []
    for p in files:
        dst = DST_DIR / p.name
        if not dry_run and not force and dst.exists() and dst.stat().st_size > 0 \
                and dst.stat().st_mtime > p.stat().st_mtime:
            skipped.append(p.name)
            say(f"  跳过（已完成）{p.name}")
            continue
        t1 = time.time()
        try:
            st = clean_one(p, dry_run)
        except Exception as exc:
            plain_error(exc)
            raise
        st["耗时"] = round(time.time() - t1, 2)
        stats.append(st)
        say(f"  {st['file'][:34]:36s} {st['页数']:>4}页 "
            f"删页眉{st['删页眉']:>4} 删页脚{st['删页脚']:>4} "
            f"补页码{st['补印刷页码']:>3} 留疑{st['保留可疑行']:>2} "
            f"{st['耗时']:>5.2f}s")

    if skipped:
        say(f"本次跳过 {len(skipped)} 份（已完成）")

    # ---- 汇总 ------------------------------------------------------------
    tot = Counter()
    for st in stats:
        for k in ("页数", "删页眉", "删页脚", "补印刷页码", "保留可疑行",
                  "删字符", "删非空白", "补标记字符", "插说明字符",
                  "原字符", "新字符"):
            tot[k] += st[k]
    elapsed = time.time() - t0

    say("")
    say("=" * 72)
    say("合计")
    say("=" * 72)
    say(f"  处理 {len(stats)} 份 ｜ 跳过 {len(skipped)} 份 ｜ 用时 {elapsed:.1f} 秒")
    say(f"  页数 {tot['页数']:,} 页")
    say(f"  删页眉 {tot['删页眉']:,} 行 ｜ 删页脚 {tot['删页脚']:,} 行 "
        f"｜ 其中补印刷页码 {tot['补印刷页码']:,} 页")
    fmt = Counter()
    for st in stats:
        fmt.update(st["_fmt"])
    say(f"  页脚格式：{' ｜ '.join(f'{k} {v:,} 行' for k, v in fmt.most_common())}")
    say(f"  块尾对不上页码、保留未删的数字行：{tot['保留可疑行']:,} 行")
    lost = tot["原字符"] - tot["新字符"]
    expect = tot["删非空白"] - tot["补标记字符"] - tot["插说明字符"]
    say(f"  字符（去空白）{tot['原字符']:,} → {tot['新字符']:,}，净减 {lost:,} 字")
    say(f"    账目：删除的行 {tot['删非空白']:,} 字 − 补进标记 {tot['补标记字符']:,} 字"
        f"（{tot['补印刷页码']:,} 页标记变长）− 插入的清洗说明 {tot['插说明字符']:,} 字"
        f" = {expect:,}")
    say(f"    恒等式校验：{'✔ 完全对上 → 正文一个字没动' if lost == expect else f'！对不上，差 {lost - expect} 字，停下来查'}")
    # 页眉是否一家一种、有没有意外形状
    say("")
    say("  各文件页眉文本种类数（应当每家只有 1 种）：")
    multi = 0
    for st in stats:
        n = len(st["_hdr_texts"])
        if n != 1:
            multi += 1
            say(f"    ！{st['file']}：{n} 种 → {dict(st['_hdr_texts'])}")
    if not multi:
        say("    15 份全部只有 1 种页眉 ✔")
    odd = [st["file"] for st in stats if st["_odd_offsets"]]
    if odd:
        say(f"  ！偏移量（物理页−印刷页）不唯一的文件：{odd}")
    else:
        say(f"  偏移量（物理页−印刷页）逐份唯一 ✔："
            f"{ {st['file'][:8]: st['偏移量'] for st in stats} }")
    if tot["保留可疑行"]:
        say(f"  ！有 {tot['保留可疑行']} 行块尾数字对不上页码，已保留，明细见日志")

    # ---- 落盘 ------------------------------------------------------------
    if not dry_run:
        logf = LOG_DIR / f"clean_hf_{stamp}.log"
        with logf.open("w", encoding="utf-8") as f:
            f.write(f"# 删页眉页脚日志 {stamp}\n")
            f.write(f"# 输入 {SRC_DIR}\n# 输出 {DST_DIR}\n\n")
            for st in stats:
                f.write(f"## {st['file']}\n")
                f.write(f"   页数 {st['页数']} ｜ 删页眉 {st['删页眉']} ｜ 删页脚 {st['删页脚']}"
                        f" ｜ 补印刷页码 {st['补印刷页码']} ｜ 留疑 {st['保留可疑行']}"
                        f" ｜ 用时 {st['耗时']}s\n")
                for t, c in st["_hdr_texts"].most_common():
                    f.write(f"   页眉：{c} 次 | {t}\n")
                if st["_suspect"]:
                    f.write(f"   块尾保留的可疑数字行（{len(st['_suspect'])} 行）：\n")
                    for phys, s in st["_suspect"][:50]:
                        f.write(f"     物理页 {phys}: {s!r}\n")
                f.write("\n")
        say(f"  日志 → logs/{logf.name}")

        csvf = LOG_DIR / f"clean_hf_summary_{stamp}.csv"
        with csvf.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["文件", "页数", "偏移量", "删页眉行", "删页脚行", "补印刷页码",
                        "保留可疑行", "原字符", "新字符", "删字符", "耗时秒"])
            for st in stats:
                w.writerow([st["file"], st["页数"], st["偏移量"], st["删页眉"],
                            st["删页脚"], st["补印刷页码"], st["保留可疑行"],
                            st["原字符"], st["新字符"], st["删字符"], st["耗时"]])
        say(f"  统计 → logs/{csvf.name}")

        remf = LOG_DIR / f"clean_hf_removed_{stamp}.csv"
        with remf.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["文件", "物理页", "类型", "被删原文"])
            for st in stats:
                for phys, kind, text in st["_removed"]:
                    w.writerow([st["file"], phys, kind, text])
        say(f"  被删原文 → logs/{remf.name}（{sum(len(s['_removed']) for s in stats):,} 行，可逐条复核）")

    say("")
    say(f"完成，用时 {elapsed:.1f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
