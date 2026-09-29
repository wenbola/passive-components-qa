# -*- coding: utf-8 -*-
"""
fetch_reports.py —— 批量下载选定公司的定期报告（年报 / 半年报）全文 PDF

【这个脚本能做什么】
读 data/clean/研究对象_选定清单.csv 里的公司，去东方财富公告接口找出
指定的定期报告，拿到 PDF 直链，按公司分文件夹下载到 data/raw/ 下。

【默认下哪几期】
2024年年度报告、2025年半年度报告、2025年年度报告、2026年半年度报告
（改下面的 WANT_REPORTS 即可调整，格式 (年, 类型)）。

【这个脚本不能做什么】
- 不下摘要、不下英文版、不下「半年度财务报告」（那是另一份文件，不是全文）。
  这些被排除的条目会记进 logs/ 的 skipped 文件里，方便回头核对。
- 不解析 PDF 内容，只下载。提取财务数据是后续步骤的事。
- 不做任何重命名或改写，下下来是什么就是什么。

【接口】
东方财富公告列表 np-anotice-stock.eastmoney.com/api/security/ann
  f_node=1 表示「定期报告」分类，能大幅减少翻页。
PDF 直链在 np-cnotice-stock.eastmoney.com/api/content/ann 的 attach_url 字段。

【坑（实测）】
标题前缀不统一：风华高科是「风华高科:2025年年度报告」（带英文冒号），
法拉电子是「法拉电子2025年年度报告」（**不带冒号**）。
所以匹配必须从标题**尾部**做，不能按冒号切前缀 —— 按冒号切会让法拉电子 0 命中。

【怎么跑】
cd scripts && python fetch_reports.py
可反复跑：已存在且字节数一致的文件会跳过。
"""

import sys
import csv
import time
import hashlib
import logging
import datetime
import re
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TAG = "[fetch_reports]"


def say(msg):
    print(f"{TAG} {msg}", flush=True)


ERROR_HINTS = {
    "SSLError": "SSL 证书校验失败（本地根证书问题，不是对方挂了）",
    "ConnectionError": "网络连不上（断网，或对方拒绝连接）",
    "Timeout": "对方太久没响应（超时）",
    "HTTPError": "对方返回了错误状态码（多半是被限流或封了）",
    "JSONDecodeError": "对方返回的不是 JSON（接口改版或被拦截了）",
}


def plain_error(exc):
    name = type(exc).__name__
    hint = ERROR_HINTS.get(name)
    say(f"出错：{hint}" if hint else f"出错：{name}")
    say(f"原始信息：{exc}")


# ---- 配置 ----------------------------------------------------------------

TODAY = datetime.date.today().isoformat()

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
LOG_DIR = ROOT / "logs"
SELECTION = ROOT / "data" / "clean" / "研究对象_选定清单.csv"

# 要哪几期：(年份, 类型)。类型只能是「年度报告」或「半年度报告」。
WANT_REPORTS = [
    ("2024", "年度报告"),
    ("2025", "半年度报告"),
    ("2025", "年度报告"),
    ("2026", "半年度报告"),
]

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/",
}

ANN_LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
ANN_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"

# 从标题**尾部**匹配，允许前面挂任意公司名前缀（可能带冒号也可能不带）
TITLE_PAT = re.compile(r"(?P<y>\d{4})\s*年\s*(?P<t>年度报告|半年度报告)$")

# 明确排除的修饰词：带这些的一律不要（摘要不是全文）
EXCLUDE_WORDS = ["摘要", "英文", "英文版", "更正", "修订", "补充", "取消",
                 "财务报告", "意见", "公告"]


# ---- 工具 ----------------------------------------------------------------

def http_get(url, params=None, retries=3, timeout=40):
    """带重试的 GET。返回 requests.Response。"""
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    raise last


def norm_title(title: str) -> str:
    """标题规范化：去掉公司名前缀和空白，只留核心部分。

    注意不能按冒号切——见文件头部的坑。
    这里只做 strip 和全角/半角冒号后的取尾，
    真正的「是不是我们要的报告」交给 TITLE_PAT 从尾部判断。
    """
    t = title.strip()
    # 去掉形如「XXXX:」或「XXXX：」的前缀（有就切，没有就留着，尾部匹配仍能工作）
    for sep in (":", "："):
        if sep in t:
            t = t.split(sep)[-1].strip()
    return t


def fetch_ann_list(stock_code: str, log):
    """拉定期报告类公告，翻页直到早于 2025-01-01 或翻满。

    注意：接口的 stock_list 只认 6 位纯代码，传「000636.SZ」会静默返回 0 条。
    """
    code6 = stock_code.split(".")[0]
    out = []
    seen = set()
    for page in range(1, 6):
        try:
            r = http_get(ANN_LIST_URL, params={
                "sr": -1, "page_size": 100, "page_index": page,
                "ann_type": "A", "client_source": "web",
                "stock_list": code6, "f_node": 1, "s_node": 0,
            })
            lst = r.json().get("data", {}).get("list", []) or []
        except Exception as exc:
            log.warning("ann list %s page %d failed: %s", stock_code, page, exc)
            say(f"  ⚠ 公告列表第 {page} 页请求失败，后面的页可能也拿不到")
            break

        if not lst:
            break
        new = 0
        for it in lst:
            ac = it.get("art_code")
            if ac and ac not in seen:
                seen.add(ac)
                out.append(it)
                new += 1
        # 本页所有条目都早于 2025-01-01 就可以停了
        dates = [str(it.get("notice_date", ""))[:10] for it in lst]
        if dates and max(dates) < "2025-01-01":
            break
        if new == 0:
            break
        time.sleep(0.3)

    log.info("ann list %s: %d entries", stock_code, len(out))
    return out


def pick_reports(entries, log, stock_code):
    """从公告里挑出目标报告。返回 (命中列表, 被排除的近义条目列表)。"""
    hits, near = {}, []
    for it in entries:
        raw_title = str(it.get("title", ""))
        core = norm_title(raw_title)
        m = TITLE_PAT.search(core)
        if not m:
            if "年度报告" in core or "半年度报告" in core:
                near.append((raw_title, str(it.get("notice_date", ""))[:10]))
            continue
        y, t = m.group("y"), m.group("t")
        if (y, t) not in WANT_REPORTS:
            continue
        if any(w in core for w in EXCLUDE_WORDS):
            near.append((raw_title, str(it.get("notice_date", ""))[:10]))
            continue
        key = (y, t)
        # 同一年同一类型出现多次（比如重发），保留公告日期最新的那条
        prev = hits.get(key)
        if prev is None or str(it.get("notice_date", "")) > str(prev.get("notice_date", "")):
            hits[key] = it
    log.info("pick %s: hits=%s near=%d", stock_code,
             sorted(hits.keys()), len(near))
    return hits, near


def get_pdf_url(art_code: str, log):
    """由 art_code 取 PDF 直链。"""
    r = http_get(ANN_CONTENT_URL, params={
        "art_code": art_code, "client_source": "web", "page_index": 1,
    })
    data = r.json().get("data", {}) or {}
    url = data.get("attach_url") or data.get("attach_url_web")
    return url, data.get("notice_title", "")


def download_pdf(url: str, dest: Path, log):
    """下 PDF。返回 (字节数, md5)。已存在且大小一致则跳过，返回 (size, md5)。"""
    if dest.exists():
        size = dest.stat().st_size
        with open(dest, "rb") as f:
            head = f.read(5)
        if size > 0 and head.startswith(b"%PDF"):
            md5 = hashlib.md5(dest.read_bytes()).hexdigest()
            return size, md5, True  # skipped

    r = http_get(url, retries=3, timeout=180)
    content = r.content
    if not content.startswith(b"%PDF"):
        raise ValueError(f"下下来的不是 PDF（前 8 字节：{content[:8]!r}）")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".pdf.part")
    tmp.write_bytes(content)
    tmp.replace(dest)
    return len(content), hashlib.md5(content).hexdigest(), False


# ---- 主流程 --------------------------------------------------------------

def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / f"fetch_reports_{TODAY}.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    log = logging.getLogger(TAG)

    if not SELECTION.exists():
        say(f"找不到选定清单：{SELECTION}")
        return 1

    with open(SELECTION, encoding="utf-8-sig") as f:
        companies = list(csv.DictReader(f))

    # 便于小样测试：--codes 000636,600563 只跑这几家
    import argparse
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--codes", default="",
                    help="只跑这些股票代码，逗号分隔。不填则跑清单里全部")
    args = ap.parse_args()
    if args.codes:
        # 清单里是 000636.SZ 带后缀，这里按 6 位前缀比对，免得传纯代码匹配不上
        want = {s.strip() for s in args.codes.split(",") if s.strip()}
        companies = [c for c in companies
                     if c["股票代码"].strip().split(".")[0] in want]
        say(f"（小样模式，只跑 {len(companies)} 家）")
        if not companies:
            say("⚠ 小样过滤后一家都没有，检查 --codes 写对没有")
            return 1

    say(f"开始（{TODAY}）｜公司 {len(companies)} 家｜目标报告 "
        f"{len(WANT_REPORTS)} 期/家 = 最多 {len(companies) * len(WANT_REPORTS)} 份")

    manifest = []
    skipped_all = []
    missing_all = {}

    for idx, c in enumerate(companies, 1):
        code = c["股票代码"].strip()
        name = c["公司名"].strip()
        say(f"[{idx}/{len(companies)}] {code} {name}")
        log.info("=== %s %s ===", code, name)

        try:
            entries = fetch_ann_list(code, log)
        except Exception as exc:
            plain_error(exc)
            missing_all[f"{code} {name}"] = [f"{y}年{t}" for y, t in WANT_REPORTS]
            continue

        # 列表为空 = 接口没给数据。绝不能当成「这家公司没有定期报告」——
        # 那会静默漏掉整家公司（代码写错、接口改版都会这样）。
        if not entries:
            say(f"  ⚠ 定期报告列表返回 0 条，这是异常不是「没有报告」。"
                f"很可能是代码格式或接口问题，需要排查")
            log.error("ann list empty for %s", code)

        hits, near = pick_reports(entries, log, code)
        for t, d in near:
            skipped_all.append({"股票代码": code, "公司名": name,
                                "标题": t, "公告日期": d})

        missing = [(y, t) for y, t in WANT_REPORTS if (y, t) not in hits]
        if missing:
            missing_all[f"{code} {name}"] = [f"{y}年{t}" for y, t in missing]

        for (y, t) in WANT_REPORTS:
            it = hits.get((y, t))
            if not it:
                say(f"  缺：{y}年{t}")
                continue
            label = f"{y}年{t}"
            dest = RAW_DIR / f"{code}_{name}" / f"{label}.pdf"
            try:
                pdf_url, real_title = get_pdf_url(it["art_code"], log)
                if not pdf_url:
                    say(f"  {label}：没拿到 PDF 链接，跳过")
                    log.warning("%s %s no attach_url", code, label)
                    missing_all.setdefault(f"{code} {name}", []).append(label + "(无链接)")
                    continue
                size, md5, was_skipped = download_pdf(pdf_url, dest, log)
                mb = size / 1024 / 1024
                say(f"  {'跳过已有' if was_skipped else '已下载'}：{label}  "
                    f"{mb:.1f} MB")
                manifest.append({
                    "股票代码": code, "公司名": name, "报告期": label,
                    "公告日期": str(it.get("notice_date", ""))[:10],
                    "art_code": it["art_code"],
                    "公告标题": real_title,
                    "PDF链接": pdf_url,
                    "本地文件": str(dest.relative_to(ROOT)),
                    "字节数": size, "md5": md5,
                    "下载日期": TODAY,
                })
            except Exception as exc:
                plain_error(exc)
                say(f"  {label} 下载失败")
                log.exception("download %s %s failed", code, label)
                missing_all.setdefault(f"{code} {name}", []).append(label + "(下载失败)")
            time.sleep(0.4)

    # ---- 落盘 ----
    if manifest:
        mpath = ROOT / "data" / "clean" / "定期报告_下载清单.csv"
        mpath.parent.mkdir(parents=True, exist_ok=True)
        with open(mpath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(manifest[0].keys()))
            w.writeheader()
            w.writerows(manifest)
        say(f"下载清单已存：{mpath.relative_to(ROOT)}（{len(manifest)} 条）")

    if skipped_all:
        spath = LOG_DIR / f"fetch_reports_skipped_{TODAY}.csv"
        with open(spath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(skipped_all[0].keys()))
            w.writeheader()
            w.writerows(skipped_all)
        say(f"被排除的近似条目已存：{spath.relative_to(ROOT)}（{len(skipped_all)} 条）")

    # ---- 汇总 ----
    say("")
    say(f"===== 汇总：共下载/保留 {len(manifest)} 份 =====")
    if missing_all:
        say(f"⚠ 有 {len(missing_all)} 家没凑齐：")
        for k, v in missing_all.items():
            say(f"    {k}：缺 {'、'.join(v)}")
    else:
        say("全部凑齐，无缺漏。")

    log.info("done: %d files, missing=%s", len(manifest), missing_all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
