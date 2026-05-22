import os
import akshare as ak
import tushare as ts
import requests
import pandas as pd
from datetime import datetime, timedelta

# ========== 配置区（从 GitHub Secrets 读取） ==========
WORKBUDDY_API_KEY = os.environ.get("WORKBUDDY_API_KEY", "")
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")

# 初始化 Tushare
if TUSHARE_TOKEN:
    ts.set_token(TUSHARE_TOKEN)
    pro = ts.pro_api()
else:
    pro = None

# 股票列表
def load_stock_list():
    stocks = []
    with open("stock_list.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                code, name = line.split()
                stocks.append({"code": code, "name": name})
    return stocks

# ========== 1. 免费数据：AKShare/Tushare 获取行情、历史、估值 ==========
def get_stock_basic_data(code, name):
    data = {
        "name": name,
        "code": code,
        "price": 0.0,
        "change_pct": 0.0,
        "volume": 0.0,
        "high3y": 0.0,
        "low3y": 0.0,
        "avg3y": 0.0,
        "pe": "暂无数据",
        "pb": "暂无数据",
        "industry_pe": "暂无数据"
    }
    try:
        # 实时行情（AKShare）
        spot = ak.stock_zh_a_spot_em()
        row = spot[spot["代码"] == code]
        if not row.empty:
            data["price"] = float(row["最新价"].values[0])
            data["change_pct"] = float(row["涨跌幅"].values[0])
            data["volume"] = float(row["成交额"].values[0]) / 1e8  # 亿

        # 近3年历史（AKShare）
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=365*3)).strftime("%Y%m%d")
        hist = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end)
        if not hist.empty:
            data["high3y"] = hist["最高"].max()
            data["low3y"] = hist["最低"].min()
            data["avg3y"] = hist["收盘"].mean()

        # PE/PB（Tushare 免费版）
        if pro:
            # 日线
            daily = pro.daily(ts_code=code+".SH" if code.startswith("6") else code+".SZ", start_date=start, end_date=end)
            if not daily.empty:
                data["pe"] = daily["pe"].iloc[-1] if "pe" in daily.columns else "暂无数据"
                data["pb"] = daily["pb"].iloc[-1] if "pb" in daily.columns else "暂无数据"
    except Exception as e:
        print(f"获取 {code} 基础数据失败: {e}")
    return data

# ========== 2. WorkBuddy 生成摘要/结论（仅这部分耗积分） ==========
def workbuddy_analyze(data):
    if not WORKBUDDY_API_KEY:
        return {"summary": "API Key未配置", "news": "暂无数据", "decision": "持有观望"}

    prompt = f"""
#人设#
你是专业股票情报分析助手，务实、输出有价值判断，不模棱两可。

#任务#
根据以下**已获取的客观数据**，生成3段内容（每段≤50字）：
1. 基本面摘要：一句话核心判断
2. 近期要点：公告/新闻/行业热点
3. 投资决策：仅输出 分批布局 / 持有观望 / 减仓处理，不带建议

#数据#
【股票】{data['name']}({data['code']})
【现价】{data['price']}元 | 涨跌幅：{data['change_pct']}% | 成交额：{data['volume']}亿
【历史位置】近3年最高{data['high3y']}、最低{data['low3y']}、均价{data['avg3y']}
【估值】PE {data['pe']} | PB {data['pb']} | 行业PE {data['industry_pe']}

#约束#
- 严禁编造数据
- 结论必须明确，不带模糊词
- 每段≤50字
    """.strip()

    try:
        url = "https://api.workbuddy.com/v1/chat"
        headers = {"Authorization": f"Bearer {WORKBUDDY_API_KEY}", "Content-Type": "application/json"}
        body = {"model": "gpt-3.5-turbo", "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
        res = requests.post(url, json=body, timeout=30)
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"].strip()
        # 拆分三段
        parts = content.split("\n")
        return {
            "summary": parts[0].strip() if len(parts)>0 else "暂无摘要",
            "news": parts[1].strip() if len(parts)>1 else "暂无要点",
            "decision": parts[2].strip() if len(parts)>2 else "持有观望"
        }
    except Exception as e:
        print(f"WorkBuddy 分析失败: {e}")
        return {"summary": "分析失败", "news": "分析失败", "decision": "持有观望"}

# ========== 3. 生成报告文本 ==========
def generate_report(stocks_data):
    report = "📈 自选标的日报（按决策排序）\n"
    # 排序：分批布局在前
    sorted_list = sorted(stocks_data, key=lambda x: 0 if x["ai"]["decision"]=="分批布局" else 1 if x["ai"]["decision"]=="持有观望" else 2)
    for item in sorted_list:
        d = item["basic"]
        ai = item["ai"]
        # 颜色标签
        if ai["decision"] == "分批布局":
            tag = "🟢 分批布局"
        elif ai["decision"] == "持有观望":
            tag = "🟡 持有观望"
        else:
            tag = "🔴 减仓处理"
        report += f"""
{tag}
【{d['name']}·{d['code']}】
【最新价】：{d['price']:.2f} 元 | 涨跌幅：{d['change_pct']:+.2f}% | 成交额：{d['volume']:.2f} 亿
【历史位置】：近3年最高{d['high3y']:.2f} / 最低{d['low3y']:.2f} / 均价{d['avg3y']:.2f}
【估值参考】：PE {d['pe']} | PB {d['pb']}（行业平均PE {d['industry_pe']}）
【基本面摘要】：{ai['summary']}
【近期要点】：{ai['news']}
【投资决策】：{ai['decision']}
        """.strip() + "\n"
    report += "\n⚠️ 免责声明：本内容仅为数据整理与AI生成摘要，不构成任何投资建议。股市有风险，投资需谨慎。"
    return report

# ========== 4. Pushplus 推微信 ==========
def send_pushplus(title, content):
    if not PUSHPLUS_TOKEN:
        print("Pushplus Token未配置，跳过推送")
        return
    try:
        url = "https://www.pushplus.plus/send"
        data = {"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"}
        res = requests.post(url, json=data, timeout=10)
        res.raise_for_status()
        print("Pushplus 推送成功")
    except Exception as e:
        print(f"Pushplus 推送失败: {e}")

# ========== 主流程 ==========
def main():
    print("=== 开始生成股票日报 ===")
    stocks = load_stock_list()
    stocks_data = []
    for s in stocks:
        print(f"处理 {s['name']}({s['code']})...")
        basic = get_stock_basic_data(s["code"], s["name"])
        ai = workbuddy_analyze(basic)
        stocks_data.append({"basic": basic, "ai": ai})
    report = generate_report(stocks_data)
    print(report)
    send_pushplus("📈 股票日报", report)
    print("=== 日报生成完成 ===")

if __name__ == "__main__":
    main()
