"""
股票日报自动化系统 v2.2
架构：AKShare数据采集（行情+基本面+公告） → TokenHub AI分析 → Pushplus微信推送
运行环境：GitHub Actions (ubuntu-latest, Python 3.10+)

修复记录：
  v2.2 - 全面改用 AKShare，移除 Tushare 依赖
    - AKShare Connection aborted → 加重试机制（3次）+ 请求间隔
    - Tushare daily_basic 需要2000积分（用户仅120积分），改用 AKShare stock_individual_info_em
    - Tushare disclosure_date/anns 限频严重，改用 AKShare stock_notice_report
    - 移除 tushare 依赖，不再需要 TUSHARE_TOKEN
"""

import os
import sys
import time
import requests
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

# ========== 配置区（从 GitHub Secrets 读取） ==========
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")

# TokenHub AI 配置（OpenAI 兼容格式）
HUNYUAN_API_KEY = os.environ.get("HUNYUAN_API_KEY", "")
HUNYUAN_BASE_URL = "https://tokenhub.tencentmaas.com/v1"
HUNYUAN_MODEL = "deepseek-v4-flash"

# AKShare 请求配置
AKSHARE_MAX_RETRIES = 3        # 最大重试次数
AKSHARE_RETRY_DELAY = 5        # 重试间隔（秒）
AKSHARE_REQUEST_DELAY = 2      # 每次请求间隔（秒）

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


# ========== 通用重试装饰器 ==========
def retry_on_failure(func):
    """AKShare 请求重试包装器，自动重试 3 次"""
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(1, AKSHARE_MAX_RETRIES + 1):
            try:
                result = func(*args, **kwargs)
                if attempt > 1:
                    print(f"    ✅ 第{attempt}次重试成功")
                return result
            except Exception as e:
                last_error = e
                if attempt < AKSHARE_MAX_RETRIES:
                    print(f"    ⚠️ 第{attempt}次请求失败: {e}，{AKSHARE_RETRY_DELAY}秒后重试...")
                    time.sleep(AKSHARE_RETRY_DELAY)
                else:
                    print(f"    ❌ 重试{AKSHARE_MAX_RETRIES}次均失败: {e}")
        return None
    return wrapper


# ========== 股票列表 ==========
def load_stock_list():
    stocks = []
    try:
        with open("stock_list.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and " " in line:
                    parts = line.split(maxsplit=1)
                    code = parts[0].strip()
                    name = parts[1].strip()
                    stocks.append({"code": code, "name": name})
    except Exception as e:
        print(f"读取股票列表失败: {e}")
    return stocks


# ========== 1. AKShare：实时行情（带重试） ==========
@retry_on_failure
def fetch_spot_data():
    return ak.stock_zh_a_spot_em()


def get_realtime_data(code):
    """获取实时行情：最新价、涨跌幅、成交额"""
    spot = fetch_spot_data()
    if spot is None:
        return {"price": 0.0, "change_pct": 0.0, "volume": 0.0}

    row = spot[spot["代码"] == code]
    if row.empty:
        print(f"    未找到 {code} 行情数据")
        return {"price": 0.0, "change_pct": 0.0, "volume": 0.0}

    return {
        "price": float(row["最新价"].values[0]) if pd.notna(row["最新价"].values[0]) else 0.0,
        "change_pct": float(row["涨跌幅"].values[0]) if pd.notna(row["涨跌幅"].values[0]) else 0.0,
        "volume": float(row["成交额"].values[0]) / 1e8 if pd.notna(row["成交额"].values[0]) else 0.0,
    }


# ========== 2. AKShare：3年历史数据（带重试） ==========
@retry_on_failure
def fetch_history_data(code):
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y%m%d")
    return ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start, end_date=end, adjust="qfq"
    )


def get_history_data(code):
    """获取近3年最高价、最低价、均价"""
    hist = fetch_history_data(code)
    if hist is None or hist.empty:
        return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}

    return {
        "high3y": float(hist["最高"].max()),
        "low3y": float(hist["最低"].min()),
        "avg3y": float(hist["收盘"].mean()),
    }


# ========== 3. AKShare：基本面数据（PE/PB） ==========
@retry_on_failure
def fetch_individual_info(code):
    return ak.stock_individual_info_em(symbol=code)


def get_fundamental_data(code):
    """通过 AKShare 获取 PE、PB 等基本面指标"""
    data = {"pe": None, "pb": None}
    try:
        df = fetch_individual_info(code)
        if df is not None and not df.empty:
            # stock_individual_info_em 返回两列：item / value
            info_dict = dict(zip(df["item"], df["value"]))
            # 尝试提取 PE、PB
            for key in ["市盈率(动态)", "市盈率-动态", "PE(动)", "动态市盈率"]:
                if key in info_dict:
                    try:
                        data["pe"] = float(str(info_dict[key]).replace(",", ""))
                    except (ValueError, TypeError):
                        pass
                    break

            for key in ["市净率", "PB", "市净率MRQ"]:
                if key in info_dict:
                    try:
                        data["pb"] = float(str(info_dict[key]).replace(",", ""))
                    except (ValueError, TypeError):
                        pass
                    break

            if data["pe"] is not None or data["pb"] is not None:
                print(f"    PE={data['pe']}, PB={data['pb']}")
            else:
                print(f"    未找到PE/PB字段，可用字段: {list(info_dict.keys())[:10]}")
    except Exception as e:
        print(f"    基本面获取失败: {e}")

    return data


# ========== 4. AKShare：公司公告/新闻 ==========
@retry_on_failure
def fetch_stock_notice(code):
    return ak.stock_notice_report(symbol=code)


def get_news_data(code):
    """获取公司最新公告标题"""
    news_list = []
    try:
        df = fetch_stock_notice(code)
        if df is not None and not df.empty:
            # 取最近 3 条公告
            for _, row in df.head(3).iterrows():
                title = str(row.get("标题", row.get("title", "")))
                date = str(row.get("日期", row.get("date", "")))
                news_list.append(f"{date} {title}")
            print(f"    公告 {len(news_list)} 条")
    except Exception as e:
        print(f"    公告获取失败: {e}")

    return news_list


# ========== 5. AI 分析（TokenHub OpenAI 兼容接口） ==========
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
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {HUNYUAN_API_KEY}"
        }
        payload = {
            "model": HUNYUAN_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.3,
            "max_tokens": 4096
        }

        print("正在调用AI进行分析...")
        response = requests.post(
            f"{HUNYUAN_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )
        response.raise_for_status()
        result = response.json()

        ai_report = result["choices"][0]["message"]["content"]
        print("✅ AI分析完成")
        return ai_report

    except requests.exceptions.Timeout:
        print("❌ AI接口请求超时，使用纯数据报告")
        return generate_plain_report(stocks_data)
    except requests.exceptions.HTTPError as e:
        print(f"❌ AI接口请求失败: {e}")
        return generate_plain_report(stocks_data)
    except Exception as e:
        print(f"❌ AI分析异常: {e}")
        return generate_plain_report(stocks_data)


def generate_plain_report(stocks_data):
    """AI不可用时的纯数据报告（降级方案）"""
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


# ========== 6. 推送微信（Pushplus） ==========
def send_pushplus(title, content):
    if not PUSHPLUS_TOKEN:
        print("未配置 PUSHPLUS_TOKEN，跳过推送")
        return False

    try:
        url = "https://www.pushplus.plus/send"
        data = {
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content.replace("\n", "<br>"),
            "template": "html"
        }
        response = requests.post(url, json=data, timeout=10)
        result = response.json()
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
    print("股票日报自动化系统 v2.2（纯AKShare版）")
    print(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 0. 检查配置
    print(f"\n配置检查：")
    print(f"  PUSHPLUS_TOKEN: {'✅ 已配置' if PUSHPLUS_TOKEN else '⚠️ 未配置'}")
    print(f"  HUNYUAN_API_KEY: {'✅ 已配置' if HUNYUAN_API_KEY else '⚠️ 未配置（将使用数据版）'}")

    # 1. 加载股票列表
    stocks = load_stock_list()
    if not stocks:
        print("❌ 未找到股票列表，请检查 stock_list.txt")
        sys.exit(1)

    print(f"\n共加载 {len(stocks)} 只标的：")
    for s in stocks:
        print(f"  - {s['name']}({s['code']})")

    # 2. 先批量获取全市场行情（只请求一次）
    print("\n" + "=" * 50)
    print("批量获取全市场实时行情...")
    print("=" * 50)

    spot_df = fetch_spot_data()
    if spot_df is None:
        print("❌ 全市场行情获取失败，退出")
        sys.exit(1)
    print(f"✅ 获取到 {len(spot_df)} 只标的行情")

    # 3. 逐只采集数据
    print("\n" + "=" * 50)
    print("开始逐只采集详细数据...")
    print("=" * 50)

    stocks_data = []
    for idx, s in enumerate(stocks):
        code = s["code"]
        print(f"\n[{idx+1}/{len(stocks)}] 处理 {s['name']}({code})...")

        # 实时行情（从已获取的全量数据中过滤，不再重复请求）
        row = spot_df[spot_df["代码"] == code]
        if not row.empty:
            realtime = {
                "price": float(row["最新价"].values[0]) if pd.notna(row["最新价"].values[0]) else 0.0,
                "change_pct": float(row["涨跌幅"].values[0]) if pd.notna(row["涨跌幅"].values[0]) else 0.0,
                "volume": float(row["成交额"].values[0]) / 1e8 if pd.notna(row["成交额"].values[0]) else 0.0,
            }
        else:
            print(f"    ⚠️ 未在行情列表中找到 {code}")
            realtime = {"price": 0.0, "change_pct": 0.0, "volume": 0.0}

        # 历史数据（带重试）
        history = get_history_data(code)

        # 基本面 PE/PB（带重试）
        fundamental = get_fundamental_data(code)

        # 公司公告（带重试）
        news = get_news_data(code)

        basic = {**realtime, **history, "name": s["name"], "code": code}
        stocks_data.append({
            "basic": basic,
            "fundamental": fundamental,
            "news": news
        })

        print(f"    价格:{basic['price']:.2f} 涨跌:{basic['change_pct']:+.2f}% "
              f"成交额:{basic['volume']:.2f}亿")

        # 请求间隔，避免被限
        if idx < len(stocks) - 1:
            time.sleep(AKSHARE_REQUEST_DELAY)

    # 4. AI 分析
    print("\n" + "=" * 50)
    print("开始AI分析...")
    print("=" * 50)

    report = call_ai_analysis(stocks_data)

    # 5. 推送
    print("\n" + "=" * 50)
    print("推送结果...")
    print("=" * 50)

    today = datetime.now().strftime("%Y-%m-%d")
    send_pushplus(f"📈 股票日报 {today}", report)

    # 6. 输出报告
    print("\n" + report)

    print("\n" + "=" * 50)
    print("=== 任务完成 ===")
    print("=" * 50)


if __name__ == "__main__":
    main()
