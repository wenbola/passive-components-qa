# -*- coding: utf-8 -*-
"""
text_tokenize.py —— BM25 用的中文分词，建索引和查询**共用这一个**。

为什么单独抽一个文件：BM25 靠词面重合打分，
如果建索引时切一套词、查询时切另一套，分数会静默错掉（不报错，只是结果变烂）。
所以两边 import 同一个 tokenize()，不许各写一份。

分词策略：
- jieba 精确模式切中文。财务专有名词（营业收入、归属于上市公司股东的净利润）jieba 词表里
  没有，会被切碎，但**两边切法一致**，BM25 依然能匹配，只是粒度细一点。
- **数字、百分比、日期整体保留**：财务问答大量围绕具体数字，
  「23,194,230.30」被切碎就查不到了。所以先按正则把数字串抠出来当整词，再切其余部分。
- 丢掉纯标点和空白（`|`、`---`、`<br>` 这些表格残留不该进词表）。
"""

import re

import jieba

# 数字／日期／百分比：先整段抠出来，保证不被 jieba 切碎
RE_NUM = re.compile(r"\d[\d,\.]*%?")
# 保留含中文、字母或数字的 token，丢掉纯符号
RE_KEEP = re.compile(r"[一-鿿A-Za-z0-9]")


def tokenize(text):
    """切词，返回小写 token 列表。建索引和查询都走这个函数。"""
    if not text:
        return []
    toks = []
    # 先把数字抠出来单独处理，剩下的中文/英文再交给 jieba
    pos = 0
    for m in RE_NUM.finditer(text):
        for t in jieba.cut(text[pos:m.start()]):
            t = t.strip().lower()
            if t and RE_KEEP.search(t):
                toks.append(t)
        toks.append(m.group(0).lower())
        pos = m.end()
    for t in jieba.cut(text[pos:]):
        t = t.strip().lower()
        if t and RE_KEEP.search(t):
            toks.append(t)
    return toks


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    demo = "2025 年营业收入 23,194,230.30 元，同比增长 12.5%。单位：元"
    print(demo)
    print(tokenize(demo))
