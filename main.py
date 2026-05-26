"""
股票日报自动化系统 v2.5
架构：新浪财经(行情) → TokenHub AI(个股分析+行业风向标) → Pushplus(微信推送)
运行环境：GitHub Actions (ubuntu-latest, Python 3.10+)

v2.5 新增：
  - 任务二：行业风向标（10大板块 AI 分析，识别3-6个月上涨机会）
  - 个股日报 + 行业风向标 合并为一条微信消息推送
"""

import os
import sys
import time
import json
import requests
from datetime import datetime

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


def call_ai(system_prompt, user_message, max_tokens=4096, label="AI"):
    """通用 AI 调用封装"""
    if not HUNYUAN_API_KEY:
        print(f"  未配置 HUNYUAN_API_KEY，跳过 {label}")
        return None
    try:
        print(f"  正在调用 {label}...")
        response = requests.post(
            f"{HUNYUAN_BASE_URL}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {HUNYUAN_API_KEY}"
            },
            json={
                "model": HUNYUAN_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message}
                ],
                "temperature": 0.3,
                "max_tokens": max_tokens
            },
            timeout=90
        )
        response.raise_for_status()
        result = response.json()["choices"][0]["message"]["content"]
        print(f"  {label} 完成")
        return result
    except Exception as e:
        print(f"  {label} 失败: {e}")
        return None


# ========== 1. 新浪财经：实时行情（批量） ==========

def get_sina_realtime_batch(codes):
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


# ========== 2. 新浪财经：月K历史数据（3年区间） ==========

def get_sina_daily_history(code):
    sina_code = to_sina_code(code)
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_code}&scale=240&ma=no&datalen=36"
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    resp = retry_request(url, headers)
    if resp is None:
        url2 = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sina_code}&scale=30&ma=no&datalen=160"
        resp = retry_request(url2, headers)
        if resp is None:
            return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}

    try:
        kline_data = json.loads(resp.text.strip())
        if not kline_data:
            return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}
        highs = [safe_float(d["high"]) for d in kline_data if safe_float(d["high"]) > 0]
        lows = [safe_float(d["low"]) for d in kline_data if safe_float(d["low"]) > 0]
        closes = [safe_float(d["close"]) for d in kline_data if safe_float(d["close"]) > 0]
        if highs and lows and closes:
            return {"high3y": max(highs), "low3y": min(lows), "avg3y": sum(closes) / len(closes)}
    except Exception as e:
        print(f"      历史数据解析失败: {e}")

    return {"high3y": 0.0, "low3y": 0.0, "avg3y": 0.0}


# ========== 3. 任务一：个股日报 AI 分析 ==========

SYSTEM_PROMPT_STOCKS = """#人设#
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
- 投资决策必须给出明确方向，不要写"可考虑""建议关注"等暧昧表述"""


def call_ai_stocks(stocks_data):
    """任务一：个股日报 AI 分析"""
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

    result = call_ai(SYSTEM_PROMPT_STOCKS, user_message, max_tokens=4096, label="个股日报AI")
    if result:
        return result
    return generate_plain_report(stocks_data)


def generate_plain_report(stocks_data):
    """AI 不可用时的纯数据报告"""
    report = "一、自选标的日报（数据版，AI分析暂不可用）\n\n"
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
        report += f"估值参考：PE 暂无 | PB 暂无（AI分析不可用）\n\n"
    return report


# ========== 4. 任务二：行业风向标 AI 分析 ==========

SYSTEM_PROMPT_INDUSTRY = """#人设#
你是一位专业、高效的股票情报分析助手，专注于宏观与行业趋势研判。风格务实，输出有价值的判断，不模棱两可。

#任务#
搜索宏观与行业信息，识别未来3-6个月具有复苏/兴起/周期性上涨潜力的行业机会，判断其驱动逻辑和股价领先时间窗口，并进行优先级排序。

#关注板块#
六大核心板块：
1、大金融：银行、非银金融（证券、保险）、房地产
2、大消费：食品饮料、医药生物、家用电器、商贸零售、美容护理、社会服务
3、大科技（成长）：电子、计算机、通信、传媒、国防军工
4、大周期（资源/制造）：煤炭、石油石化、有色金属、钢铁、基础化工、建筑材料
5、高端制造：电力设备（新能源）、汽车、机械设备
6、公用事业与基建：公用事业、环保、建筑装饰、交通运输

四大概念板块：
1、科技成长：AI、算力、半导体、机器人、数字经济、信创
2、新能源：光伏、储能、锂电池、新能源车、风电
3、政策主题：中特估、一带一路、国企改革、碳中和
4、医药健康：创新药、医疗器械、CXO、中药

#每个板块需分析的内容#
1、近3天热点新闻、公司公告、行业政策（基于你的知识库）
2、根据热点，选取1个头部公司股票
3、用"预期-发酵-兑现"三阶段模型分析该公司股票走势
4、最新财报关键指标（营收增速、净利润增速、PE/PB估值）
5、近7天重大事件（公告、政策、新闻）

#输出格式#
二、行业风向标（按投资决策排序）
🟢 行业向上 / 🔴 行业向下
（"行业向上"排最前）

每个板块严格按以下格式输出：
【行业名称】：
【头部公司】：股票名称-股票代码
【当前状态】：用一句话描述所处周期位置（触底 / 磨底 / 转折 / 加速上行）
【驱动逻辑】：导致未来复苏/上涨的核心原因（供给收缩/需求回升/政策催化，50字内）
【近期要点】：近3天重要新闻/公告/政策（50字内）
【阶段判断】：当前处于"预期/发酵/兑现"哪个阶段，一句话说明
【股价领先判断】：股票通常领先基本面底部X个月见底/启动，预计股价启动窗口为YYYY年MM月前后
【投资建议】：🟢 行业向上 / 🔴 行业向下

#约束#
- 只输出具有明确上涨或下行逻辑的板块，没有明确信号的板块可不输出
- 输出板块数量控制在 5-8 个（优中选优）
- 驱动逻辑不超过50字，近期要点不超过50字
- 行业周期判断必须有数据或历史事实支撑，禁止纯靠推断编造结论
- 投资建议必须明确，不要写"可关注""可参考"等模糊词
- 最后加一行：⚠️ 免责声明：本内容为AI分析，不构成任何投资建议。"""


def call_ai_industry():
    """任务二：行业风向标 AI 分析（纯AI知识库，不需要行情数据）"""
    user_message = f"""当前日期：{datetime.now().strftime('%Y年%m月%d日')}

请基于你的知识库，对上述10大板块（六大核心+四大概念）进行行业风向标分析。

要求：
1. 识别未来3-6个月具有复苏/兴起/周期性上涨潜力的行业机会
2. 每个输出的板块必须包含完整的驱动逻辑和"预期-发酵-兑现"阶段判断
3. 按投资机会优先级从高到低排序输出（🟢行业向上排最前）
4. 总输出控制在5-8个板块（优中选优，没有明确信号的不输出）"""

    return call_ai(SYSTEM_PROMPT_INDUSTRY, user_message, max_tokens=4096, label="行业风向标AI")


# ========== 5. 推送微信 ==========

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
    print("股票日报自动化系统 v2.5")
    print("任务一：个股日报 | 任务二：行业风向标")
    print(f"运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    print(f"\n配置检查：")
    print(f"  PUSHPLUS_TOKEN: {'✅ 已配置' if PUSHPLUS_TOKEN else '⚠️ 未配置'}")
    print(f"  HUNYUAN_API_KEY: {'✅ 已配置' if HUNYUAN_API_KEY else '⚠️ 未配置'}")

    # ===== 任务一：个股日报 =====
    print("\n" + "=" * 50)
    print("【任务一】个股日报")
    print("=" * 50)

    stocks = load_stock_list()
    if not stocks:
        print("未找到股票列表，跳过个股日报")
        stocks_report = "（个股日报：股票列表读取失败）\n"
    else:
        print(f"\n共 {len(stocks)} 只标的：" + "、".join([f"{s['name']}({s['code']})" for s in stocks]))
        codes = [s["code"] for s in stocks]

        # 批量获取实时行情
        print("\n[1/2] 获取实时行情...")
        sina_data = get_sina_realtime_batch(codes)

        # 逐只获取3年历史
        print("\n[2/2] 获取3年历史区间...")
        stocks_data = []
        for idx, s in enumerate(stocks):
            code = s["code"]
            print(f"  [{idx+1}/{len(stocks)}] {s['name']}({code})...")

            if code in sina_data:
                sd = sina_data[code]
                basic = {
                    "name": s["name"], "code": code,
                    "price": sd["price"], "change_pct": sd["change_pct"],
                    "volume": sd["volume"],
                    "high3y": 0.0, "low3y": 0.0, "avg3y": 0.0,
                }
                print(f"    行情: {sd['price']:.2f} {sd['change_pct']:+.2f}%")
            else:
                basic = {
                    "name": s["name"], "code": code,
                    "price": 0.0, "change_pct": 0.0, "volume": 0.0,
                    "high3y": 0.0, "low3y": 0.0, "avg3y": 0.0,
                }
                print(f"    行情: 获取失败")

            history = get_sina_daily_history(code)
            basic.update(history)
            if history["high3y"] > 0:
                print(f"    3年区间: {history['low3y']:.2f} ~ {history['high3y']:.2f} 均{history['avg3y']:.2f}")
            else:
                print(f"    3年区间: 获取失败（AI估算）")

            stocks_data.append({"basic": basic})
            if idx < len(stocks) - 1:
                time.sleep(1)

        # 个股 AI 分析
        print("\n个股日报 AI 分析...")
        stocks_report = call_ai_stocks(stocks_data)

    # ===== 任务二：行业风向标 =====
    print("\n" + "=" * 50)
    print("【任务二】行业风向标")
    print("=" * 50)
    industry_report = call_ai_industry()
    if not industry_report:
        industry_report = "（行业风向标：AI分析暂不可用）\n"

    # ===== 合并推送 =====
    print("\n" + "=" * 50)
    print("合并推送...")
    print("=" * 50)

    today = datetime.now().strftime("%Y-%m-%d")
    full_report = (
        f"{stocks_report}\n\n"
        f"{'='*30}\n\n"
        f"{industry_report}\n\n"
        f"⚠️ 免责声明：本内容为行情数据整理与AI分析，不构成任何投资建议。"
    )

    send_pushplus(f"📈 股票日报+行业风向标 {today}", full_report)

    print("\n" + full_report)
    print("\n" + "=" * 50)
    print("=== 全部任务完成 ===")
    print("=" * 50)


if __name__ == "__main__":
    main()
