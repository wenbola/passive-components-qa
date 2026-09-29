# -*- coding: utf-8 -*-
r"""
docx_to_md.py —— .docx → .md 转换（本机没装 pandoc，用 python-docx 自己转）

【这个脚本能干什么】
两种模式，自动识别：

A. 「Word 里粘的就是 markdown 文本」（如 outputs\逐题测试记录.docx）：
   每个段落 = 一行 markdown，空行 = 空段落。直接按段落抽出、
   把连续多个空行并成一个，原样还原。

B. 「普通 Word 文档」：
   按正文顺序（段落、表格穿插）重建 markdown：
   标题样式（Heading 1 / 标题 1）→ # ## ###；
   项目符号 / 编号 → "- " / "1. "（多级按缩进）；
   加粗片段 → **加粗**；表格 → GFM 管道表。

【用法】
  python scripts\docx_to_md.py "outputs\逐题测试记录.docx"
  python scripts\docx_to_md.py 输入.docx --out 输出.md    # 默认与输入同目录、同名 .md

【产出】
  与输入同目录同名的 .md（除非 --out 指定）。只读 docx，不改原文件。

【限制】
  不处理图片 / 公式 / 脚注；文档里有图片会在终端提醒（本次不导出）。
"""

import argparse
import re
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TAG = "[docx2md]"


def say(msg):
    print(f"{TAG} {msg}", flush=True)


def plain_error(exc):
    say(f"出错：{type(exc).__name__}")
    say(f"原始信息：{exc}")


MD_MARKERS = ("**", "##", "|---")


def looks_like_markdown(paras):
    """模式判定：足够多的段落行本身就带 markdown 记号。"""
    nonempty = [x.strip() for x in paras if x.strip()]
    if not nonempty:
        return False
    hit = sum(1 for x in nonempty
              if any(m in x for m in MD_MARKERS) or x == "---")
    return hit / len(nonempty) >= 0.25


def convert_md_text(paras):
    """模式 A：段落本来就是 markdown 行，保留结构、压缩连续空行。"""
    out, prev_blank = [], False
    for x in paras:
        t = x.rstrip()
        if not t:
            if prev_blank:
                continue
            prev_blank = True
            out.append("")
        else:
            prev_blank = False
            out.append(t)
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n"


def para_text_with_bold(par):
    if "**" in par.text:
        return par.text          # 已经是 markdown，别再套加粗
    parts = []
    for r in par.runs:
        t = r.text
        if not t:
            continue
        parts.append(f"**{t}**" if r.bold else t)
    return "".join(parts) or par.text


RE_HEAD = re.compile(r"^(?:Heading|标题)\s*(\d*)$")


def para_prefix(par):
    """返回 (kind, prefix)；kind: 'head' / 'li' / 'p'。"""
    name = (par.style.name or "").strip()
    m = RE_HEAD.match(name)
    if m:
        lvl = int(m.group(1)) if m.group(1) else 1
        return "head", "#" * min(max(lvl, 1), 6) + " "

    from docx.oxml.ns import qn
    numpr = par._p.find(".//" + qn("w:numPr"))
    is_list = ("List" in name) or ("列表" in name) or (numpr is not None)
    if is_list:
        ilvl = 0
        if numpr is not None:
            el = numpr.find(qn("w:ilvl"))
            if el is not None:
                try:
                    ilvl = int(el.get(qn("w:val")))
                except (TypeError, ValueError):
                    ilvl = 0
        ordered = ("Number" in name) or ("编号" in name)
        # 编号一律写 "1. "：markdown 渲染器会自动递增，序号反而不会乱
        return "li", "  " * ilvl + ("1. " if ordered else "- ")
    return "p", ""


def table_to_md(tbl):
    rows = []
    for row in tbl.rows:
        cells = []
        for c in row.cells:
            t = c.text.strip().replace("|", "\\|").replace("\n", "<br>")
            cells.append(t)
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(head) + " |",
             "|" + "|".join(["---"] * width) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def convert_plain_doc(doc):
    """模式 B：普通 Word 文档，按正文顺序重建。"""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    items = []   # (kind, text)
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            par = Paragraph(child, doc)
            text = para_text_with_bold(par).strip()
            if not text:
                continue
            kind, prefix = para_prefix(par)
            items.append((kind, prefix + text))
        elif child.tag == qn("w:tbl"):
            tbl = Table(child, doc)
            items.append(("table", table_to_md(tbl)))

    blocks, current = [], None
    for kind, text in items:
        if kind == "li":
            if current is None:
                current = [text]
                blocks.append(current)
            else:
                current.append(text)
        else:
            current = None
            blocks.append([text])
    return "\n\n".join("\n".join(b) for b in blocks) + "\n"


def convert(src, out=None):
    src = Path(src)
    out = Path(out) if out else src.with_suffix(".md")
    t0 = time.time()
    say(f"输入：{src.name}（{src.stat().st_size:,} bytes）")

    import docx
    doc = docx.Document(str(src))

    paras = [p.text for p in doc.paragraphs]
    n_par, n_blank = len(paras), sum(1 for x in paras if not x.strip())
    images = len(doc.inline_shapes)
    say(f"段落 {n_par}（空 {n_blank}）｜表格 {len(doc.tables)}｜图片 {images}")

    if looks_like_markdown(paras):
        say("识别为：Word 里粘的 markdown 文本（模式 A，原样还原）")
        md = convert_md_text(paras)
    else:
        say("识别为：普通 Word 文档（模式 B，按样式重建）")
        md = convert_plain_doc(doc)

    out.write_text(md, encoding="utf-8", newline="\n")
    say(f"输出：{out.name}（{out.stat().st_size:,} bytes，"
        f"{md.count(chr(10))} 行，用时 {time.time() - t0:.2f}s）")
    if images:
        say(f"提醒：文档里有 {images} 张图片，本次没有导出。")
    return out


def main():
    ap = argparse.ArgumentParser(description="docx → md（两种模式自动识别）")
    ap.add_argument("src", help="输入 .docx")
    ap.add_argument("--out", default=None, help="输出 .md（默认与输入同目录同名）")
    args = ap.parse_args()
    try:
        convert(args.src, args.out)
    except Exception as e:
        plain_error(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
