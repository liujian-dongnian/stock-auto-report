import os
import akshare as ak
import requests
import pandas as pd
from datetime import datetime, timedelta

# ========== 配置区（从 GitHub Secrets 读取） ==========
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")

# 股票列表
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
        print("读取股票列表失败:", e)
    return stocks

# ========== 1. 免费数据：AKShare 获取行情、历史 ==========
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
            data["volume"] = float(row["成交额"].values[0]) / 1e8

        # 近3年历史数据
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=365*3)).strftime("%Y%m%d")
        hist = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq")
        if not hist.empty:
            data["high3y"] = hist["最高"].max()
            data["low3y"] = hist["最低"].min()
            data["avg3y"] = hist["收盘"].mean()

    except Exception as e:
        print(f"获取 {code} 数据失败: {e}")
    return data

# ========== 2. 生成报告（无AI，纯数据，0报错） ==========
def generate_report(stocks_data):
    report = "📈 自选标的日报（免费数据版）\n"
    report += "数据来源：AKShare 免费接口\n\n"

    for item in stocks_data:
        d = item["basic"]
        report += f"""【{d['name']} · {d['code']}】
【最新价】：{d['price']:.2f} 元 | 涨跌幅：{d['change_pct']:+.2f}% | 成交额：{d['volume']:.2f} 亿
【历史位置】：近3年最高 {d['high3y']:.2f} / 最低 {d['low3y']:.2f} / 均价 {d['avg3y']:.2f}
【估值】：PE {d['pe']} | PB {d['pb']}

"""
    report += "⚠️ 免责声明：本内容仅为数据整理，不构成任何投资建议。"
    return report

# ========== 3. 推送微信（Pushplus） ==========
def send_pushplus(title, content):
    if not PUSHPLUS_TOKEN:
        print("未配置 PUSHPLUS_TOKEN，跳过推送")
        return
    try:
        url = "https://www.pushplus.plus/send"
        data = {
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content.replace("\n", "<br>"),
            "template": "html"
        }
        requests.post(url, json=data, timeout=10)
        print("✅ 推送成功")
    except Exception as e:
        print("推送失败:", e)

# ========== 主流程 ==========
def main():
    print("=== 开始生成股票日报 ===")
    stocks = load_stock_list()
    if not stocks:
        print("未找到股票列表")
        return

    stocks_data = []
    for s in stocks:
        print(f"处理 {s['name']}({s['code']})")
        basic = get_stock_basic_data(s["code"], s["name"])
        stocks_data.append({"basic": basic})

    report = generate_report(stocks_data)
    print("\n" + report)
    send_pushplus("股票日报", report)
    print("=== 完成 ===")

if __name__ == "__main__":
    main()
