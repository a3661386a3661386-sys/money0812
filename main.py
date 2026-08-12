import os, json, re, uvicorn
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf

# 設定 API 金鑰
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# 1. 設定投信分析師 prompt
analyst_system_instruction = (
    "你是專業投信分析師。請依據提供的最新市場數據（包含收盤價、漲跌幅、近期走勢等），"
    "使用繁體中文（800字以內），分項說明該公司的股市價量表現、籌碼面、財務概況，"
    "並給予投資人具體專業的展望建議。"
)

analyst_config = genai.types.GenerateContentConfig(
    max_output_tokens=2500,
    temperature=0.2,
    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
    system_instruction=analyst_system_instruction
)

# 2. 設定自然語言意圖解析 prompt (輸出 JSON)
intent_system_instruction = """
你是一個台股意圖分析助手。請分析使用者的輸入，並回應 JSON 格式（不要加 Markdown 標記）：
{
  "stock_query": "股票代號或名稱，若沒提及填 null",
  "intent": "ONLY_PRICE (只問價格/漲跌) 或 FULL_ANALYSIS (要求分析/整體表現/展望) 或 UNKNOWN (非台股查詢)"
}
範例：
- "台積電今日收盤價" -> {"stock_query": "2330", "intent": "ONLY_PRICE"}
- "聯發科表現如何" -> {"stock_query": "2454", "intent": "FULL_ANALYSIS"}
- "8454" -> {"stock_query": "8454", "intent": "FULL_ANALYSIS"}
"""

intent_config = genai.types.GenerateContentConfig(
    temperature=0.0,
    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
    system_instruction=intent_system_instruction
)

line_bot_api = LineBotApi(os.environ["CHANNEL_ACCESS_TOKEN"])
line_handler = WebhookHandler(os.environ["CHANNEL_SECRET"])

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"title": "Line Bot"}

@app.post("/webhook")
async def webhook(request: Request, x_line_signature=Header(None)):
    body = await request.body()
    try:
        line_handler.handle(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "ok"

def parse_user_intent(user_msg: str):
    """ 利用 Gemini 將自然語言轉為結構化意圖 """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_msg,
            config=intent_config
        )
        clean_json = response.text.strip().replace("```json", "").replace("```", "")
        return json.loads(clean_json)
    except:
        # 降級處理：若純數字則當成股票代號完整分析
        if user_msg.isdigit():
            return {"stock_query": user_msg, "intent": "FULL_ANALYSIS"}
        return {"stock_query": user_msg, "intent": "FULL_ANALYSIS"}

def get_stock_data(query: str):
    """ 搜尋上市 (.TW) 及 上櫃 (.TWO) 股票資訊 """
    # 如果傳入的是中文名稱或純數字，yfinance 支援直接查詢代號
    stock_id = query.strip()
    
    for suffix in [".TW", ".TWO"]:
        ticker_symbol = f"{stock_id}{suffix}"
        ticker = yf.Ticker(ticker_symbol)
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
            stock_name = info.get('shortName', stock_id)
            
            return {
                "symbol": stock_id,
                "name": stock_name,
                "market_type": market_type,
                "date": latest.name.strftime('%Y-%m-%d'),
                "close_price": close_price,
                "change": change,
                "change_percent": change_percent,
                "volume": volume,
                "hist_close": list(round(hist['Close'], 2)),
                "summary": info.get('longBusinessSummary', '無詳細資料')
            }
    return None

@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_msg = event.message.text.strip()
    
    if user_msg == "再見":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Bye!"))
        return

    try:
        # 1. 判斷使用者意圖與提取個股名稱/代號
        intent_result = parse_user_intent(user_msg)
        stock_query = intent_result.get("stock_query")
        intent = intent_result.get("intent")

        if not stock_query or intent == "UNKNOWN":
            out = "請輸入您想查詢的台灣上市/上櫃股票代號或名稱（例如：「台積電今日收盤價」或「2330 展望」）！"
        else:
            # 2. 獲取股票當日與歷史資料
            stock_info = get_stock_data(stock_query)
            
            if not stock_info:
                out = f"查無【{stock_query}】的股票資訊，請確認代號或名稱是否正確（支援上市與上櫃股票）。"
            else:
                # 3. 根據意圖做出針對性回應
                sign = '+' if stock_info['change'] > 0 else ''
                
                if intent == "ONLY_PRICE":
                    # 精準回應價格，並在最後導引問詢
                    out = (
                        f"📊 【{stock_info['name']} ({stock_info['symbol']})】{stock_info['market_type']}\n"
                        f"📅 日期：{stock_info['date']}\n"
                        f"💰 收盤價：{stock_info['close_price']} 元\n"
                        f"📈 漲跌：{sign}{stock_info['change']} 元 ({sign}{stock_info['change_percent']}%)\n"
                        f"📦 成交量：{stock_info['volume']:,} 股\n\n"
                        f"💡 想進一步了解 {stock_info['symbol']} 近期的個股狀況與投信展望分析嗎？\n"
                        f"請回覆：「分析 {stock_info['symbol']}」或「{stock_info['symbol']} 近況」！"
                    )
                else:
                    # 進行完整的投信分析報告
                    prompt_data = (
                        f"股票代號/名稱：{stock_info['symbol']} ({stock_info['name']}) [{stock_info['market_type']}]\n"
                        f"最新交易日：{stock_info['date']}\n"
                        f"收盤價：{stock_info['close_price']} 元\n"
                        f"漲跌：{sign}{stock_info['change']} 元 ({sign}{stock_info['change_percent']}%)\n"
                        f"成交量：{stock_info['volume']:,} 股\n"
                        f"近5日收盤價走勢：{stock_info['hist_close']}\n"
                        f"公司基本面概要：{stock_info['summary']}"
                    )
                    prompt = f"請根據以下最新個股市場資料進行剖析並給予專業投資建議：\n\n{prompt_data}"
                    
                    completion = client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                        config=analyst_config
                    )
                    out = completion.text

    except Exception as e:
        out = f"系統處理出錯：{str(e)}"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=out)
    )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=True)
