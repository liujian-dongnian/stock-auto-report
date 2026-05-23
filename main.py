"""
股票日报自动化系统 v2.3
架构：新浪财经(备用行情) + AKShare(主力数据) → TokenHub AI分析 → Pushplus微信推送
运行环境：GitHub Actions (ubuntu-latest, Python 3.10+)

修复记录：
  v2.3 - 解决 GitHub Actions 境外 IP 被 AKShare/东方财富拒绝的问题
    - 新增新浪财经备用数据源（解决 Connection aborted）
    - AKShare 作为主力数据源，失败时自动降级到新浪
    - 新浪接口支持一次批量查询（逗号分隔多个代码）
    - 所有 AKShare 请求带 3 次重试 + 5 秒间隔
    - 不依赖 tushare，不需要 TUSHARE_TOKEN
"""

import os
import sys
import time
import requests
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

# ========== 配置区 ==========
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
HUNYUAN_API_KEY = os.environ.get("HUNYUAN_API_KEY", "")
HUNYUAN_BASE_URL = "https://tokenhub.tencentmaas.com/v1"
HUNYUAN_MODEL = "deepseek-v4-flash"

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY = 5

# AI 分析系统提示词
SYSTEM_PROMPT = """#人设#
你是一位专业、高效的股票情报分析助手。风格务实，输出有价值的判断，不模棱两可。

#任务#
根据提供的股票数据，生成个股日报。对每只标的给出明确的投资决策和关注价位。

#输出格式要求#
一、自选标的日报（按投资决策排序）
🟢 分批布局 / 🟡 持有观望 / 🔴 减仓处理
（用颜色标签标注操作建议，"分批布局"排最前）

每只标的格式：
【股票名称 · 代码】
【最新价】：XX.XX 元 | 涨跌幅：+X.XX% | 成交额：X.XX 亿
【历史位置】：当前价处于近3年区间的 X%（最高XX / 最低XX / 均价XX）
【估值参考】：PE XX.X | PB X.XX（行业平均PE XX）
【基本面摘要】：一句话核心判断（50字内）
【近期要点】：公司公告、行业政策、热点新闻（50字内）
【投资决策】：分批布局 / 持有观望 / 减仓处理（选一个，不带"建议"等模糊词）
【关注价位】：低于 XX 元可考虑建仓/加仓，高于 XX 元可考虑减仓

#约束#
- 所有数据必须基于提供的真实数据，禁止编造任何价格、数值
- 如果某项数据缺失，标注"暂无数据"而非编造
- 个股基本面和新闻分析各不超过50字
- 投资决策必须给出明确方向，不要写"可考虑""建议关注"等暧昧表述
- 最后加一行免责声明：⚠️ 免责声明：本内容仅为数据整理与AI分析，不构成任何投资建议。
"""


# ========== 工具函数 ==========

def to_sina_code(code: str) -> str:
    """将纯数字代码转为新浪格式：sh600519 / sz000001"""
    if code.startswith("6") or code.startswith("5"):
        return "sh" + code
    else:
        return "sz" + code


def safe_float(val, default=0.0):
    """安全转浮点数"""
    try:
        if val is None or val == "" or val == "-":
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


# ========== 1. 新浪财经：实时行情（备用数据源，支持批量） ==========

def get_sina_realtime_batch(codes):
    """
    通过新浪财经批量获取实时行情（一次请求获取所有股票）
    返回 dict: {code: {price, change_pct, volume, name, pre_close}}
    """
    result = {}
    try:
        sina_codes = [to_sina_code(c) for c in codes]
        url = "http://hq.sinajs.cn/list=" + ",".join(sina_codes)
        headers = {
            "Referer": "https://finance.sina.com.cn",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"  新浪接口返回 {resp.status_code}")
            return result

        for line in resp.text.strip().split("\n"):
            if not line or "=" not in line:
                continue
            try:
                # 解析: var hq_str_sh600519="贵州茅台,开盘价,昨收,当前价,最高,最低,买一价,买一量,卖一价,卖一量,成交量,成交额,...";
                data_part = line.split('"')[1]
                if not data_part or data_part == "":
                    continue

                # 提取代码
                code_raw = line.split("=")[0].replace("var hq_str_", "")
                # sh600519 -> 600519
                code = code_raw[2:]

                fields = data_part.split(",")
                name = fields[0]
                pre_close = safe_float(fields[2])
                current = safe_float(fields[3])
                high = safe_float(fields[4])
                low = safe_float(fields[5])
                volume_amount = safe_float(fields[9])  # 成交额（元）

                # 计算涨跌幅
                if pre_close > 0:
                    change_pct = (current - pre_close) / pre_close * 100
                else:
                    change_pct = 0.0

                result[code] = {
                    "name": name,
                    "price": current,
                    "pre_close": pre_close,
                    "change_pct": change_pct,
                    "high": high,
                    "low": low,
                    "volume": volume_amount / 1e8,  # 转为亿元
                }
            except (IndexError, ValueError) as e:
                continue

        print(f"  ✅ 新浪财经获取到 {len(result)} 只标的行情")
    except Exception as e:
        print(f"  ❌ 新浪财经请求失败: {e}")

    return result


# ========== 2. AKShare：实时行情（带重试，主力数据源） ==========

def fetch_with_retry(func, desc="请求"):
    """通用重试包装"""
    last_err = None
    for i in range(1, MAX_RETRIES + 1):
        try:
            return func()
        except Exception as e:
            last_err = e
            if i < MAX_RETRIES:
                print(f"    ⚠️ {desc} 第{i}次失败: {e}，{RETRY_DELAY}秒后重试...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    ❌ {desc} 重试{MAX_RETRIES}次均失败: {e}")
    return None


def get_akshare_realtime(code):
    """通过 AKShare 获取单只股票实时行情"""
    spot = fetch_with_retry(
        lambda: ak.stock_zh_a_spot_em(),
        desc=f"AKShare行情"
    )
    if spot is None:
        return None

    row = spot[spot["代码"] == code]
    if row.empty:
        return None

    return {
        "price": safe_float(row["最新价"].values[0]),
        "change_pct": safe_float(row["涨跌幅"].values[0]),
        "volume": safe_float(row["成交额"].values[0]) / 1e8,
    }


def get_realtime_data(code, sina_data):
    """获取实时行情：优先 AKShare，失败降级到新浪"""
    # 先尝试 AKShare
    ak_data = get_akshare_realtime(code)
    if ak_data is not None and ak_data["price"] > 0:
        print(f"    行情(AKShare): {ak_data['price']:.2f} {ak_data['change_pct']:+.2f}%")
        return ak_data

    # AKShare 失败，降级到新浪
    if code in sina_data:
        sd = sina_data[code]
        print(f"    行情(新浪备用): {sd['price']:.2f} {sd['change_pct']:+.2f}%")
        return {
            "price": sd["price"],
            "change_pct": sd["change_pct"],
            "volume": sd["volume"],
        }

    print(f"    ❌ 行情获取完全失败")
    return {"price": 0.0, "change_pct": 0.0, "volume": 0.0}


# ========== 3. AKShare：3年历史数据（带重试） ==========

def get_history_data(code):
    """获取近3年最高价、最低价、均价"""
    hist = fetch_with_retry(
        lambda: ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(datetime.now() - timedelta(days=365*3)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
            adjust="qfq"
        ),
        desc=f"AKShare历史({code})"
    )
    if hist is None or hist.empty:
        return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}

    return {
        "high3y": float(hist["最高"].max()),
        "low3y": float(hist["最低"].min()),
        "avg3y": float(hist["收盘"].mean()),
    }


# ========== 4. AKShare：基本面 PE/PB（带重试） ==========

def get_fundamental_data(code):
    """通过 AKShare 获取 PE、PB"""
    data = {"pe": None, "pb": None}
    try:
        df = fetch_with_retry(
            lambda: ak.stock_individual_info_em(symbol=code),
            desc=f"AKShare基本面({code})"
        )
        if df is not None and not df.empty:
            info_dict = dict(zip(df["item"], df["value"]))
            # 尝试多种可能的字段名
            for key in ["市盈率(动态)", "市盈率-动态", "PE(动)", "动态市盈率", "市盈率"]:
                if key in info_dict:
                    data["pe"] = safe_float(str(info_dict[key]).replace(",", ""))
                    break
            for key in ["市净率", "PB", "市净率MRQ"]:
                if key in info_dict:
                    data["pb"] = safe_float(str(info_dict[key]).replace(",", ""))
                    break
            if data["pe"] is not None or data["pb"] is not None:
                print(f"    PE={data['pe']}, PB={data['pb']}")
    except Exception as e:
        print(f"    基本面获取失败: {e}")

    return data


# ========== 5. AKShare：公司公告（带重试） ==========

def get_news_data(code):
    """获取公司最新公告"""
    news_list = []
    try:
        df = fetch_with_retry(
            lambda: ak.stock_notice_report(symbol=code),
            desc=f"AKShare公告({code})"
        )
        if df is not None and not df.empty:
            for _, row in df.head(3).iterrows():
                title = str(row.get("标题", row.get("title", "")))
                news_list.append(title)
            print(f"    公告 {len(news_list)} 条")
    except Exception as e:
        print(f"    公告获取失败: {e}")
    return news_list


# ========== 6. AI 分析 ==========

def call_ai_analysis(stocks_data):
    if not HUNYUAN_API_KEY:
        print("未配置 HUNYUAN_API_KEY，使用纯数据报告模式")
        return generate_plain_report(stocks_data)

    user_message = "以下是我关注的标的最新数据，请根据这些数据生成个股日报：\n\n"

    for item in stocks_data:
        d = item["basic"]
        f = item.get("fundamental", {})
        news = item.get("news", [])

        user_message += f"【{d['name']} · {d['code']}】\n"
        user_message += f"- 最新价：{d['price']:.2f} 元\n"
        user_message += f"- 涨跌幅：{d['change_pct']:+.2f}%\n"
        user_message += f"- 成交额：{d['volume']:.2f} 亿\n"
        user_message += f"- 近3年最高：{d['high3y']:.2f} / 最低：{d['low3y']:.2f} / 均价：{d['avg3y']:.2f}\n"

        if f.get("pe") is not None:
            user_message += f"- PE：{f['pe']:.2f}\n"
        else:
            user_message += "- PE：暂无数据\n"
        if f.get("pb") is not None:
            user_message += f"- PB：{f['pb']:.2f}\n"
        else:
            user_message += "- PB：暂无数据\n"

        if news:
            user_message += f"- 近期公告：{' | '.join(news[:3])}\n"

        user_message += "\n"

    try:
        response = requests.post(
            f"{HUNYUAN_BASE_URL}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {HUNYUAN_API_KEY}"
            },
            json={
                "model": HUNYUAN_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message}
                ],
                "temperature": 0.3,
                "max_tokens": 4096
            },
            timeout=60
        )
        response.raise_for_status()
        ai_report = response.json()["choices"][0]["message"]["content"]
        print("✅ AI分析完成")
        return ai_report
    except Exception as e:
        print(f"❌ AI分析失败: {e}，使用纯数据报告")
        return generate_plain_report(stocks_data)


def generate_plain_report(stocks_data):
    report = "📈 自选标的日报（数据版，AI分析暂不可用）\n\n"
    for item in stocks_data:
        d = item["basic"]
        f = item.get("fundamental", {})
        if d["high3y"] > d["low3y"]:
            position = (d["price"] - d["low3y"]) / (d["high3y"] - d["low3y"]) * 100
        else:
            position = 50.0
        pe_str = f"{f['pe']:.2f}" if f.get("pe") is not None else "暂无数据"
        pb_str = f"{f['pb']:.2f}" if f.get("pb") is not None else "暂无数据"
        report += f"""【{d['name']} · {d['code']}】
【最新价】：{d['price']:.2f} 元 | 涨跌幅：{d['change_pct']:+.2f}% | 成交额：{d['volume']:.2f} 亿
【历史位置】：当前价处于近3年区间的 {position:.1f}%（最高{d['high3y']:.2f} / 最低{d['low3y']:.2f} / 均价{d['avg3y']:.2f}）
【估值参考】：PE {pe_str} | PB {pb_str}

"""
    report += "⚠️ 免责声明：本内容仅为数据整理，不构成任何投资建议。\n"
    return report


# ========== 7. 推送微信 ==========

def send_pushplus(title, content):
    if not PUSHPLUS_TOKEN:
        print("未配置 PUSHPLUS_TOKEN，跳过推送")
        return False
    try:
        result = requests.post(
            "https://www.pushplus.plus/send",
            json={
                "token": PUSHPLUS_TOKEN,
                "title": title,
                "content": content.replace("\n", "<br>"),
                "template": "html"
            },
            timeout=10
        ).json()
        if result.get("code") == 200:
            print("✅ Pushplus推送成功")
            return True
        else:
            print(f"❌ Pushplus推送失败: {result}")
            return False
    except Exception as e:
        print(f"❌ 推送异常: {e}")
        return False


# ========== 主流程 ==========

def main():
    print("=" * 50)
    print("股票日报自动化系统 v2.3（新浪备用+AKShare）")
    print(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    print(f"\n配置检查：")
    print(f"  PUSHPLUS_TOKEN: {'✅' if PUSHPLUS_TOKEN else '⚠️'}")
    print(f"  HUNYUAN_API_KEY: {'✅' if HUNYUAN_API_KEY else '⚠️'}")

    # 1. 加载股票列表
    stocks = []
    try:
        with open("stock_list.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and " " in line:
                    parts = line.split(maxsplit=1)
                    stocks.append({"code": parts[0].strip(), "name": parts[1].strip()})
    except Exception as e:
        print(f"❌ 读取股票列表失败: {e}")
        sys.exit(1)

    print(f"\n共 {len(stocks)} 只标的：")
    for s in stocks:
        print(f"  - {s['name']}({s['code']})")

    codes = [s["code"] for s in stocks]

    # 2. 新浪财经批量获取实时行情（备用数据源，一次请求搞定）
    print("\n" + "=" * 50)
    print("[1/3] 新浪财经批量获取行情（备用）...")
    print("=" * 50)
    sina_data = get_sina_realtime_batch(codes)
    if sina_data:
        print(f"  ✅ 新浪备用数据就绪（{len(sina_data)}只）")
    else:
        print(f"  ⚠️ 新浪也失败了，仅靠 AKShare")

    # 3. 逐只采集详细数据
    print("\n" + "=" * 50)
    print("[2/3] 逐只采集详细数据...")
    print("=" * 50)

    stocks_data = []
    for idx, s in enumerate(stocks):
        code = s["code"]
        print(f"\n[{idx+1}/{len(stocks)}] {s['name']}({code})...")

        # 实时行情：AKShare 优先，新浪兜底
        realtime = get_realtime_data(code, sina_data)

        # 历史数据
        history = get_history_data(code)
        print(f"    3年区间: {history['low3y']:.2f} ~ {history['high3y']:.2f} 均价{history['avg3y']:.2f}")

        # 基本面 PE/PB
        fundamental = get_fundamental_data(code)

        # 公告
        news = get_news_data(code)

        basic = {**realtime, **history, "name": s["name"], "code": code}
        stocks_data.append({
            "basic": basic,
            "fundamental": fundamental,
            "news": news
        })

        # 请求间隔
        if idx < len(stocks) - 1:
            time.sleep(2)

    # 4. AI 分析
    print("\n" + "=" * 50)
    print("[3/3] AI 分析 & 推送...")
    print("=" * 50)

    report = call_ai_analysis(stocks_data)
    today = datetime.now().strftime("%Y-%m-%d")
    send_pushplus(f"📈 股票日报 {today}", report)

    print("\n" + report)
    print("\n" + "=" * 50)
    print("=== 任务完成 ===")
    print("=" * 50)


if __name__ == "__main__":
    main()
