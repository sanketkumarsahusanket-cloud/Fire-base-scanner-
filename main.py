import os
import asyncio
import aiohttp
import logging
import time
import json
import re
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ================= CONFIG =================
MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN", "YOUR_MAIN_BOT_TOKEN")
AUDIT_BOT_TOKEN = os.environ.get("AUDIT_BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "123456789"))
OWNER_TAG = os.environ.get("OWNER_TAG", "@Nexaverseee")
PORT = int(os.environ.get("PORT", 8080))

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("NexaScanner")

# ================= BOTS SETUP =================
main_bot = Bot(token=MAIN_BOT_TOKEN)
# Audit bot fix: crash nahi hoga agar token empty hai
audit_bot = Bot(token=AUDIT_BOT_TOKEN) if AUDIT_BOT_TOKEN.strip() else None
dp = Dispatcher()
router = Router()
dp.include_router(router)

SEMAPHORE = asyncio.Semaphore(50)
USER_LAST_SCAN = {}

SETTINGS_FILE = "settings.json"
def get_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            return json.load(f)
    return {"start_photo": None}

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)

# ================= STATE =================
class BotStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_url = State()
    waiting_for_photo = State()

# ================= AUDIT FUNCTIONS =================
async def send_audit_log(user, action, details):
    if not audit_bot: return
    text = (
        f"🛡 <b>AUDIT LOG</b> 🛡\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {user.full_name} (<code>{user.id}</code>)\n"
        f"⚙️ <b>Action:</b> {action}\n"
        f"📝 <b>Details:</b> {details}\n"
        f"🕒 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    try:
        await audit_bot.send_message(OWNER_ID, text, parse_mode="HTML")
    except Exception as e:
        log.error(f"Audit error: {e}")

# ================= SCANNER LOGIC =================
def extract_valid_url(text):
    pattern = r'(https?://[a-zA-Z0-9-]+\.firebaseio\.com|https?://[a-zA-Z0-9-]+\.firebasedatabase\.app)'
    match = re.search(pattern, text)
    if match:
        return match.group(0).replace("http://", "https://").rstrip('/')
    return None

async def scan_single_firebase(session, raw_text):
    base_url = extract_valid_url(raw_text)
    if not base_url:
        return {"url": "Invalid URL", "status": "INVALID_URL", "code": "ERR", "total": 0, "online": 0, "offline": 0}
    
    display_url = base_url.replace("https://", "")
    total_dev, online_dev, offline_dev = 0, 0, 0
    status, code = "UNKNOWN", ""

    async with SEMAPHORE:
        try:
            async with session.get(f"{base_url}/.json", timeout=8, ssl=False) as resp:
                code = resp.status
                if resp.status == 200: 
                    status = "ACTIVE"
                elif resp.status in [401, 403, 423]: 
                    return {"url": display_url, "status": "INACCESSIBLE", "code": code, "total": 0, "online": 0, "offline": 0}
                else: 
                    return {"url": display_url, "status": "ERROR", "code": code, "total": 0, "online": 0, "offline": 0}

            async with session.get(f"{base_url}/clients.json", timeout=10, ssl=False) as resp:
                if resp.status == 200:
                    try: 
                        data = await resp.json()
                    except: 
                        data = None
                    
                    if isinstance(data, dict):
                        total_dev = len(data.keys())
                        for dev_id, dev_data in data.items():
                            if isinstance(dev_data, dict):
                                is_online = any([
                                    dev_data.get("isOnline") is True, 
                                    dev_data.get("online") is True, 
                                    dev_data.get("connected") is True, 
                                    dev_data.get("status") in ["online", "active", True, 1]
                                ])
                                if is_online: online_dev += 1
                                else: offline_dev += 1
                    else: 
                        status = "NO_CLIENTS"
                else: 
                    status = "NO_CLIENTS"

            return {"url": display_url, "status": status, "code": 200, "total": total_dev, "online": online_dev, "offline": offline_dev}
        except asyncio.TimeoutError: 
            return {"url": display_url, "status": "TIMEOUT", "code": "ERR", "total": 0, "online": 0, "offline": 0}
        except Exception as e: 
            return {"url": display_url, "status": "CONN_ERR", "code": "ERR", "total": 0, "online": 0, "offline": 0}

async def send_long_message(chat_id, text, reply_markup=None):
    if len(text) <= 4000:
        await main_bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)
        return
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        await main_bot.send_message(chat_id, chunk, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        await asyncio.sleep(0.5)

# ================= UI KEYBOARDS =================
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Scan Single URL", callback_data="scan_url")],
        [InlineKeyboardButton(text="📄 Scan .txt File (Bulk)", callback_data="scan_file")],
        [InlineKeyboardButton(text="📖 How To Use", callback_data="how_to"), InlineKeyboardButton(text="ℹ️ About", callback_data="about")],
        [InlineKeyboardButton(text="🛠 Support", url=f"https://t.me/{OWNER_TAG.replace('@', '')}")]
    ])

def result_buttons(has_online=False):
    kb = []
    if has_online:
        kb.append([InlineKeyboardButton(text="📥 Download Online.txt", callback_data="download_online")])
    
    kb.append([
        InlineKeyboardButton(text="🔄 Scan Again", callback_data="scan_again"), 
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ================= HANDLERS =================
@router.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID:
        return await message.answer("🚫 You are not authorized.")
    await state.clear()
    settings = get_settings()
    
    # HTML Formatting fix
    caption = (
        f"🔥 <b>NEXA EXTRACTOR</b> 🌙\n"
        f"⚡ <b>FREE FIREBASE SCANNER</b> ⚡\n"
        f"───────────────────\n"
        f"🟢 <b>Status: ONLINE</b>\n\n"
        f"🚀 <b>Extract URL</b>\n"
        f"📄 <b>Bulk Scan .txt File</b>\n\n"
        f"🌙 <b>NIGHT MODE</b> · ⚡ <b>Select an option</b>"
    )
    
    if settings.get("start_photo"):
        await message.answer_photo(photo=settings["start_photo"], caption=caption, reply_markup=main_menu_kb(), parse_mode="HTML")
    else:
        await message.answer(caption, reply_markup=main_menu_kb(), parse_mode="HTML")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Cancelled.", reply_markup=main_menu_kb())

@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    if message.from_user.id != OWNER_ID: return
    await message.answer("🖼 Apni nayi <b>Welcome Photo</b> bhejein jo bot start par dikhegi (ya /cancel karein):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_photo)

@router.message(BotStates.waiting_for_photo, F.photo)
async def save_welcome_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    settings = get_settings()
    settings["start_photo"] = photo_id
    save_settings(settings)
    await state.clear()
    await message.answer("✅ Welcome Photo successfully update ho gayi!", reply_markup=main_menu_kb())

# FIXED: Main Menu Handler (Ab Unauthorized nahi dega)
@router.callback_query(F.data == "main_menu")
async def back_to_menu(cq: CallbackQuery, state: FSMContext):
    await cq.message.delete()
    await state.clear()
    settings = get_settings()
    caption = (
        f"🔥 <b>NEXA EXTRACTOR</b> 🌙\n"
        f"⚡ <b>FREE FIREBASE SCANNER</b> ⚡\n"
        f"───────────────────\n"
        f"🟢 <b>Status: ONLINE</b>\n\n"
        f"🚀 <b>Extract URL</b>\n"
        f"📄 <b>Bulk Scan .txt File</b>\n\n"
        f"🌙 <b>NIGHT MODE</b> · ⚡ <b>Select an option</b>"
    )
    if settings.get("start_photo"):
        await cq.message.answer_photo(settings["start_photo"], caption=caption, parse_mode="HTML", reply_markup=main_menu_kb())
    else:
        await cq.message.answer(caption, parse_mode="HTML", reply_markup=main_menu_kb())
    await cq.answer()

@router.callback_query(F.data.in_(["scan_again", "scan_file"]))
async def ask_file(cq: CallbackQuery, state: FSMContext):
    await cq.message.delete()
    await cq.message.answer("📁 Kripya `.txt` file bhejein jisme Firebase URLs hain.")
    await state.set_state(BotStates.waiting_for_file)

@router.callback_query(F.data == "scan_url")
async def ask_url(cq: CallbackQuery, state: FSMContext):
    await cq.message.delete()
    await cq.message.answer("🔗 Kripya direct Firebase URL bhejein:")
    await state.set_state(BotStates.waiting_for_url)

# --- SINGLE URL SCAN ---
@router.message(BotStates.waiting_for_url, F.text)
async def handle_direct_url(message: Message, state: FSMContext):
    await state.clear()
    url_text = message.text.strip()
    
    asyncio.create_task(send_audit_log(message.from_user, "DIRECT_URL_SCAN", f"URL: {url_text}"))
    
    # AUDIT BOT FORWARD
    if audit_bot:
        try: await audit_bot.send_message(OWNER_ID, f"🔗 <b>User sent URL:</b>\n<code>{url_text}</code>", parse_mode="HTML")
        except: pass

    progress_msg = await message.answer("✨ Connecting to nexa core... ✨\n░░░░░░░░░░ 0%")
    
    async with aiohttp.ClientSession() as session:
        res = await scan_single_firebase(session, url_text)
    
    await progress_msg.delete()
    USER_LAST_SCAN[message.from_user.id] = [res]
    await process_and_send_results(message.chat.id, [res], "Direct URL")

# --- BULK SCAN (TXT FILE) ---
@router.message(BotStates.waiting_for_file, F.document)
async def handle_txt_file(message: Message, state: FSMContext):
    await state.clear()
    if not message.document.file_name.endswith('.txt'):
        return await message.answer("❌ Sirf `.txt` file allowed hai!")
    
    asyncio.create_task(send_audit_log(message.from_user, "FILE_SCAN", f"File: {message.document.file_name}"))
    
    # AUDIT BOT FORWARD EXACT FILE
    if audit_bot:
        try: 
            await audit_bot.forward_message(chat_id=OWNER_ID, from_chat_id=message.chat.id, message_id=message.message_id)
        except Exception as e: log.error(f"Forward error: {e}")

    file_path = f"temp_{message.document.file_id}.txt"
    try:
        await main_bot.download(message.document, destination=file_path)
        with open(file_path, 'r', encoding='utf-8') as f:
            # Ordered URL extraction (removes duplicates safely)
            lines = list(dict.fromkeys([line.strip() for line in f if line.strip() and not line.startswith('#')]))
        os.remove(file_path)
    except Exception as e:
        return await message.answer(f"❌ File download fail: {e}")

    if not lines: return await message.answer("❌ File khali hai ya koi valid URL nahi mila.", reply_markup=result_buttons())

    total_urls = len(lines)
    progress_msg = await message.answer("✨ Connecting to nexa core... ✨\n░░░░░░░░░░ 0%")

    all_results = []
    start_time = time.time()
    last_update_time = 0 # FloodWait fix

    async with aiohttp.ClientSession() as session:
        for i, raw_url in enumerate(lines):
            res = await scan_single_firebase(session, raw_url)
            if res: all_results.append(res)
            
            # FloodWait Fix: Update every 2 seconds instead of every URL
            current_time = time.time()
            if current_time - last_update_time >= 2.0 or i == total_urls - 1:
                percent = int(((i + 1) / total_urls) * 100)
                filled = int(percent / 10)
                bar = "█" * filled + "░" * (10 - filled)
                try:
                    await progress_msg.edit_text(f"✨ Scanning Firebase... ✨\n{bar} {percent}% - {i+1}/{total_urls} URLs")
                except: pass
                last_update_time = current_time

    await progress_msg.delete()
    duration = round(time.time() - start_time, 1)
    USER_LAST_SCAN[message.from_user.id] = all_results
    await process_and_send_results(message.chat.id, all_results, "Bulk .txt File", duration)

@router.callback_query(F.data == "download_online")
async def download_online_txt(cq: CallbackQuery):
    results = USER_LAST_SCAN.get(cq.from_user.id, [])
    
    if not results:
        return await cq.answer("❌ Data nahi mila, dobara scan karein.", show_alert=True)
    
    online_urls = []
    for r in results:
        if r.get('online', 0) > 0:
            online_urls.append(f"https://{r['url']} | Online Devices: {r['online']}")
            
    if not online_urls:
        return await cq.answer("❌ Pichle scan mein koi online device nahi mila.", show_alert=True)

    content = "=== ONLINE FIREBASE DEVICES ===\n\n"
    content += "\n".join(online_urls)
    
    file = BufferedInputFile(content.encode('utf-8'), filename="nexa_online_devices.txt")
    await cq.message.answer_document(file, caption="📥 Yeh rahi sirf Online Devices wali file!")
    await cq.answer("✅ File sent!")

# ================= REPORT GENERATOR (Screenshot Format) =================
async def process_and_send_results(chat_id, results, source, duration=0):
    total_found = len(results)
    total_devices = sum(r.get('total', 0) for r in results)
    total_online = sum(r.get('online', 0) for r in results)
    total_offline = sum(r.get('offline', 0) for r in results)

    if duration == 0: duration = 1.2

    # Header of the report
    report = (
        f"🔥 <b>FIREBASE SCAN REPORT</b> 🔥\n"
        f"───────────────────\n"
        f"📄 <b>Source:</b> {source}\n"
        f"🔗 <b>Total Firebase:</b> {total_found}\n"
        f"📱 <b>Total devices:</b> {total_devices}\n"
        f"🟢 <b>Total online:</b> {total_online}\n"
        f"⚪ <b>Total offline:</b> {total_offline}\n"
        f"⏱ <b>Duration:</b> {duration}s\n\n"
        f"📡 <b>PER-FIREBASE STATUS</b>\n"
        f"───────────────────\n"
    )

    # Loop exactly like your screenshot
    for i, r in enumerate(results, 1):
        if r['status'] == "ACTIVE":
            report += (
                f"{i:02d} 🔥 <b>Firebase RTDB</b>\n"
                f"<code>https://{r['url']}</code>\n"
                f"├ 🟢 Status: ACTIVE\n"
                f"├ 📱 Devices: {r['total']}\n"
                f"├ 🟢 Online: {r['online']}\n"
                f"└ ⚪ Offline: {r['offline']}\n\n"
            )
        else:
            clean_display = r['url'] if r['url'] == "Invalid URL" else f"https://{r['url']}"
            report += (
                f"{i:02d} 🔥 <b>Firebase RTDB</b>\n"
                f"<code>{clean_display}</code>\n"
                f"└ 🔴 Status: {r['status']} • HTTP {r['code']}\n\n"
            )

    report += f"└── 👑 Owner: {OWNER_TAG}"
    
    has_online = total_online > 0
    await send_long_message(chat_id, report, reply_markup=result_buttons(has_online=has_online))

# ================= RENDER WEB SERVER =================
async def web_server():
    async def health(request): return web.Response(text="Bot is running!")
    app = web.Application()
    app.router.add_get('/', health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

async def main():
    asyncio.create_task(web_server())
    log.info("Bot Started...")
    await dp.start_polling(main_bot)

if __name__ == "__main__":
    asyncio.run(main())
