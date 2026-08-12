import json, os, glob, pathlib
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Request, Header, HTTPException, status
from google import genai
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import httpx
import tempfile
import uvicorn  # 補上 uvicorn

# 設定 Google AI API 金鑰
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
system_instruction = "你是投信分析師，請使用繁體中文2000字以內，分項說明公司股市價量表現、融資融卷、內外資進出及財務資訊，並分析近期公司股市展望給投資人具體的專業建議!"
thinking_config = genai.types.ThinkingConfig(thinking_budget=0) 
generation_config = genai.types.GenerateContentConfig(
    max_output_tokens=5000, 
    temperature=0.1, 
    top_p=0.2,
    thinking_config=thinking_config,
    system_instruction=system_instruction
)

# 設定 Line Bot 的 API 金鑰和秘密金鑰
line_bot_api = LineBotApi(os.environ["CHANNEL_ACCESS_TOKEN"])
line_handler = WebhookHandler(os.environ["CHANNEL_SECRET"])

working_status = os.getenv("DEFAULT_TALKING", default="true").lower() == "true"

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

# 修改點 1：直接在 Webhook 內 synchronous 處理 handler，不使用 BackgroundTasks
@app.post("/webhook")
async def webhook(
    request: Request,
    x_line_signature=Header(None),
):
    body = await request.body()
    try:
        line_handler.handle(body.decode("utf-8"), x_line_signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "ok"

@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    global working_status
    
    user_msg = event.message.text.strip()
    
    if user_msg == "再見":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="Bye!"))
        return
       
    if working_status:
        try: 
            question = user_msg
            doc_url = f"https://www.twse.com.tw/pdf/ch/{question}_ch.pdf"
            
            # 修改點 2：加入 User-Agent 避免證交所阻擋爬蟲
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            with httpx.Client(timeout=10.0, follow_redirects=True) as http_client:
                doc_data = http_client.get(doc_url, headers=headers)
            
            if doc_data.status_code != 200:
                out = '查無股票代號或無法取得 PDF！請輸入台灣上市股票代號（例如：2330）！'
            else:
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
                    temp_file.write(doc_data.content)
                    temp_file_path = temp_file.name
                
                # 上傳至 Gemini
                sample_doc = client.files.upload(file=temp_file_path)
                prompt = "請給專業建議!"               
                
                completion = client.models.generate_content(
                    model="gemini-3-flash-preview",  # 請確保模型名稱與權限正確
                    contents=[sample_doc, prompt],
                    config=generation_config
                )
                out = completion.text
                
                # 刪除臨時檔案
                os.remove(temp_file_path)
                
        except Exception as e:
            out = f"Gemini 執行出錯：{str(e)}" 

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=out)
        )

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7860, reload=True)
