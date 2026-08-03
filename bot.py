import os
import asyncio
import re
import threading
import datetime
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from newspaper import Article, Config
from bs4 import BeautifulSoup
import requests
from flask import Flask
from notion_client import AsyncClient

# 1. 환경변수 로드
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
if NOTION_DATABASE_ID:
    # URL 전체를 복사했거나 공백이 들어간 경우를 대비해 순수 ID만 추출
    import re
    _match = re.search(r'([a-fA-F0-9]{32}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12})', NOTION_DATABASE_ID)
    if _match:
        NOTION_DATABASE_ID = _match.group(1).replace('-', '')
    else:
        NOTION_DATABASE_ID = NOTION_DATABASE_ID.split("?")[0].split("/")[-1].strip()

# 노션 클라이언트 초기화
notion = AsyncClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

# 2. Gemini API 설정
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-lite-latest')

def summarize_with_gemini(text, user_text):
    """Gemini를 이용해 뉴스 본문을 요약하는 함수"""
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    prompt = f"""
다음 뉴스 기사(또는 텍스트)를 읽고 아래 양식에 정확히 맞춰서 요약해 줘.

[사용자 입력 텍스트]: {user_text}

[기사 원문]:
{text[:4000]}

[요약 양식]
**📌 제목:** (기사의 핵심을 나타내는 제목. 본문에 제목이 있으면 쓰고, 없으면 만들어줘)
**📅 날짜:** (원문에서 발행일을 찾아 'YYYY-MM-DD' 형식으로 적어줘. 단, 날짜를 찾을 수 없거나 사용자가 링크 없이 텍스트만 직접 입력한 경우, 요크에게 이 글을 전달한 오늘 날짜인 '{today_str}'를 무조건 적어줘.)
**📌 3줄 요약:**
1. 
2. 
3. 

**🔑 핵심 키워드:** (반드시 다음 카테고리 중에서 기사에 맞는 1~2개만 골라서 해시태그로 적어줘: #거시경제 #기업_실적 #증시_시황 #부동산 #반도체_IT #가상화폐 #환율_원자재 #정책_규제 #기타)
"""
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"요약 중 오류가 발생했습니다: {e}"

async def save_to_notion(title, summary, url, date_str=None):
    """요약된 내용을 노션 데이터베이스에 저장하는 함수"""
    if not notion or not NOTION_DATABASE_ID:
        return "노션 API 설정이 누락되어 저장되지 않았습니다."
        
    try:
        # 요약본에서 #키워드 추출
        tags = re.findall(r'#([^\s#]+)', summary)
        
        # 노션 API에 보낼 속성(properties) 구성
        properties = {
            "제목": {
                "title": [{"text": {"content": title[:2000]}}]
            },
            "요약": {
                "rich_text": [{"text": {"content": summary[:2000]}}]
            }
        }
        
        if url:
            properties["링크"] = {"url": url}
            
        if date_str:
            properties["날짜"] = {
                "date": {"start": date_str}
            }
            
        if tags:
            # 다중 선택(Multi-select) 형식에 맞게 변환 (최대 100개 옵션)
            properties["키워드"] = {
                "multi_select": [{"name": tag[:100]} for tag in tags[:10]]
            }
            
        await notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=properties
        )
        return "✅ 노션에 성공적으로 저장되었습니다!"
    except Exception as e:
        return f"❌ 노션 저장 실패: {e}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id != ALLOWED_CHAT_ID:
        await update.message.reply_text("이 봇을 사용할 권한이 없습니다.")
        return

    user_text = update.message.text
    processing_msg = await update.message.reply_text("⏳ 분석 및 요약 중입니다... 잠시만 기다려주세요.")
    
    # URL 추출 정규식
    urls = re.findall(r'(https?://[^\s]+)', user_text)
    
    text_to_summarize = ""
    title = "직접 입력한 텍스트"
    url = ""
    
    if urls:
        url = urls[0]
        try:
            # 봇 차단(403) 우회를 위해 User-Agent 설정
            config = Config()
            config.browser_user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            config.request_timeout = 10
            
            try:
                article = Article(url, config=config, language='ko')
                article.download()
                article.parse()
                title = article.title
                text_to_summarize = article.text
            except Exception:
                title = ""
                text_to_summarize = ""
            
            # newspaper3k가 실패했거나 내용이 빈약한 경우 (네이버 뉴스 등 방어된 사이트)
            if not text_to_summarize or len(text_to_summarize) < 100:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
                }
                res = requests.get(url, headers=headers, timeout=10)
                res.raise_for_status()
                soup = BeautifulSoup(res.text, 'html.parser')
                
                # 네이버 뉴스 등 한국 기사 사이트의 주요 본문 영역 추출 시도
                article_body = soup.find('div', {'id': 'dic_area'}) or \
                               soup.find('article') or \
                               soup.find('div', class_='article_body') or \
                               soup.find('div', id='newsView') or \
                               soup.find('div', id='news_body_id') or \
                               soup.find('div', class_='news_body') or \
                               soup.body
                               
                if article_body:
                    text_to_summarize = article_body.get_text(separator=' ', strip=True)
                    if not title or len(title) < 2:
                        title_tag = soup.find('title')
                        if title_tag:
                            title = title_tag.get_text(strip=True)
                
            if not text_to_summarize or len(text_to_summarize) < 50:
                await processing_msg.edit_text("기사 본문을 추출하지 못했습니다. 보안이 강한 사이트이거나 이미지 위주의 기사일 수 있습니다. 본문을 직접 복사해서 보내주시면 요약해 드립니다!")
                return
                
        except Exception as e:
            await processing_msg.edit_text(f"링크 처리 중 오류가 발생했습니다: {e}\n(이 경우 기사 본문을 직접 복사해서 전송해 주세요)")
            return
    else:
        # URL이 없으면 사용자가 직접 복사해서 보낸 긴 텍스트로 간주하고 바로 요약
        if len(user_text) < 50:
            await processing_msg.edit_text("뉴스 기사 링크(URL)를 보내거나, 요약할 긴 본문을 직접 복사해서 보내주세요!")
            return
        text_to_summarize = user_text
        
    try:
        # 수집된 원본 제목이 있으면 본문에 합쳐서 Gemini가 참고하게 함
        if title and title != "직접 입력한 텍스트":
            text_to_summarize = f"[수집된 원본 제목]: {title}\n\n{text_to_summarize}"
            
        # Gemini 요약 요청 (사용자 텍스트 같이 전달)
        summary = summarize_with_gemini(text_to_summarize, user_text)
        
        # 제목 파싱
        title_for_notion = "제목 추출 실패"
        title_match = re.search(r'\*\*📌 제목:\*\*\s*(.*)', summary)
        if title_match:
            title_for_notion = title_match.group(1).strip()
            
        # 날짜 파싱 (YYYY-MM-DD 형식만 추출)
        date_for_notion = None
        date_match = re.search(r'\*\*📅 날짜:\*\*\s*([0-9]{4}-[0-9]{2}-[0-9]{2})', summary)
        if date_match:
            date_for_notion = date_match.group(1).strip()
        
        # 노션 자동 저장 시도
        notion_status = await save_to_notion(title_for_notion, summary, url, date_for_notion)
        
        final_message = summary
        if url:
            final_message += f"\n\n🔗 원문: {url}"
            
        final_message += f"\n\n{notion_status}"
            
        try:
            await processing_msg.edit_text(final_message, parse_mode='Markdown')
        except Exception:
            # 텔레그램 마크다운 문법 오류 시 일반 텍스트로 안전하게 전송
            await processing_msg.edit_text(final_message)
        
    except Exception as e:
        await processing_msg.edit_text(f"요약 중 오류가 발생했습니다.\n에러: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id != ALLOWED_CHAT_ID:
        await update.message.reply_text("이 봇을 사용할 권한이 없습니다.")
        return
    await update.message.reply_text("환영합니다! 뉴스 링크(URL)나 긴 글을 그대로 복사해서 보내주시면 AI가 3줄로 요약해 드립니다.\n(수동으로 오늘의 뉴스레터를 발행하려면 /newsletter 를 입력하세요!)")

async def command_newsletter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """사용자가 원할 때 즉시 뉴스레터를 발행하는 수동 명령어"""
    chat_id = str(update.effective_chat.id)
    if chat_id != ALLOWED_CHAT_ID:
        return
    await update.message.reply_text("수동으로 오늘의 뉴스레터 발행을 시작합니다! ⏳ (작업에 10~20초 정도 소요될 수 있습니다)")
    
    # asyncio.create_task를 통해 백그라운드 실행
    context.application.create_task(generate_newsletter(context.bot))

async def generate_newsletter(bot):
    """매일 자정에 실행되는 뉴스레터 발행 작업 (직접 봇 객체 받음)"""
    if not notion or not NOTION_DATABASE_ID:
        return
        
    chat_id = ALLOWED_CHAT_ID
    
    # 1. 오늘 날짜 구하기
    tz = pytz.timezone('Asia/Seoul')
    today = datetime.datetime.now(tz)
    today_str = today.strftime('%Y-%m-%d')
    
    await bot.send_message(chat_id=chat_id, text="🕒 자정입니다! 오늘의 뉴스를 모아 뉴스레터 발행을 시작합니다...")
    
    try:
        import httpx
        db_id = NOTION_DATABASE_ID.replace('/', '').strip() # 혹시 모를 슬래시 제거
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.notion.com/v1/databases/{db_id}/query",
                headers={
                    "Authorization": f"Bearer {NOTION_TOKEN}",
                    "Notion-Version": "2022-06-28"
                },
                json={
                    "filter": {
                        "property": "날짜",
                        "date": {
                            "equals": today_str
                        }
                    }
                }
            )
            if resp.status_code != 200:
                raise Exception(f"Notion API Error: {resp.status_code} {resp.text}")
            response = resp.json()
        
        results = response.get('results', [])
        if not results:
            await bot.send_message(chat_id=chat_id, text="오늘은 스크랩된 기사가 없어서 뉴스레터를 발행하지 않습니다. 편안한 밤 되세요! 🌙")
            return
            
        # 3. 텍스트 추출 및 병합
        news_texts = []
        for page in results:
            props = page.get('properties', {})
            
            # 뉴스레터 본인은 제외
            tags = props.get('키워드', {}).get('multi_select', [])
            is_newsletter = any(t.get('name') == '#일간뉴스레터' for t in tags)
            if is_newsletter:
                continue
                
            title_prop = props.get('제목', {}).get('title', [])
            page_title = title_prop[0]['text']['content'] if title_prop else "제목 없음"
            
            summary_prop = props.get('요약', {}).get('rich_text', [])
            page_summary = "".join([t['text']['content'] for t in summary_prop]) if summary_prop else ""
            
            news_texts.append(f"■ {page_title}\n{page_summary}")
            
        if not news_texts:
            await bot.send_message(chat_id=chat_id, text="뉴스레터 발행 대상 기사가 없습니다. 🌙")
            return
            
        combined_news = "\n\n".join(news_texts)
        
        # 4. Gemini에게 뉴스레터 작성 요청
        prompt = f"""
오늘 사용자님이 수집한 뉴스 기사들의 요약본 모음입니다.

{combined_news[:25000]}

위 내용들을 종합하여, 하루를 마무리하며 읽기 좋은 '일간 종합 뉴스레터'를 작성해 줘.
독자가 오늘의 핵심 트렌드와 주요 이슈를 한눈에 파악할 수 있도록 흐름을 짚어주고, 매거진 편집장처럼 부드럽고 전문적인 톤으로 작성해 줘.
반드시 아래 양식에 정확히 맞춰서 답변해 줘.

[요약 양식]
**📌 제목:** (예: [일간 뉴스레터] 2026-07-31 종합 요약 - 반도체 훈풍과 부동산 규제 완화)
**📅 날짜:** {today_str}
**📌 3줄 요약:**
1. 
2. 
3. 

**📰 오늘의 상세 브리핑:**
(주제별 또는 흐름별로 기사 내용을 엮어서 자세하고 맛깔나게 설명해 줘)
"""
        newsletter_response = model.generate_content(prompt)
        newsletter_text = newsletter_response.text
        
        # 5. 노션에 저장
        title_match = re.search(r'\*\*📌 제목:\*\*\s*(.*)', newsletter_text)
        newsletter_title = title_match.group(1).strip() if title_match else f"[일간 뉴스레터] {today_str} 종합 요약"
        
        properties = {
            "제목": {"title": [{"text": {"content": newsletter_title[:2000]}}]},
            "요약": {"rich_text": [{"text": {"content": newsletter_text[:2000]}}]},
            "날짜": {"date": {"start": today_str}},
            "키워드": {"multi_select": [{"name": "#일간뉴스레터"}]}
        }
        
        await notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=properties
        )
        
        # 6. 텔레그램 발송
        try:
            await bot.send_message(chat_id=chat_id, text=newsletter_text, parse_mode='Markdown')
        except Exception:
            await bot.send_message(chat_id=chat_id, text=newsletter_text)
            
        await bot.send_message(chat_id=chat_id, text="✅ 오늘의 뉴스레터가 노션에 발행 및 저장되었습니다! 굿나잇! 🌙")
        
    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"뉴스레터 발행 중 오류가 발생했습니다: {e}")

async def daily_timer_loop(application: Application):
    """APScheduler 대신 asyncio로 직접 만든 자정 타이머"""
    tz = pytz.timezone('Asia/Seoul')
    while True:
        now = datetime.datetime.now(tz)
        tomorrow = now + datetime.timedelta(days=1)
        midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # 테스트를 위해 10초 뒤로 세팅하지 않고 정상적으로 자정 계산
        sleep_seconds = (midnight - now).total_seconds()
        
        await asyncio.sleep(sleep_seconds)
        
        # 자정이 되면 뉴스레터 발행
        await generate_newsletter(application.bot)

async def post_init(application: Application):
    """봇 시작 시 타이머 루프 실행"""
    application.create_task(daily_timer_loop(application))

def main():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
        
    # Render 클라우드 배포용 가짜 웹 서버 실행 (백그라운드 스레드)
    app_web = Flask(__name__)
    
    @app_web.route('/')
    def home():
        return "AI 뉴스 봇이 정상적으로 살아있습니다!"
        
    def run_web():
        port = int(os.environ.get("PORT", 8080))
        app_web.run(host="0.0.0.0", port=port)
        
    threading.Thread(target=run_web, daemon=True).start()
    print("클라우드용 웹 서버가 시작되었습니다.")
        
    # post_init을 통해 타이머 루프를 등록
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newsletter", command_newsletter))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("AI 뉴스 요약 비서 봇이 실행되었습니다!")
    app.run_polling()

if __name__ == "__main__":
    main()
