import os, uvicorn
from fastapi import FastAPI, Request, Header, HTTPException
from google import genai
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf

# 設定 API 金鑰
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

system_instruction = (
    "你是專業投信分析師。請依據提供的最新市場數據（包含收盤價、漲跌幅、近期走勢等），"
    "使用繁體中文（800字以內），分項說明該公司的股市價量表現、籌碼面、財務概況，"
    "並給予投資人具體專業的展望建議。"
)

generation_config = genai.types.GenerateContentConfig(
    max_output_tokens=2500,
    temperature=0.2,
    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
    system_instruction=system_instruction
)

line_bot_api = LineBotApi(os.environ["CHANNEL_ACCESS_TOKEN"])
line_handler = WebhookHandler(os.environ["CHANNEL_SECRET"])

app = FastAPI()

def get_stock_data(stock_id: str):
    """
    自動嘗試 上市(.TW) 與 上櫃(.TWO) 股票代碼
    """
    for suffix in [".TW", ".TWO"]:
        ticker_symbol = f"{stock_id}{suffix}"
        ticker = yf.Ticker(ticker_symbol)
        
        # 抓取近 5 日歷史股價資訊
        hist = ticker.history(period="5d")
        if not hist.empty:
            info = ticker.info
            latest = hist.iloc[-1]
            prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else latest['Open']
            
            close_price = round(latest['Close'], 2)
            change = round(close_price - prev_close, 2)
            change_percent = round((change / prev_close) * 100, 2)
            volume = int(latest['Volume'])
            
            market_type = "上市" if suffix == ".TW" else "上櫃"
            
            stock_summary = f"""
            股票代號/名稱：{stock_id} ({info.get('shortName', '台股個股')}) [{market_type}]
            最新交易日：{latest.name.strftime('%Y-%m-%d')}
            收盤價：{close_price} 元
            漲跌：{'+' if change > 0 else ''}{change} 元 ({'+' if change_percent > 0 else ''}{change_percent}%)
            成交量：{volume:,} 股
            近5日收盤價走勢：{list(round(hist['Close'], 2))}
            公司基本面概要：{info.get('longBusinessSummary', '無詳細簡介')}
            """
            return stock_summary
    return None

@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    if user_msg == "再見":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Bye!"))
        return

    try:
        # 假設使用者輸入 4 位數股票代號（例如：2330 或 8454）
        stock_data = get_stock_data(user_msg)
        
        if not stock_data:
            out = f"查無股票代號【{user_msg}】！請確認是否輸入正確的台灣上市/上櫃代號（例如：2330 或 8454）。"
        else:
            prompt = f"請根據以下最新個股市場資料進行剖析並給予建議：\n\n{stock_data}"
            
            completion = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=generation_config
            )
            out = completion.text
            
    except Exception as e:
        out = f"分析執行失敗，原因：{str(e)}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=out)
    )
