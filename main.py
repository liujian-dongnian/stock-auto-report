"""
股票日报自动化系统 v2.4
架构：新浪财经(行情) → TokenHub AI(基本面+公告+分析) → Pushplus(微信推送)
运行环境：GitHub Actions (ubuntu-latest, Python 3.10+)

v2.4 核心变更：
  - 完全移除 AKShare（GitHub Actions 境外 IP 被东方财富限制）
  - 新浪财经获取行情 + 日K历史（一次请求搞定，稳定不卡）
  - 基本面(PE/PB/行业PE)、公司公告、基本面摘要、近期要点 → 全部由 AI 补充
  - 大幅减少网络请求次数（从30+次降到10次以内）
  - 运行更快、更稳定、信息更丰富
"""

import os
import sys
import time
import requests
from datetime import datetime, timedelta

# ========== 配置区 ==========
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")
HUNYUAN_API_KEY = os.environ.get("HUNYUAN_API_KEY", "")
HUNYUAN_BASE_URL = "https://tokenhub.tencentmaas.com/v1"
HUNYUAN_MODEL = "deepseek-v4-flash"

# ========== 股票列表 ==========
def load_stock_list():
    stocks = []
    try:
        with open("stock_list.txt", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and " " in line:
                    code, name = line.split(maxsplit=1)
                    stocks.append({"code": code.strip(), "name": name.strip()})
    except Exception as e:
        print(f"读取股票列表失败: {e}")
    return stocks


# ========== 工具函数 ==========

def to_sina_code(code: str) -> str:
    """纯数字代码 → 新浪格式：sh600519 / sz000001"""
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


def retry_request(url, headers, max_retries=3, delay=3, timeout=15):
    """带重试的 HTTP GET"""
    last_err = None
    for i in range(1, max_retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
            last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)
        if i < max_retries:
            print(f"      第{i}次失败: {last_err}，{delay}秒后重试...")
            time.sleep(delay)
    print(f"      重试{max_retries}次均失败: {last_err}")
    return None


# ========== 1. 新浪财经：实时行情（批量，一次搞定） ==========

def get_sina_realtime_batch(codes):
    """
    新浪财经批量获取实时行情
    返回 dict: {code: {name, price, change_pct, volume, high, low, pre_close}}
    """
    result = {}
    sina_codes = [to_sina_code(c) for c in codes]
    url = "http://hq.sinajs.cn/list=" + ",".join(sina_codes)
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    resp = retry_request(url, headers)
    if resp is None:
        return result

    for line in resp.text.strip().split("\n"):
        if not line or "=" not in line:
            continue
        try:
            data_part = line.split('"')[1]
            if not data_part:
                continue

            code_raw = line.split("=")[0].replace("var hq_str_", "")
            code = code_raw[2:]

            fields = data_part.split(",")
            name = fields[0]
            pre_close = safe_float(fields[2])
            current = safe_float(fields[3])
            high = safe_float(fields[4])
            low = safe_float(fields[5])
            volume_amount = safe_float(fields[9])

            change_pct = (current - pre_close) / pre_close * 100 if pre_close > 0 else 0.0

            result[code] = {
                "name": name,
                "price": current,
                "pre_close": pre_close,
                "change_pct": change_pct,
                "high": high,
                "low": low,
                "volume": volume_amount / 1e8,
            }
        except (IndexError, ValueError):
            continue

    print(f"  实时行情获取到 {len(result)} 只标的")
    return result


# ========== 2. 新浪财经：日K历史数据（获取3年区间） ==========

def get_sina_daily_history(code, days=365*3):
    """
    通过新浪财经获取日K线数据
    新浪月K接口可以一次获取多年数据，用于估算3年区间
    格式：http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=36
      scale=240 → 月K线（约3年=36个月）
      scale=30 → 周K线
    """
    sina_code = to_sina_code(code)
    # 月K线，3年约36个月
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_code}&scale=240&ma=no&datalen=36"
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    resp = retry_request(url, headers)
    if resp is None:
        # 月K失败，尝试周K
        url2 = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_code}&scale=30&ma=no&datalen=160"
        resp = retry_request(url2, headers)
        if resp is None:
            return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}

    try:
        # 新浪返回JSON格式：[{"day":"2023-01","open":..,"high":..,"low":..,"close":..}, ...]
        import json
        kline_data = json.loads(resp.text.strip())
        if not kline_data:
            return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}

        highs = [safe_float(d["high"]) for d in kline_data if safe_float(d["high"]) > 0]
        lows = [safe_float(d["low"]) for d in kline_data if safe_float(d["low"]) > 0]
        closes = [safe_float(d["close"]) for d in kline_data if safe_float(d["close"]) > 0]

        if highs and lows and closes:
            return {
                "high3y": max(highs),
                "low3y": min(lows),
                "avg3y": sum(closes) / len(closes),
            }
    except Exception as e:
        print(f"      历史数据解析失败: {e}")

    return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}


# ========== 3. AI 分析（基本面+公告+投资评估，一站式） ==========

SYSTEM_PROMPT = """#人设#
你是一位专业、高效的股票情报分析助手。风格务实，输出有价值的判断，不模棱两可。你必须基于提供的真实行情数据分析，同时运用你的知识库补充基本面和近期重要信息。

#任务#
根据提供的股票行情数据，生成个股日报。你需要：
1. 基于我提供的实时行情数据（价格、涨跌幅、成交额、3年区间位置）
2. 用你的知识库补充每只股票的基本面信息（PE、PB估值、行业PE）
3. 用你的知识库补充每只股票近7天的重要公告、行业政策、热点新闻
4. 综合所有信息给出明确的投资决策和关注价位

#输出格式要求#
一、自选标的日报（按投资决策排序）
🟢 分批布局 排最前 → 🟡 持有观望 → 🔴 减仓处理

每只标的严格按以下格式输出：
【股票名称 · 代码】
【最新价】：XX.XX 元 | 涨跌幅：+X.XX% | 成交额：X.XX 亿
【历史位置】：当前价处于近3年区间的 X%（最高XX / 最低XX / 均价XX）
【估值参考】：PE XX.X | PB X.XX（行业平均PE XX）
【基本面摘要】：一句话核心判断（50字内）
【近期要点】：公司公告、行业政策、热点新闻（50字内）
【投资决策】：分批布局 / 持有观望 / 减仓处理（选一个，不带"建议"等模糊词）
【关注价位】：低于 XX 元可考虑建仓/加仓，高于 XX 元可考虑减仓

#约束#
- 行情数据（价格、涨跌幅、成交额、3年区间）必须基于我提供的真实数据，禁止编造
- 基本面信息（PE、PB、行业PE）请基于你的知识库，如果不确定标注"暂无数据"
- 近期要点请基于你的知识库中该公司的最新信息
- 如果我提供的行情数据中3年区间为0（表示获取失败），可以基于你的知识库估算
- 个股基本面和新闻分析各不超过50字
- 投资决策必须给出明确方向，不要写"可考虑""建议关注"等暧昧表述
- 报告末尾加一行：⚠️ 免责声明：本内容为行情数据整理与AI分析，不构成任何投资建议。"""


def call_ai_analysis(stocks_data):
    """调用 TokenHub AI 进行一站式分析"""
    if not HUNYUAN_API_KEY:
        print("  未配置 HUNYUAN_API_KEY，使用纯数据报告模式")
        return generate_plain_report(stocks_data)

    # 构建用户消息
    user_message = f"以下是我关注的 {len(stocks_data)} 只标的最新行情数据（数据采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}），请生成个股日报：\n\n"

    for item in stocks_data:
        d = item["basic"]
        user_message += f"【{d['name']} · {d['code']}】\n"
        user_message += f"- 最新价：{d['price']:.2f} 元\n"
        user_message += f"- 涨跌幅：{d['change_pct']:+.2f}%\n"
        user_message += f"- 成交额：{d['volume']:.2f} 亿\n"

        if d["high3y"] > 0:
            user_message += f"- 近3年最高：{d['high3y']:.2f} / 最低：{d['low3y']:.2f} / 均价：{d['avg3y']:.2f}\n"
            if d["high3y"] > d["low3y"]:
                position = (d["price"] - d["low3y"]) / (d["high3y"] - d["low3y"]) * 100
                user_message += f"- 当前价处于近3年区间的 {position:.1f}%\n"
        else:
            user_message += "- 近3年区间数据获取失败，请基于你的知识库估算\n"

        user_message += "\n"

    try:
        print("  正在调用 AI 分析...")
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
        print("  AI 分析完成")
        return ai_report
    except Exception as e:
        print(f"  AI 分析失败: {e}，使用纯数据报告")
        return generate_plain_report(stocks_data)


def generate_plain_report(stocks_data):
    """AI 不可用时的纯数据报告"""
    report = "自选标的日报（数据版，AI分析暂不可用）\n\n"
    for item in stocks_data:
        d = item["basic"]
        if d["high3y"] > 0 and d["high3y"] > d["low3y"]:
            position = (d["price"] - d["low3y"]) / (d["high3y"] - d["low3y"]) * 100
            hist_str = f"当前价处于近3年区间的 {position:.1f}%（最高{d['high3y']:.2f} / 最低{d['low3y']:.2f} / 均价{d['avg3y']:.2f}）"
        else:
            hist_str = "历史数据暂无"
        report += f"【{d['name']} · {d['code']}】\n"
        report += f"最新价：{d['price']:.2f} 元 | 涨跌幅：{d['change_pct']:+.2f}% | 成交额：{d['volume']:.2f} 亿\n"
        report += f"历史位置：{hist_str}\n"
        report += f"估值参考：PE 暂无 | PB 暂无（需AI分析）\n\n"
    report += "免责声明：本内容仅为数据整理，不构成任何投资建议。\n"
    return report


# ========== 4. 推送微信 ==========

def send_pushplus(title, content):
    if not PUSHPLUS_TOKEN:
        print("  未配置 PUSHPLUS_TOKEN，跳过推送")
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
            print("  Pushplus 推送成功")
            return True
        else:
            print(f"  Pushplus 推送失败: {result}")
            return False
    except Exception as e:
        print(f"  推送异常: {e}")
        return False


# ========== 主流程 ==========

def main():
    print("=" * 50)
    print("股票日报自动化系统 v2.4")
    print("架构：新浪财经(行情) + TokenHub AI(分析) + Pushplus(推送)")
    print(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    # 1. 加载股票列表
    stocks = load_stock_list()
    if not stocks:
        print("未找到股票列表")
        sys.exit(1)

    print(f"\n共 {len(stocks)} 只标的：")
    for s in stocks:
        print(f"  - {s['name']}({s['code']})")

    codes = [s["code"] for s in stocks]

    # 2. 新浪财经批量获取实时行情（一次请求）
    print("\n[1/3] 获取实时行情...")
    sina_data = get_sina_realtime_batch(codes)

    if not sina_data:
        print("  实时行情获取完全失败，退出")
        sys.exit(1)

    # 3. 逐只获取3年历史区间
    print("\n[2/3] 获取3年历史区间...")
    stocks_data = []
    for idx, s in enumerate(stocks):
        code = s["code"]
        print(f"  [{idx+1}/{len(stocks)}] {s['name']}({code})...")

        # 实时行情
        if code in sina_data:
            sd = sina_data[code]
            basic = {
                "name": s["name"],
                "code": code,
                "price": sd["price"],
                "change_pct": sd["change_pct"],
                "volume": sd["volume"],
                "high3y": 0.0,
                "low3y": 0.0,
                "avg3y": 0.0,
            }
            print(f"    行情: {sd['price']:.2f} {sd['change_pct']:+.2f}%")
        else:
            basic = {
                "name": s["name"],
                "code": code,
                "price": 0.0, "change_pct": 0.0, "volume": 0.0,
                "high3y": 0.0, "low3y": 0.0, "avg3y": 0.0,
            }
            print(f"    行情: 获取失败")

        # 3年历史
        history = get_sina_daily_history(code)
        basic["high3y"] = history["high3y"]
        basic["low3y"] = history["low3y"]
        basic["avg3y"] = history["avg3y"]
        if history["high3y"] > 0:
            print(f"    3年区间: {history['low3y']:.2f} ~ {history['high3y']:.2f} 均价{history['avg3y']:.2f}")
        else:
            print(f"    3年区间: 获取失败（将由AI估算）")

        stocks_data.append({"basic": basic})

        # 请求间隔（避免新浪限频）
        if idx < len(stocks) - 1:
            time.sleep(1)

    # 4. AI 分析 + 推送
    print("\n[3/3] AI 分析 & 推送...")
    report = call_ai_analysis(stocks_data)
    today = datetime.now().strftime("%Y-%m-%d")
    send_pushplus(f"股票日报 {today}", report)

    print("\n" + report)
    print("\n" + "=" * 50)
    print("=== 任务完成 ===")
    print("=" * 50)


if __name__ == "__main__":
    main()
