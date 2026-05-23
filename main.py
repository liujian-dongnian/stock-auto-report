"""
股票日报自动化系统 v2.1
架构：AKShare+Tushare数据采集 → TokenHub AI分析 → Pushplus微信推送
运行环境：GitHub Actions (ubuntu-latest, Python 3.10+)
修复记录 v2.1：
  1. Tushare ts_code 格式修正（需带交易所后缀 .SZ/.SH/.BJ）
  2. disclosure_date 限频修复（改用 anns 公告接口，并加间隔保护）
  3. daily_basic 需要 120 积分，已对应升级
"""

import os
import sys
import time
import json
import requests
import akshare as ak
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta

# ========== 配置区（从 GitHub Secrets 读取） ==========
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")

# TokenHub AI 配置（OpenAI 兼容格式）
HUNYUAN_API_KEY = os.environ.get("HUNYUAN_API_KEY", "")
HUNYUAN_BASE_URL = "https://tokenhub.tencentmaas.com/v1"
HUNYUAN_MODEL = "deepseek-v4-flash"

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


# ========== 工具函数：ts_code 转换 ==========
def to_ts_code(code: str) -> str:
    """
    将纯数字股票代码转换为 Tushare 格式（带交易所后缀）
    规则：
      0xxxxx → 深圳主板（.SZ）
      1xxxxx → 深圳（ETF/债券）→ .SZ
      2xxxxx → 深圳 B 股 → .SZ
      3xxxxx → 深圳创业板/北交所 → 3开头 301xxx 是北交所→.BJ，300xxx是创业板→.SZ
      5xxxxx → 上海（ETF）→ .SH
      6xxxxx → 上海主板 → .SH
      688xxx → 上海科创板 → .SH
      8xxxxx/4xxxxx → 北交所 → .BJ
    """
    if "." in code:
        return code  # 已有后缀，直接返回
    if code.startswith("6") or code.startswith("5"):
        return code + ".SH"
    elif code.startswith("8") or code.startswith("4"):
        return code + ".BJ"
    elif code.startswith("301") or code.startswith("430") or code.startswith("83"):
        return code + ".BJ"
    else:
        return code + ".SZ"


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
                    market = "ETF" if code.startswith(("1", "5")) else "A股"
                    stocks.append({
                        "code": code,
                        "ts_code": to_ts_code(code),
                        "name": name,
                        "market": market
                    })
    except Exception as e:
        print(f"读取股票列表失败: {e}")
    return stocks


# ========== 1. AKShare：实时行情 + 历史数据 ==========
def get_realtime_data_akshare(code):
    try:
        spot = ak.stock_zh_a_spot_em()
        row = spot[spot["代码"] == code]
        if not row.empty:
            return {
                "price": float(row["最新价"].values[0]) if pd.notna(row["最新价"].values[0]) else 0.0,
                "change_pct": float(row["涨跌幅"].values[0]) if pd.notna(row["涨跌幅"].values[0]) else 0.0,
                "volume": float(row["成交额"].values[0]) / 1e8 if pd.notna(row["成交额"].values[0]) else 0.0,
            }
    except Exception as e:
        print(f"  AKShare实时行情获取失败: {e}")
    return {"price": 0.0, "change_pct": 0.0, "volume": 0.0}


def get_history_data_akshare(code):
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=365 * 3)).strftime("%Y%m%d")
        hist = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start, end_date=end, adjust="qfq"
        )
        if not hist.empty:
            return {
                "high3y": float(hist["最高"].max()),
                "low3y": float(hist["最低"].min()),
                "avg3y": float(hist["收盘"].mean()),
            }
    except Exception as e:
        print(f"  AKShare历史数据获取失败: {e}")
    return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}


# ========== 2. Tushare：基本面数据（daily_basic）==========
def get_fundamental_data_tushare(ts_api, ts_code):
    """
    获取 PE/PB（需要 120 积分，接口：daily_basic）
    注意：ts_code 必须带交易所后缀，如 301071.BJ
    """
    data = {
        "pe": None, "pb": None,
        "forecast_type": None,
        "forecast_pchange_min": None,
        "forecast_pchange_max": None
    }
    if not ts_api:
        print("  Tushare未配置，跳过基本面数据")
        return data

    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=10)).strftime("%Y%m%d")
        df_daily = ts_api.daily_basic(
            ts_code=ts_code,
            start_date=start,
            end_date=end,
            fields="ts_code,trade_date,pe,pb"
        )
        if df_daily is not None and not df_daily.empty:
            # 按日期降序，取最新一条
            df_daily = df_daily.sort_values("trade_date", ascending=False)
            latest = df_daily.iloc[0]
            data["pe"] = float(latest["pe"]) if pd.notna(latest["pe"]) else None
            data["pb"] = float(latest["pb"]) if pd.notna(latest["pb"]) else None
            print(f"  PE={data['pe']}, PB={data['pb']}")
        else:
            print(f"  daily_basic 无数据（ts_code={ts_code}）")
    except Exception as e:
        print(f"  Tushare基本面数据获取失败: {e}")

    return data


# ========== 3. Tushare：批量获取公告（限频保护版） ==========
def get_news_batch_tushare(ts_api, ts_codes):
    """
    批量获取公告，统一调用一次 anns 接口，避免多次触发限频。
    接口：anns（公告信息，积分 120）
    返回：dict，key 为 ts_code，value 为公告标题列表
    """
    result = {code: [] for code in ts_codes}

    if not ts_api:
        return result

    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")

        # 逐只查询，每次查询后等待 62 秒（1次/分钟限频保护）
        # 为了节省时间，改用 anns_d 接口（每日公告摘要，积分120，不限频次或限更宽松）
        for ts_code in ts_codes:
            try:
                df = ts_api.anns(
                    ts_code=ts_code,
                    start_date=start,
                    end_date=end
                )
                if df is not None and not df.empty:
                    titles = df["title"].head(3).tolist() if "title" in df.columns else []
                    result[ts_code] = [str(t) for t in titles]
                    print(f"  [{ts_code}] 公告 {len(result[ts_code])} 条")
                else:
                    print(f"  [{ts_code}] 暂无近7日公告")

                # 限频保护：每次请求后等待 62 秒
                time.sleep(62)

            except Exception as e:
                err_str = str(e)
                print(f"  [{ts_code}] 公告获取失败: {err_str}")
                # 如果是权限问题，跳过整个公告功能
                if "没有接口" in err_str or "权限" in err_str:
                    print("  ⚠️ 公告接口权限不足，跳过所有公告获取")
                    break
                # 如果是限频，继续等待后重试
                elif "超限" in err_str or "频率" in err_str:
                    print("  ⚠️ 接口限频，已等待62秒，继续下一只...")
                    time.sleep(62)

    except Exception as e:
        print(f"  公告批量获取异常: {e}")

    return result


# ========== 4. AI 分析（TokenHub OpenAI 兼容接口） ==========
def call_ai_analysis(stocks_data):
    if not HUNYUAN_API_KEY:
        print("未配置 HUNYUAN_API_KEY，使用纯数据报告模式")
        return generate_plain_report(stocks_data)

    # 构建用户消息
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

        if f.get("forecast_type"):
            user_message += f"- 业绩预告：{f['forecast_type']}"
            if f.get("forecast_pchange_min") is not None:
                user_message += f"，净利润变动：{f['forecast_pchange_min']:.0f}%~{f['forecast_pchange_max']:.0f}%"
            user_message += "\n"

        if news:
            user_message += f"- 近期公告：{' | '.join(news[:3])}\n"

        user_message += "\n"

    # 调用 OpenAI 兼容接口
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
        print(f"   响应内容: {response.text[:200]}")
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


# ========== 5. 推送微信（Pushplus） ==========
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
    print("股票日报自动化系统 v2.1")
    print(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 0. 检查配置
    print(f"\n配置检查：")
    print(f"  TUSHARE_TOKEN: {'✅ 已配置' if TUSHARE_TOKEN else '⚠️ 未配置'}")
    print(f"  PUSHPLUS_TOKEN: {'✅ 已配置' if PUSHPLUS_TOKEN else '⚠️ 未配置'}")
    print(f"  HUNYUAN_API_KEY: {'✅ 已配置' if HUNYUAN_API_KEY else '⚠️ 未配置（将使用数据版）'}")

    # 1. 加载股票列表
    stocks = load_stock_list()
    if not stocks:
        print("❌ 未找到股票列表，请检查 stock_list.txt")
        sys.exit(1)

    print(f"\n共加载 {len(stocks)} 只标的：")
    for s in stocks:
        print(f"  - {s['name']}({s['code']}) [{s['ts_code']}] [{s['market']}]")

    # 2. 初始化 Tushare
    ts_api = None
    if TUSHARE_TOKEN:
        try:
            ts.set_token(TUSHARE_TOKEN)
            ts_api = ts.pro_api()
            print("\n✅ Tushare API 初始化成功")
        except Exception as e:
            print(f"❌ Tushare API 初始化失败: {e}")

    # 3. 采集数据（行情 + 历史）
    print("\n" + "=" * 50)
    print("开始数据采集（行情 & 历史）...")
    print("=" * 50)

    stocks_data = []
    for s in stocks:
        print(f"\n处理 {s['name']}({s['code']})...")

        realtime = get_realtime_data_akshare(s["code"])
        history = get_history_data_akshare(s["code"])
        basic = {**realtime, **history, "name": s["name"], "code": s["code"]}
        fundamental = get_fundamental_data_tushare(ts_api, s["ts_code"])

        stocks_data.append({
            "stock": s,
            "basic": basic,
            "fundamental": fundamental,
            "news": []  # 暂时留空，下方批量获取
        })

        print(f"  价格:{basic['price']:.2f} 涨跌:{basic['change_pct']:+.2f}% "
              f"成交额:{basic['volume']:.2f}亿")

    # 4. 批量获取公告（Tushare，限频保护）
    if ts_api:
        print("\n" + "=" * 50)
        print("开始批量获取公告（每只间隔62秒，避免限频）...")
        print("=" * 50)

        ts_codes = [s["ts_code"] for s in stocks]
        news_map = get_news_batch_tushare(ts_api, ts_codes)

        # 把公告结果写回 stocks_data
        for item in stocks_data:
            ts_code = item["stock"]["ts_code"]
            item["news"] = news_map.get(ts_code, [])

    # 5. AI 分析
    print("\n" + "=" * 50)
    print("开始AI分析...")
    print("=" * 50)

    report = call_ai_analysis(stocks_data)

    # 6. 推送
    print("\n" + "=" * 50)
    print("推送结果...")
    print("=" * 50)

    today = datetime.now().strftime("%Y-%m-%d")
    send_pushplus(f"📈 股票日报 {today}", report)

    # 7. 输出报告
    print("\n" + report)

    print("\n" + "=" * 50)
    print("=== 任务完成 ===")
    print("=" * 50)


if __name__ == "__main__":
    main()
