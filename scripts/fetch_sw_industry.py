# -*- coding: utf-8 -*-
"""
fetch_sw_industry.py —— 抓申万三级行业成分股清单

【这个脚本能做什么】
用 akshare 取申万三级行业的成分股名单（数据源：乐咕乐股 legulegu.com，
它镜像的是申万宏源官方分类）。默认取「被动元件」(850823.SI)，
另附三个边界行业（军工电子Ⅲ / 其他电子Ⅲ / 电子化学品Ⅲ），
用来把「被动元件相关但不属于该三级」的公司讲清楚。

【这个脚本不能做什么】
- 不判断公司主业，不筛选，不分主板创业板，全部照抄数据源。
- 不提供申万分类的官方实时权威版本：本机访问申万宏源官网 xls 时
  SSL 证书校验失败（本地根证书问题），未绕过（不关校验）。
  所以走的是 legulegu 镜像，**权威性以申万宏源官网为准**。

【产出】
data/raw/申万三级_<行业名>_成分股_原始_<日期>.csv   生肉，只抓不改
data/clean/被动元件_公司清单.csv                    净菜，加了交易所/板块列
logs/fetch_sw_industry_<日期>.log                   技术细节

【怎么跑】
cd scripts && python fetch_sw_industry.py
"""

import sys
import logging
import datetime
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

TAG = "[fetch_sw_industry]"


def say(msg):
    """终端进度。必须 flush，否则输出被重定向时会攒在缓冲区里看不到。"""
    print(f"{TAG} {msg}", flush=True)


ERROR_HINTS = {
    "SSLError": "SSL 证书校验失败（本地根证书问题，不是对方挂了）",
    "ConnectionError": "网络连不上（断网，或对方拒绝连接）",
    "Timeout": "对方太久没响应（超时）",
    "HTTPError": "对方返回了错误状态码（多半是被限流或封了）",
    "KeyError": "返回的表格里没有预期的那一列（对方网页改版了）",
    "ImportError": "缺依赖库（akshare 装了没）",
}


def plain_error(exc):
    """把异常翻成人话。按类名查表，查不到就退回类名。"""
    name = type(exc).__name__
    hint = ERROR_HINTS.get(name)
    say(f"出错：{hint}" if hint else f"出错：{name}")
    say(f"原始信息：{exc}")


# ---- 配置 ----------------------------------------------------------------

TODAY = datetime.date.today().isoformat()

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
CLEAN_DIR = ROOT / "data" / "clean"
LOG_DIR = ROOT / "logs"

# 主目标：申万三级 被动元件
TARGET = ("850823.SI", "被动元件")

# 边界行业：名字常和被动元件一起提，但申万三级不在「被动元件」里。
# 只用来在 codebook 里说明边界，不进最终清单。
BOUNDARY = [
    ("857451.SI", "军工电子Ⅲ"),
    ("850841.SI", "其他电子Ⅲ"),
    ("850861.SI", "电子化学品Ⅲ"),
]


# ---- 交易所 / 板块 判定 ----------------------------------------------------

def exchange_of(code: str) -> str:
    """按代码前缀判交易所。申万只给 6 位代码 + .SH/.SZ 后缀，够用了。"""
    if code.endswith(".SH"):
        return "上交所"
    if code.endswith(".SZ"):
        return "深交所"
    if code.endswith(".BJ"):
        return "北交所"
    return "未知"


def board_of(code: str) -> str:
    """判板块。只看纯数字部分的前缀。"""
    num = code.split(".")[0]
    if num.startswith("688"):
        return "科创板"
    if num.startswith(("300", "301")):
        return "创业板"
    if num.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if num.startswith(("000", "001", "002", "003")):
        return "深市主板"
    if num.startswith(("43", "83", "87", "92")):
        return "北交所"
    return "未知"


def setup_log():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOG_DIR / f"fetch_sw_industry_{TODAY}.log"
    logging.basicConfig(
        filename=logfile,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    return logfile


# ---- 主流程 --------------------------------------------------------------

def fetch_cons(ak, code: str, name: str, log, retries: int = 3):
    """取一个三级行业的成分股。返回 DataFrame，失败返回 None。

    重试 3 次：这个源是网页抓取，实测会偶发返回空页
    （pandas 报 `No tables found`），重试一次通常就好。
    """
    import time

    for attempt in range(1, retries + 1):
        try:
            if attempt == 1:
                say(f"正在抓「{name}」({code}) …")
            else:
                say(f"  第 {attempt} 次重试「{name}」…")
            df = ak.sw_index_third_cons(symbol=code)
            if df is None or df.empty:
                raise ValueError("返回空表")
            break
        except Exception as exc:
            log.warning("fetch %s (%s) attempt %d/%d failed: %s",
                        name, code, attempt, retries, exc)
            if attempt == retries:
                plain_error(exc)
                say(f"「{name}」重试 {retries} 次仍失败")
                log.exception("fetch %s (%s) failed", name, code)
                return None
            time.sleep(2 * attempt)  # 退避，别把对方惹毛
    else:
        return None

    say(f"  拿到 {len(df)} 家")
    log.info("fetched %s (%s): %d rows", name, code, len(df))

    df = df.copy()
    df.insert(0, "申万三级代码", code)
    df.insert(1, "申万三级名称", name)

    # 生肉先落盘，只抓不改
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"申万三级_{name}_成分股_原始_{TODAY}.csv"
    df.to_csv(raw_path, index=False, encoding="utf-8-sig")
    say(f"  生肉已存：{raw_path.relative_to(ROOT)}")
    return df


def main():
    logfile = setup_log()
    log = logging.getLogger(TAG)
    say(f"开始（{TODAY}）")

    try:
        import akshare as ak
    except ImportError as exc:
        plain_error(exc)
        return 1

    # 1) 主目标
    df = fetch_cons(ak, *TARGET, log=log)
    if df is None or df.empty:
        say("主目标没抓到，中止（不清空已有产出）")
        return 1

    # 2) 净菜：只留需要的列，补上交易所/板块
    # 列名沿用数据源原义（「纳入时间」），不要改叫「上市日期」——
    # 实测它对不上上市日（如麦捷科技给 2021-07-30，实际 2012 年上市），
    # 更像是分类口径的调整时间。
    clean = pd.DataFrame({
        "股票代码": df["股票代码"].astype(str).str.strip(),
        "公司名": df["股票简称"].astype(str).str.strip(),
        "纳入时间": df.get("纳入时间", pd.Series([""] * len(df))).astype(str),
    })
    clean["交易所"] = clean["股票代码"].map(exchange_of)
    clean["板块"] = clean["股票代码"].map(board_of)
    clean["申万三级"] = TARGET[1]
    clean["申万三级代码"] = TARGET[0]
    clean["数据日期"] = TODAY
    clean = clean.sort_values("股票代码").reset_index(drop=True)

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    clean_path = CLEAN_DIR / "被动元件_公司清单.csv"
    clean.to_csv(clean_path, index=False, encoding="utf-8-sig")
    say(f"净菜已存：{clean_path.relative_to(ROOT)}（{len(clean)} 行）")

    # 3) 边界行业：只记录「哪些相关公司落在别处」，不落净菜
    #
    # 注意：只在**全部**边界行业都抓成功时才覆盖文件。
    # 之前踩过坑 —— 某个行业偶发抓空被跳过，结果用不完整的行数
    # 覆盖掉了上一轮完整的文件，等于静默丢数据。
    say("查边界行业（只为说明分类边界，不进清单）…")
    boundary_rows = []
    failed = []
    for code, name in BOUNDARY:
        bdf = fetch_cons(ak, code, name, log=log)
        if bdf is None:
            failed.append(name)
            continue
        for _, r in bdf.iterrows():
            boundary_rows.append({
                "股票代码": str(r["股票代码"]).strip(),
                "公司名": str(r["股票简称"]).strip(),
                "申万三级": name,
                "申万三级代码": code,
            })
    bpath = CLEAN_DIR / "被动元件_边界行业公司_参考.csv"
    if failed:
        say(f"  有 {len(failed)} 个边界行业没抓到（{'、'.join(failed)}），"
            f"**不覆盖**已有文件，免得丢数据")
        log.warning("boundary incomplete, not writing: %s", failed)
        if bpath.exists():
            say(f"  现有文件保持不动：{bpath.relative_to(ROOT)}")
    elif boundary_rows:
        pd.DataFrame(boundary_rows).to_csv(bpath, index=False, encoding="utf-8-sig")
        say(f"  边界行业参考已存：{bpath.relative_to(ROOT)}（{len(boundary_rows)} 行）")

    # 4) 终端打印
    say(f"===== 申万三级「被动元件」全部 {len(clean)} 家 =====")
    for i, r in clean.iterrows():
        say(f"{i + 1:>3}. {r['股票代码']:<11} {r['公司名']:<8} {r['交易所']} / {r['板块']}")

    say(f"完毕。日志：{logfile.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
