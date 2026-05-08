import os
import re
import discord
import google.generativeai as genai
from dotenv import load_dotenv

# ─── 환경변수 로드 ───────────────────────────────────────────────────────────
load_dotenv()
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BOT_NAME       = os.getenv("BOT_NAME", "봇").strip()

# ─── Gemini 2.5 Flash 초기화 ────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)

model = genai.GenerativeModel(
    model_name="gemini-2.5-flash-preview-05-20",
    system_instruction=(
        f"너의 이름은 '{BOT_NAME}'이야. "
        "디스코드 채널에서 사용자들을 도와주는 친근한 AI 어시스턴트야. "
        "한국어로 자연스럽게 대화하고, 질문에 친절하고 정확하게 답해줘. "
        "답변은 너무 길지 않게 적당히 조절해줘. "
        "이름을 불리면 반갑게 반응하고 뭘 도와줄지 물어봐."
    ),
)

# ─── 상태 저장소 ────────────────────────────────────────────────────────────
# 채널 화이트리스트: 비어있으면 모든 채널에서 응답, 등록하면 해당 채널만 응답
allowed_channels: set[int] = set()

# 채널별 Gemini 대화 세션 { channel_id: ChatSession }
chat_sessions: dict[int, genai.ChatSession] = {}

# ─── 유틸 함수 ──────────────────────────────────────────────────────────────
def get_chat(channel_id: int) -> genai.ChatSession:
    """채널마다 독립적인 멀티턴 대화 세션을 유지한다."""
    if channel_id not in chat_sessions:
        chat_sessions[channel_id] = model.start_chat(history=[])
    return chat_sessions[channel_id]

def is_admin(member: discord.Member) -> bool:
    """서버 관리자 또는 채널/서버 관리 권한 보유 여부 확인."""
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild

def is_bot_called(content: str, bot_mention: str) -> tuple[bool, str]:
    """
    봇이 호출됐는지 판단하고, 호출됐으면 (True, 질문 텍스트)를 반환한다.

    지원 패턴:
      - @멘션
      - 봇이름아 / 봇이름야  (앞에 올 때)
      - 봇이름아 / 봇이름야  (뒤에 올 때)
      - 문장 어디서든 이름 포함
    """
    name = re.escape(BOT_NAME)

    # @멘션 처리
    cleaned = content.replace(bot_mention, "").strip()
    if cleaned != content:
        return True, cleaned.strip(" ,!?.")

    # 한국어 호격 패턴: "봇이름아", "봇이름야" — 앞 또는 뒤
    vocative = re.compile(
        rf"^{name}[아야][,\s]*(.+)|(.+?)[,\s]*{name}[아야]\s*$",
        re.IGNORECASE | re.DOTALL,
    )
    m = vocative.search(content)
    if m:
        question = (m.group(1) or m.group(2) or "").strip(" ,!?.")
        return True, question or "안녕!"

    # 이름만 단독으로 언급 (인사)
    if re.fullmatch(rf"\s*{name}\s*[!?~]*\s*", content, re.IGNORECASE):
        return True, "안녕!"

    # 문장 어디서든 이름 포함 — 이름 부분 제거 후 질문 추출
    if re.search(name, content, re.IGNORECASE):
        question = re.sub(name, "", content, flags=re.IGNORECASE).strip(" ,!?.")
        return True, question or "안녕!"

    return False, ""

def channel_allowed(channel_id: int) -> bool:
    """화이트리스트가 비어있으면 전체 허용, 아니면 등록된 채널만 허용."""
    return not allowed_channels or channel_id in allowed_channels

async def send_long(message: discord.Message, text: str):
    """2000자 초과 시 분할 전송."""
    chunks = [text[i : i + 1900] for i in range(0, len(text), 1900)]
    for chunk in chunks:
        await message.reply(chunk)

# ─── Discord 클라이언트 ──────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ─── on_ready ───────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    print(f"✅ [{client.user.name}] 온라인")
    print(f"   응답 이름 키워드 : {BOT_NAME}아 / {BOT_NAME}야 / @멘션")
    print(f"   허용 채널 수     : {'전체' if not allowed_channels else len(allowed_channels)}")

# ─── on_message ─────────────────────────────────────────────────────────────
@client.event
async def on_message(message: discord.Message):
    # 봇 자신 무시
    if message.author == client.user:
        return

    # DM 무시 (서버 채널 전용)
    if not message.guild:
        return

    content = message.content.strip()
    if not content:
        return

    bot_mention = f"<@{client.user.id}>"
    
    try:
        # typing() 컨텍스트 매니저 대신 한 번만 전송
        await message.channel.trigger_typing()

        reply = await call_gemini(message.author.id, content)

        for i in range(0, len(reply), 2000):
            await message.channel.send(reply[i:i+2000])

    except Exception as e:
        await message.channel.send(f"⚠️ 오류가 발생했어요: `{e}`")

    # ── 커맨드 처리 ────────────────────────────────────────────────────────

    # ── !도움말 (누구나) ──────────────────────────────────────────────────
    if content == "!도움말":
        embed = discord.Embed(
            title=f"📖 {BOT_NAME} 도움말",
            color=0x5865F2,
        )
        embed.add_field(
            name="💬 봇 호출 방법",
            value=(
                f"`{BOT_NAME}아 [질문]` — 이름 뒤에 아/야를 붙여 질문\n"
                f"`[질문] {BOT_NAME}야` — 질문 뒤에 이름을 붙여도 OK\n"
                f"`@{BOT_NAME} [질문]` — 멘션으로 질문\n"
                f"예시: `{BOT_NAME}야 파이썬 리스트 정렬하는 법 알려줘`"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛠️ 일반 커맨드",
            value="`!도움말` — 이 메시지를 표시",
            inline=False,
        )
        embed.add_field(
            name="🔒 관리자 전용 커맨드",
            value=(
                "`!채널등록 [#채널]` — 봇이 응답할 채널 추가\n"
                "`!채널해제 [#채널]` — 채널 제거\n"
                "`!채널목록` — 허용된 채널 목록 확인\n"
                "`!기록확인` — 현재 채널의 대화 기록 수 확인"
            ),
            inline=False,
        )
        embed.set_footer(text="채널 미등록 상태에서는 모든 채널에서 응답합니다.")
        await message.channel.send(embed=embed)
        return

    # ── 관리자 전용 커맨드 ────────────────────────────────────────────────
    if content.startswith("!채널등록"):
        if not is_admin(message.author):
            await message.reply("🔒 이 커맨드는 관리자만 사용할 수 있습니다.")
            return
        if not message.channel_mentions:
            await message.reply("📌 사용법: `!채널등록 #채널명`")
            return
        added = []
        for ch in message.channel_mentions:
            allowed_channels.add(ch.id)
            added.append(ch.mention)
        await message.reply(f"✅ 채널 등록 완료: {', '.join(added)}")
        return

    if content.startswith("!채널해제"):
        if not is_admin(message.author):
            await message.reply("🔒 이 커맨드는 관리자만 사용할 수 있습니다.")
            return
        if not message.channel_mentions:
            await message.reply("📌 사용법: `!채널해제 #채널명`")
            return
        removed = []
        not_found = []
        for ch in message.channel_mentions:
            if ch.id in allowed_channels:
                allowed_channels.discard(ch.id)
                removed.append(ch.mention)
            else:
                not_found.append(ch.mention)
        parts = []
        if removed:
            parts.append(f"✅ 해제: {', '.join(removed)}")
        if not_found:
            parts.append(f"⚠️ 미등록 채널: {', '.join(not_found)}")
        await message.reply("\n".join(parts))
        return

    if content == "!채널목록":
        if not is_admin(message.author):
            await message.reply("🔒 이 커맨드는 관리자만 사용할 수 있습니다.")
            return
        if not allowed_channels:
            await message.reply("📋 등록된 채널이 없습니다. (현재 **모든 채널**에서 응답 중)")
        else:
            mentions = [f"<#{ch_id}>" for ch_id in allowed_channels]
            await message.reply(f"📋 허용된 채널 ({len(mentions)}개):\n" + "\n".join(mentions))
        return

    if content == "!기록확인":
        if not is_admin(message.author):
            await message.reply("🔒 이 커맨드는 관리자만 사용할 수 있습니다.")
            return
        session = chat_sessions.get(message.channel.id)
        count = len(session.history) if session else 0
        await message.reply(
            f"📊 이 채널의 대화 기록: **{count}턴**\n"
            f"(초기화하려면 `{BOT_NAME}야 대화 초기화해줘` 라고 말해보세요)"
        )
        return

    # ── AI 응답 처리 ───────────────────────────────────────────────────────

    # 허용된 채널인지 확인
    if not channel_allowed(message.channel.id):
        return

    # 봇이 호출됐는지 확인
    called, question = is_bot_called(content, bot_mention)
    if not called:
        return

    # "대화 초기화" 요청 감지
    if re.search(r"대화\s*(초기화|리셋|지워|삭제)", question):
        chat_sessions.pop(message.channel.id, None)
        await message.reply("🔄 대화 기록을 초기화했어요! 새로 시작해봐요.")
        return

    # Gemini에게 질문
    async with message.channel.typing():
        try:
            chat = get_chat(message.channel.id)
            response = chat.send_message(question)
            await send_long(message, response.text)
        except Exception as e:
            print(f"[오류] {type(e).__name__}: {e}")
            await message.reply("⚠️ 응답 생성 중 오류가 발생했어요. 잠시 후 다시 시도해주세요.")

# ─── 실행 ────────────────────────────────────────────────────────────────────
client.run(DISCORD_TOKEN)
