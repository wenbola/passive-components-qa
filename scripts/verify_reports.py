# -*- coding: utf-8 -*-
"""
verify_reports.py —— 核验已下载的定期报告 PDF

【这个脚本能做什么】
两层核验：
① 结构层（不需要任何库）：是不是真 PDF（魔数）、字节数与 md5 是否和下载清单一致、
   页数（从 /Count 读）、PDF 内部 /Title 元数据。
② 内容层（需要 PyMuPDF）：抽正文，核对首页里是不是写着对的公司代码／简称／年份／报告类型，
   并按方法卡的阈值判断有没有文本层（不是扫描件）。

【为什么需要它】
「脚本说下载成功」不等于「下对了」。这个脚本是独立的第二双眼睛，
专门用来抓「标题对但内容是别的报告」这类错误。

【这个脚本不能做什么】
- 不核对财务数字（那是后续提取步骤的事）。
- 内容层只读**前 2 页**做身份核对，不逐页通读。
"""

import re
import sys
import hashlib
import pathlib

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TAG = "[verify_reports]"


def say(msg):
    print(f"{TAG} {msg}", flush=True)


ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "clean" / "定期报告_下载清单.csv"

# /Title 的括号内容，限制长度并排除未转义的右括号（避免跨二进制乱匹配）
TITLE_RE = re.compile(rb"/Title\s*\(([^)]{0,120})\)")
COUNT_RE = re.compile(rb"/Count\s+(\d+)")


def pdf_title(data: bytes) -> str:
    """从 PDF 元数据里取标题，试着按几种编码解出中文。"""
    m = TITLE_RE.search(data)
    if not m:
        return ""
    raw = m.group(1)
    for enc in ("utf-8", "utf-16-be", "latin-1"):
        try:
            s = raw.decode(enc)
        except Exception:
            continue
        if sum("一" <= ch <= "鿿" for ch in s) >= 2:
            return s.strip()
    return ""


def content_check(path: pathlib.Path, code6: str, name: str, period: str):
    """内容层核验：抽正文核对身份。返回 (结果字典, 问题列表)。

    依据：methods/方法卡_年报提取.md
    - 文本层优先；全文字数/页数 < 10 基本可判为扫描件

    ⚠ 为什么要搜**全文**而不是只搜封面：
    项目实测发现封面格式极不统一 ——
      法拉电子封面写「公司代码：600563 / 公司简称：法拉电子」
      但 三环集团、达利凯普、麦捷科技 等封面只写**工商全称**
      （「潮州三环（集团）股份有限公司」），既没 6 位代码也没简称。
    只查封面会把 40 份正确文件误报成问题。所以身份核对放宽到全文，
    封面信息单独记录下来当参考，不作为通过与否的依据。
    """
    problems = []
    try:
        import pymupdf
    except ImportError:
        return None, ["未安装 PyMuPDF，跳过内容层核验"]

    doc = pymupdf.open(path)
    npages = doc.page_count
    total = 0
    per_page = []
    for i in range(npages):
        n = len(doc[i].get_text("text"))
        total += n
        per_page.append(n)

    # 封面（前 2 页）：只记录，不判定
    head = "".join(doc[i].get_text("text") for i in range(min(2, npages)))
    # 全文：用来做身份核对
    full = "".join(doc[i].get_text("text") for i in range(npages))
    doc.close()

    density = total / npages if npages else 0

    year = period[:4]
    is_annual = "半年度" not in period
    type_str = "年度报告" if is_annual else "半年度报告"

    r = {
        "提取字数": total,
        "字/页": round(density),
        "有文本层": density >= 10,
        "全文含代码": code6 in full,
        "全文含简称": name in full,
        "封面含代码": code6 in head,
        "封面含简称": name in head,
        "封面含年份": year in head,
        "封面含类型": type_str in head,
    }

    if density < 10:
        problems.append(f"疑似扫描件（{total} 字 / {npages} 页 = {density:.1f} 字/页）")
    # 身份：全文里必须能找到代码和简称，否则可能是下错了公司的报告
    if code6 not in full:
        problems.append(f"全文找不到股票代码 {code6}")
    if name not in full:
        problems.append(f"全文找不到公司简称「{name}」")
    if year not in full:
        problems.append(f"全文找不到年份 {year}")
    if type_str not in full:
        problems.append(f"全文找不到「{type_str}」")

    return r, problems


def main():
    if not MANIFEST.exists():
        say(f"找不到下载清单：{MANIFEST}")
        return 1

    m = pd.read_csv(MANIFEST)
    say(f"核验 {len(m)} 份，清单：{MANIFEST.relative_to(ROOT)}")

    rows = []
    problems = []
    for _, r in m.iterrows():
        # 清单里存的是相对项目根目录的路径，先补成绝对路径，
        # 否则从 scripts/ 跑会解析成 scripts/data/... 全部报「文件不存在」
        p = pathlib.Path(r["本地文件"])
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            problems.append(f"{r['公司名']} {r['报告期']}：文件不存在")
            continue
        b = p.read_bytes()

        # 1) 魔数
        if not b.startswith(b"%PDF"):
            problems.append(f"{r['公司名']} {r['报告期']}：不是 PDF")

        # 2) 字节数 + md5 与清单一致
        if len(b) != r["字节数"]:
            problems.append(f"{r['公司名']} {r['报告期']}："
                            f"字节数不符 {len(b)} vs {r['字节数']}")
        if hashlib.md5(b).hexdigest() != r["md5"]:
            problems.append(f"{r['公司名']} {r['报告期']}：md5 不符")

        pages = [int(x) for x in COUNT_RE.findall(b)]
        rows.append({
            "股票代码": r["股票代码"], "公司名": r["公司名"], "报告期": r["报告期"],
            "页数": max(pages) if pages else 0,
            "MB": round(len(b) / 1024 / 1024, 1),
            "PDF内标题": pdf_title(b),
        })

    # ---- 内容层核验 ----
    say("")
    say("内容层核验（抽全文，核对公司代码／简称／年份／报告类型）…")
    say("    注：60 份逐份抽全文，比较慢，正常。")
    content_rows = []
    content_problems = []
    for _, r in m.iterrows():
        p = pathlib.Path(r["本地文件"])
        if not p.is_absolute():
            p = ROOT / p
        if not p.exists():
            continue
        code6 = str(r["股票代码"]).split(".")[0]
        cres, cprob = content_check(p, code6, str(r["公司名"]), str(r["报告期"]))
        if cres is None:
            content_problems.append(f"{r['公司名']} {r['报告期']}：{cprob[0]}")
            continue
        cres.update({"股票代码": r["股票代码"], "公司名": r["公司名"],
                     "报告期": r["报告期"]})
        content_rows.append(cres)
        for x in cprob:
            content_problems.append(f"{r['公司名']} {r['报告期']}：{x}")

    if content_rows:
        c = pd.DataFrame(content_rows)
        cols = ["股票代码", "公司名", "报告期", "提取字数", "字/页", "有文本层",
                "全文含代码", "全文含简称", "封面含代码", "封面含简称"]
        with pd.option_context("display.max_colwidth", 40, "display.width", 220):
            say(c[cols].to_string(index=False))

        say("")
        say(f"文本层判定：{int(c['有文本层'].sum())}/{len(c)} 份有文本层"
            f"（字/页 中位 {int(c['字/页'].median())}）")
        say("身份核对（判定用，全文搜索）：")
        for col, label in [("全文含代码", "股票代码"), ("全文含简称", "公司简称")]:
            say(f"    全文含{label}：{int(c[col].sum())}/{len(c)}")
        say("封面信息（仅参考，不作为判定依据）：")
        for col, label in [("封面含代码", "股票代码"), ("封面含简称", "公司简称"),
                           ("封面含年份", "年份"), ("封面含类型", "报告类型")]:
            say(f"    封面含{label}：{int(c[col].sum())}/{len(c)}")

    d = pd.DataFrame(rows)
    with pd.option_context("display.max_colwidth", 40, "display.width", 200):
        say("")
        say("结构层核验：")
        say(d.to_string(index=False))

    say("")
    say(f"页数 min/中位/max：{d['页数'].min()} / {int(d['页数'].median())} / {d['页数'].max()}")

    tiny = d[d["页数"] < 20]
    if len(tiny):
        say(f"⚠ 页数异常少（<20 页）的 {len(tiny)} 份：")
        for _, r in tiny.iterrows():
            say(f"    {r['公司名']} {r['报告期']}：{r['页数']} 页")
    else:
        say("页数全部正常（无 <20 页的，不像下载中断或错误页）。")

    # 3) 页数排序一致性：同公司内 年报 应比 半年报 厚
    say("")
    say("同一年内『年报应比半年报厚』检查（只比 2025 年，因为 2026 年还没有年报）：")
    say("    注：这条只是启发式，不是定律 —— 版面松紧、附件多寡都会影响页数。")
    bad = []
    for code, g in d.groupby("股票代码"):
        # 注意：必须用 (?<!半) 排除「半年度报告」。
        # 直接 str.contains("年度报告") 会把「半年度报告」当成年度报告（子串包含），
        # 导致每家都被误报成「年报比半年报薄」。
        ann = g[g["报告期"].str.contains(r"(?<!半)年度报告", regex=True)
                & g["报告期"].str.startswith("2025")]
        half = g[g["报告期"].str.contains("2025年半年度报告", regex=False)]
        if len(ann) and len(half):
            a, h = int(ann["页数"].iloc[0]), int(half["页数"].iloc[0])
            if a <= h:
                bad.append(f"{g['公司名'].iloc[0]}：2025年报 {a} 页 ≤ "
                           f"2025半年报 {h} 页")
    if bad:
        for x in bad:
            say(f"    ⚠ {x}")
        say(f"    （{len(bad)} 家。若只是几家且差距小，通常是版面差异；"
            f"若差很多或某家特别突兀，值得翻一下 PDF 确认没下错文件）")
    else:
        say("    全部符合（2025 年报都比 2025 半年报厚）。")

    have = d[d["PDF内标题"] != ""]
    say("")
    say(f"元数据里能读出中文标题的：{len(have)}/{len(d)} 份")
    if len(have):
        ok = sum(str(r["报告期"])[:4] in r["PDF内标题"] for _, r in have.iterrows())
        say(f"  其中标题里的年份与报告期一致的：{ok}/{len(have)}")

    say("")
    allbad = [("结构", x) for x in problems] + [("内容", x) for x in content_problems]
    if allbad:
        say(f"===== 发现 {len(allbad)} 个问题 =====")
        for kind, x in allbad:
            say(f"    [{kind}] {x}")
        return 1

    say("===== 核验全部通过 =====")
    say(f"    结构层：{len(d)} 份，文件完整性 / 页数 / 元数据均无异常")
    if content_rows:
        c = pd.DataFrame(content_rows)
        say(f"    内容层：{len(c)} 份，全文都能找到对应股票代码、公司简称、年份、报告类型；"
            f"全部有文本层（字/页 中位 {int(c['字/页'].median())}），不是扫描件")
        say(f"    （封面格式各家不同，只有 {int(c['封面含代码'].sum())}/{len(c)} 份"
            f"封面上写了股票代码，只有 {int(c['封面含简称'].sum())}/{len(c)} 份写了简称 —— "
            f"所以身份核对用的是全文搜索，不是封面）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
