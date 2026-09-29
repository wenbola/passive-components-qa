# -*- coding: utf-8 -*-
"""
extract_pdf_text.py —— 把定期报告 PDF 的正文与表格提取成 Markdown（文本层，不走 OCR）

【这个脚本能做什么】
逐页提取 PDF 的正文和表格：
- 每页开头写 [页码: N]，N 是 PDF **物理页序号**（第 1 页 = 第 1 页，1 起）。
  页脚上印的页码能认出来时附在后面：`[页码: 11 | 印刷页码: 10]`。
  两个都要，是因为它们的用途不同：物理页序号在阅读器里能直接跳，
  印刷页码能和报告自己的目录对上（方法卡要求「每个数字都要能说出出处」）。
- 表格尽量按行列还原成 Markdown 表格，**插回它在页面上的原位置**。
- 提取失败的页写进日志，并且在 md 里留下痕迹（不静默跳过）。
- 表格还原存疑的（只有 1 列、空单元格过半、单元格过长等）**只记录，不硬修**。

【产出】
  data/clean/text_raw/<股票代码>_<公司名>_<报告期>.md  每份 PDF 一个（原样提取，回溯依据）
      —— text_raw/ 不是语料目录：clean_headers_footers.py 读它、去掉页眉页脚后
         写进 data/clean/text/，那才是切块与建索引认的语料目录。
  logs/extract_text_<日期>.log                        逐份进度 + 失败页 / 存疑表格明细
  logs/extract_text_summary_<日期>.csv                每份的页数 / 字数 / 表格数 / 耗时
  logs/extract_text_tables_suspect_<日期>.csv         存疑表格清单（哪一页、什么原因）

【这个脚本不能做什么】
- 不做 OCR、不调视觉模型 —— 方法卡流程里的第②③步是后续步骤，这里只做第①步文本层提取。
- **不提取任何财务指标数值**，也不判断数字对不对，只负责把文字和表格原样搬出来。
- 不「硬修」表格：列对不齐照原样出 + 记录，不猜、不补。

【依据】methods/方法卡_年报提取.md 第三节流程第①步
"""

import csv
import re
import sys
import time
import logging
import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TAG = "[extract_pdf_text]"


def say(msg):
    print(f"{TAG} {msg}", flush=True)


def plain_error(exc):
    say(f"出错：{type(exc).__name__}")
    say(f"原始信息：{exc}")


# ---- 配置 ----------------------------------------------------------------

TODAY = datetime.date.today().isoformat()

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "clean" / "定期报告_下载清单.csv"
# 两段式：本脚本只做**原样提取**，输出到 text_raw/（回溯依据）；
# clean_headers_footers.py 读 text_raw/、删掉页眉页脚后写进 text/（语料目录，
# 切块与建索引只认 text/）。
OUT_DIR = ROOT / "data" / "clean" / "text_raw"
LOG_DIR = ROOT / "logs"

# 语料范围（2026-09-29 定）：data/clean/text/ 是**给后续切块和建索引用**的语料目录，
# 只放这 15 份 2025 年年度报告。其余报告期（2024 年报、2025 半年报、2026 半年报）
# 的 PDF 原件留在 data/raw/ 里不动，也不要提取进 text_raw/ —— 清洗后照样会混进语料，
# 被切块和索引一起吃下去。
CORPUS_PERIOD = "2025年年度报告"

# 判定阈值（依据方法卡：全文/页数 < 10 基本可判为扫描件）
MIN_CHARS_PER_PAGE = 10      # 单页正文少于此值 → 记「疑似扫描件/纯图页」
EMPTY_CELL_RATIO = 0.40      # 表格空单元格占比超过此值 → 记「还原存疑」
LONG_CELL_CHARS = 200        # 单元格超过这么多字 → 多半把正文误判成了表格
TABLE_EDGE_TOP = 0.10        # 页眉区：页面上方 10%
TABLE_EDGE_BOTTOM = 0.90     # 页脚区：页面下方 10%


# ---- 工具函数 ------------------------------------------------------------

def cell_text(v):
    """把表格单元格变成能放进 Markdown 表格的一格。

    两个必须处理的字符（否则整个表会被撑坏）：
    - 换行 → <br>：Markdown 表格的一行必须在同一行上，
      直接留换行会让表格断成两截。用 <br> 是 GFM 的通用写法。
    - 竖线 | → \\| ：竖线是 Markdown 表格的列分隔符，不转义会把一格切成两格。
    """
    if v is None:
        return ""
    s = str(v).replace("\r", "").replace("\n", "<br>").replace("|", "\\|")
    return s.strip()


def rows_to_markdown(rows):
    """把 extract() 出来的二维表转成 Markdown 表格。

    第一行固定当表头（Markdown 表格语法要求有表头行，没有也得占一行）。
    """
    ncol = max(len(r) for r in rows) if rows else 0
    lines = []
    for i, r in enumerate(rows):
        cells = [cell_text(c) for c in r]
        cells += [""] * (ncol - len(cells))     # 补齐，免得缺格
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + "---|" * ncol)
    return "\n".join(lines)


def table_suspects(t, rows):
    """判断这个表还原得可不可信，返回存疑原因列表（空列表 = 没发现问题）。

    只做「看起来就不对」的判断，不做「数字对不对」的判断 —— 后者要回原图核对。
    """
    reasons = []
    nrow = len(rows)
    ncol = max((len(r) for r in rows), default=0)

    if ncol < 2:
        reasons.append(f"只有 {ncol} 列（多半是误检，不是真表格）")
    if nrow < 2:
        reasons.append(f"只有 {nrow} 行")
    if len({len(r) for r in rows}) > 1:
        reasons.append("各行格数不齐")

    total = sum(len(r) for r in rows)
    empty = sum(1 for r in rows for c in r if not cell_text(c))
    if total and empty / total > EMPTY_CELL_RATIO:
        reasons.append(f"空单元格 {empty}/{total}（{empty / total:.0%}）")

    longest = max((len(cell_text(c)) for r in rows for c in r), default=0)
    if longest > LONG_CELL_CHARS:
        reasons.append(f"有单元格长 {longest} 字（可能把正文误判成表格）")

    # 注意：这里**不**检查「有没有识别到表头」。
    # 实测发现 PyMuPDF 的 table.header_rows 在这批年报里几乎恒为 0
    # （风华高科 2024 年报 325 个表 100% 触发），当成判据等于给每个表都打标记，
    # 反而把「只有 1 行」「单元格过长」这些真信号淹没了。

    return reasons


def norm_text(s):
    """归一化文本，用来判断「这段文字是不是已经出现在表格里了」。

    去掉所有空白和我们自己加的 <br>，只比字符内容本身。
    注意这是**内容比对**，不是位置比对 —— 位置比对（重叠面积）会丢字，见下面说明。
    """
    return re.sub(r"\s+", "", s.replace("<br>", ""))


def area_overlap_ratio(bb, tbbox):
    """文字块和表格框的重叠面积占比。只用来判断「这块离表格近不近」。"""
    x0, y0 = max(bb[0], tbbox[0]), max(bb[1], tbbox[1])
    x1, y1 = min(bb[2], tbbox[2]), min(bb[3], tbbox[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return ((x1 - x0) * (y1 - y0)) / max((bb[2] - bb[0]) * (bb[3] - bb[1]), 1e-6)


def block_text(b):
    """把一个文字块拼回纯文本（按行拼，行内按 span 拼）。"""
    return "\n".join(
        "".join(sp["text"] for sp in ln["spans"]) for ln in b.get("lines", [])
    )


def printed_page_no(blocks, page_h):
    """从页眉/页脚里认印刷页码。认不出返回 None。

    只认「整块内容就是纯数字」且位于页面上下边缘的块 ——
    宁可认不出，也不要从正文里抓个数字当页码（那会给出错误的出处）。
    """
    for b in blocks:
        if b["type"] != 0:       # 只看文字块
            continue
        y0, y1 = b["bbox"][1], b["bbox"][3]
        if y1 < page_h * TABLE_EDGE_TOP or y0 > page_h * TABLE_EDGE_BOTTOM:
            t = block_text(b).strip()
            if t.isdigit() and 1 <= int(t) <= 5000:
                return int(t)
    return None


# ---- 单页提取 ------------------------------------------------------------

def render_page(page, page_no):
    """提取一页，返回 (文本, 统计字典, 存疑列表)。

    版面顺序：把文字块和表格按「纵坐标、横坐标」排回页面上原本的先后，
    这样读起来才是报告原本的次序，而不是「正文全在前、表格全在后」。

    ⚠ 正文和表格会重叠，必须去重，但**去重只能按内容，不能按位置**。
    一开始的做法是「文字块和表格框重叠面积 ≥50% 就整块跳过」，
    实测这**会丢字**：文字块常有一截伸到表格框外，整块跳过时那一截就没了 ——
    15 份年报里实测丢了 21 个财务数字，全是这么丢的。
    现在的做法：以**行**为单位，只有这一行的文字确实已经出现在表格里才跳过。
    代价是偶尔重复（正文行也出现在表格里），但**重复好过丢失**。
    """
    blocks = page.get_text("dict")["blocks"]
    tables = page.find_tables().tables

    # 「字数」按**这一页总共抽出来多少内容**算（正文 + 表格），
    # 因为「薄页」要判的是「这页有没有提取出东西」——
    # 只数正文会把整页是财务报表的页面误判成扫描件（那种页正文本来就寥寥几行）。
    stats = {"表格": 0, "存疑": 0, "字数": 0}
    suspects = []
    elements = []

    # 这一页所有表格单元格原文拼成的「指纹」，后面用来判断
    # 某个正文行是不是已经被表格收进去了。
    fingerprint = ""

    for t in tables:
        try:
            rows = t.extract()
        except Exception as exc:
            suspects.append({"页": page_no, "表格序号": stats["表格"] + 1,
                             "行列": "-", "原因": f"表格解析失败：{type(exc).__name__}"})
            stats["存疑"] += 1
            continue
        if not rows:
            continue
        stats["表格"] += 1
        fingerprint += norm_text("".join(str(c) for r in rows for c in r if c))
        md = rows_to_markdown(rows)
        stats["字数"] += len(md)
        reasons = table_suspects(t, rows)
        if reasons:
            stats["存疑"] += 1
            suspects.append({"页": page_no, "表格序号": stats["表格"],
                             "行列": f"{len(rows)}行×{max(len(r) for r in rows)}列",
                             "原因": "；".join(reasons)})
            # 存疑标记写进 md，但用 HTML 注释：渲染时看不见，源码里查得到
            md = f"<!-- 表格还原存疑：{'；'.join(reasons)} -->\n{md}"
        elements.append((t.bbox[1], t.bbox[0], "表", md, t.bbox))

    for b in blocks:
        if b["type"] != 0:                  # 只看文字块（图片不提取）
            continue
        # 只有「贴着表格」的块才做内容去重。离表格远的正文块一律保留 ——
        # 免得正文里一句恰好和某个表格单元格同名的短句被误删。
        near_table = any(area_overlap_ratio(b["bbox"], t.bbox) >= 0.3
                         for t in tables)
        kept = []
        for ln in b.get("lines", []):
            lt = "".join(sp["text"] for sp in ln["spans"])
            if not lt.strip():
                continue
            if near_table and norm_text(lt) in fingerprint:
                continue                     # 已在表格里 → 跳过，免得重复
            kept.append(lt.strip())
        if not kept:
            continue
        txt = "\n".join(kept)
        stats["字数"] += len(txt)
        elements.append((b["bbox"][1], b["bbox"][0], "文", txt, b["bbox"]))

    elements.sort(key=lambda e: (round(e[0], 1), round(e[1], 1)))

    pno = printed_page_no(blocks, page.rect.height)
    head = f"[页码: {page_no}]" if pno is None else f"[页码: {page_no} | 印刷页码: {pno}]"

    body = "\n\n".join(e[3] for e in elements)
    return f"{head}\n\n{body}".rstrip(), stats, suspects


# ---- 单份提取 ------------------------------------------------------------

def extract_one(pymupdf, pdf_path, out_stem, code, name, period):
    """提取一份 PDF，写 md。返回 (统计字典, 存疑列表, 失败页列表)。"""
    doc = pymupdf.open(pdf_path)
    if doc.needs_pass:
        doc.close()
        raise RuntimeError("PDF 有密码，打不开")

    npages = doc.page_count
    parts = []
    nchars = 0
    ntab = 0
    nsusp = 0
    failed_pages = []
    suspects = []
    thin_pages = []

    for i in range(npages):
        page_no = i + 1
        try:
            text, stats, susp = render_page(doc[i], page_no)
        except Exception as exc:
            failed_pages.append({"页": page_no, "原因": f"{type(exc).__name__}: {exc}"})
            parts.append(f"[页码: {page_no}]\n\n（本页提取失败：{type(exc).__name__}，"
                         f"详见日志）")
            continue
        parts.append(text)
        nchars += stats["字数"]
        ntab += stats["表格"]
        nsusp += stats["存疑"]
        suspects.extend(susp)
        if stats["字数"] < MIN_CHARS_PER_PAGE:
            thin_pages.append(page_no)

    doc.close()

    body = "\n\n---\n\n".join(parts)
    header = (
        f"# {code} {name} {period}\n\n"
        f"- 来源 PDF：`data/raw/{out_stem.rsplit('_', 1)[0]}/{period}.pdf`\n"
        f"- 提取时间：{datetime.datetime.now():%Y-%m-%d %H:%M}\n"
        f"- 页数：{npages} ｜ 提取字数：{nchars:,} ｜ 表格：{ntab} 个（存疑 {nsusp} 个）"
        f" ｜ 失败页：{len(failed_pages)}\n"
        f"- 工具：PyMuPDF {pymupdf.__version__}（文本层提取，未走 OCR）\n\n"
        f"> 页码为 PDF 物理页序号（1 起）。`印刷页码` 是报告页脚上印的页码，"
        f"能和报告目录对上；两者差几页属正常（封面、目录常不印页码）。\n\n---\n\n"
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{out_stem}.md").write_text(header + body + "\n", encoding="utf-8")

    return {
        "页数": npages, "提取字数": nchars, "表格": ntab, "存疑表格": nsusp,
        "失败页": len(failed_pages), "薄页": thin_pages,
    }, suspects, failed_pages


# ---- 主流程 --------------------------------------------------------------

def main():
    import argparse
    import pymupdf

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / f"extract_text_{TODAY}.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    log = logging.getLogger(TAG)

    if not MANIFEST.exists():
        say(f"找不到下载清单：{MANIFEST}")
        return 1

    with open(MANIFEST, encoding="utf-8-sig") as f:
        records = list(csv.DictReader(f))

    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--codes", default="",
                    help="只跑这些股票代码，逗号分隔（按 6 位前缀比对）。不填则跑全部")
    ap.add_argument("--period", default="",
                    help="只跑报告期里含这个字符串的，如「2024年年度报告」。不填则跑全部")
    ap.add_argument("--show-pages", type=int, default=0,
                    help="跑完在终端打印前 N 页内容，用来肉眼验提取质量（默认 0 = 不打印）")
    args = ap.parse_args()

    if args.codes:
        want = {s.strip() for s in args.codes.split(",") if s.strip()}
        records = [r for r in records
                   if r["股票代码"].strip().split(".")[0] in want]
        say(f"（--codes 过滤后 {len(records)} 份）")
    if args.period:
        records = [r for r in records if args.period in r["报告期"]]
        say(f"（--period 过滤后 {len(records)} 份）")
    if not records:
        say("⚠ 过滤后一份都没有，检查 --codes / --period 写对没有")
        return 1

    # 语料只该放 2025 年报。选了范围外的报告期不是错，但要说清楚 ——
    # 它们写进 text_raw/ 后会被清洗带进语料目录 text/，和切块/索引用到的语料
    # 混在一起，而且是静默混。
    outside = [r for r in records if r["报告期"].strip() != CORPUS_PERIOD]
    if outside:
        say(f"⚠ 选中的 {len(outside)} 份不在语料范围内（语料范围 = {CORPUS_PERIOD}）。"
            f"它们会写进原样提取目录 {OUT_DIR.relative_to(ROOT)}，清洗后进入语料 text/，")
        say(f"  以后切块和建索引会把这些一起吃进去。如果只是想临时看看，"
            f"跑完记得清掉。")

    say(f"开始（{TODAY}）｜共 {len(records)} 份 PDF ｜ PyMuPDF {pymupdf.__version__}")
    say(f"输出目录：{OUT_DIR.relative_to(ROOT)}")
    log.info("=== 开始：%d 份 ===", len(records))

    t_start = time.time()
    summary = []
    suspect_all = []
    n_ok = 0
    n_fail = 0
    tot_pages = tot_chars = tot_tab = tot_susp = tot_failpage = 0

    for idx, r in enumerate(records, 1):
        code = r["股票代码"].strip()
        name = r["公司名"].strip()
        period = r["报告期"].strip()
        out_stem = f"{code}_{name}_{period}"
        pdf = Path(r["本地文件"])
        if not pdf.is_absolute():
            pdf = ROOT / pdf

        label = f"{code} {name} {period}"
        if not pdf.exists():
            say(f"[{idx}/{len(records)}] {label}  ✗ 文件不存在：{pdf}")
            log.error("文件不存在：%s", pdf)
            n_fail += 1
            summary.append({"股票代码": code, "公司名": name, "报告期": period,
                            "页数": 0, "提取字数": 0, "表格": 0, "存疑表格": 0,
                            "失败页": 0, "薄页数": 0, "耗时秒": 0,
                            "输出文件": "", "状态": "失败：文件不存在"})
            continue

        t0 = time.time()
        try:
            st, susp, failed = extract_one(pymupdf, pdf, out_stem, code, name, period)
        except Exception as exc:
            say(f"[{idx}/{len(records)}] {label}  ✗ 提取失败：{type(exc).__name__}: {exc}")
            log.error("提取失败 %s：%s", label, exc)
            n_fail += 1
            summary.append({"股票代码": code, "公司名": name, "报告期": period,
                            "页数": 0, "提取字数": 0, "表格": 0, "存疑表格": 0,
                            "失败页": 0, "薄页数": 0, "耗时秒": round(time.time() - t0, 1),
                            "输出文件": "", "状态": f"失败：{type(exc).__name__}"})
            continue

        dt = time.time() - t0
        n_ok += 1
        tot_pages += st["页数"]
        tot_chars += st["提取字数"]
        tot_tab += st["表格"]
        tot_susp += st["存疑表格"]
        tot_failpage += st["失败页"]

        say(f"[{idx}/{len(records)}] {label}  ✓ {st['页数']} 页 / "
            f"{st['提取字数']:,} 字 / {st['表格']} 表（存疑 {st['存疑表格']}）"
            f" / 失败页 {st['失败页']} / {dt:.1f}s")
        log.info("%s | %d页 %d字 %d表 存疑%d 失败页%d | %.1fs",
                 label, st["页数"], st["提取字数"], st["表格"],
                 st["存疑表格"], st["失败页"], dt)

        for p in failed:
            log.warning("失败页 %s 第 %d 页：%s", label, p["页"], p["原因"])
        for p in st["薄页"]:
            log.warning("薄页 %s 第 %d 页：正文不足 %d 字（疑似扫描件/纯图页）",
                        label, p, MIN_CHARS_PER_PAGE)
        for s in susp:
            s2 = {"股票代码": code, "公司名": name, "报告期": period}
            s2.update(s)
            suspect_all.append(s2)

        summary.append({
            "股票代码": code, "公司名": name, "报告期": period,
            "页数": st["页数"], "提取字数": st["提取字数"], "表格": st["表格"],
            "存疑表格": st["存疑表格"], "失败页": st["失败页"],
            "薄页数": len(st["薄页"]), "耗时秒": round(dt, 1),
            "输出文件": f"data/clean/text/{out_stem}.md", "状态": "成功",
        })

    # ---- 落地：汇总 CSV + 存疑表格 CSV ----
    sum_path = LOG_DIR / f"extract_text_summary_{TODAY}.csv"
    with open(sum_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)

    sus_path = None
    if suspect_all:
        sus_path = LOG_DIR / f"extract_text_tables_suspect_{TODAY}.csv"
        with open(sus_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["股票代码", "公司名", "报告期",
                                              "页", "表格序号", "行列", "原因"])
            w.writeheader()
            w.writerows(suspect_all)
    else:
        # 本次没有存疑表格时，把上一次留下的同名文件删掉 ——
        # 否则会留下一份对不上本次结果的旧清单
        stale = LOG_DIR / f"extract_text_tables_suspect_{TODAY}.csv"
        if stale.exists():
            stale.unlink()
            say(f"（本次没有存疑表格，已删掉上一次留下的 {stale.name}）")

    # ---- 统计 ----
    dt_all = time.time() - t_start
    say("")
    say("===== 提取完成 =====")
    say(f"    份数：成功 {n_ok} 份 ／ 失败 {n_fail} 份（共 {len(records)} 份）")
    say(f"    总页数：{tot_pages:,} 页")
    say(f"    总字数：{tot_chars:,} 字")
    say(f"    表格：{tot_tab:,} 个，其中还原存疑 {tot_susp:,} 个")
    say(f"    提取失败的页：{tot_failpage} 页")
    say(f"    用时：{dt_all / 60:.1f} 分钟")
    say(f"    输出：{OUT_DIR.relative_to(ROOT)}")
    say(f"    汇总：{sum_path.relative_to(ROOT)}")
    if sus_path:
        say(f"    存疑表格清单：{sus_path.relative_to(ROOT)}")
    say(f"    运行日志：{(LOG_DIR / f'extract_text_{TODAY}.log').relative_to(ROOT)}")
    log.info("=== 完成：成功 %d 失败 %d，共 %d 页 %d 字 ===",
             n_ok, n_fail, tot_pages, tot_chars)

    # ---- 打印前 N 页，肉眼验质量 ----
    if args.show_pages > 0:
        shown = 0
        for r in records:
            if shown >= 3:
                say("（--show-pages 最多打印前 3 份，其余省略）")
                break
            out_stem = f"{r['股票代码'].strip()}_{r['公司名'].strip()}_{r['报告期'].strip()}"
            p = OUT_DIR / f"{out_stem}.md"
            if not p.exists():
                continue
            shown += 1
            lines = p.read_text(encoding="utf-8").splitlines()
            say("")
            say("=" * 72)
            say(f"【{out_stem}.md 前 {args.show_pages} 页】")
            say("=" * 72)
            seen = 0
            for ln in lines:
                if ln.startswith("[页码:"):
                    seen += 1
                    if seen > args.show_pages:
                        break
                print(ln)

    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
