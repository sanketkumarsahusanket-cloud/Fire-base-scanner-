import os
import asyncio
import aiohttp
import logging
import time
from datetime import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import CommandStart

# ================= CONFIG (Render Env Variables) =================
MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN", "YOUR_MAIN_BOT_TOKEN_HERE")
AUDIT_BOT_TOKEN = os.environ.get("AUDIT_BOT_TOKEN", "YOUR_AUDIT_BOT_TOKEN_HERE")
OWNER_ID = int(os.environ.get("OWNER_ID", "123456789")) # Apna Telegram ID dalein
PORT = int(os.environ.get("PORT", 8080))

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("Scanner")

# ================= BOTS SETUP =================
main_bot = Bot(token=MAIN_BOT_TOKEN)
audit_bot = Bot(token=AUDIT_BOT_TOKEN)
dp = Dispatcher()

# Semaphore for preventing RAM spike and IP Ban
MAX_CONCURRENT_REQUESTS = 50 
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# ================= AUDIT FUNCTION =================
async def send_audit_log(user_id, username, action, details):
    """Sirf owner ko audit log bhejega dusre bot se"""
    if not AUDIT_BOT_TOKEN or AUDIT_BOT_TOKEN == "YOUR_AUDIT_BOT_TOKEN_HERE":
        return
    
    text = (
        f"🛡 <b>AUDIT LOG</b> 🛡\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 <b>User:</b> {username} (<code>{user_id}</code>)\n"
        f"⚙️ <b>Action:</b> {action}\n"
        f"📝 <b>Details:</b> {details}\n"
        f"🕒 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    try:
        await audit_bot.send_message(OWNER_ID, text, parse_mode="HTML")
    except Exception as e:
        log.error(f"Audit log send failed: {e}")

# ================= SCANNER LOGIC =================
async def scan_single_firebase(session, url):
    url = url.strip().rstrip('/')
    if not url.startswith('http'):
        url = 'https://' + url
    
    if "firebaseio.com" not in url and "firebasedatabase.app" not in url:
        return None

    total_dev = 0
    online_dev = 0
    offline_dev = 0
    status = "UNKNOWN"
    http_code = ""

    async with SEMAPHORE: 
        try:
            async with session.get(f"{url}/.json", timeout=5) as resp:
                http_code = resp.status
                if resp.status == 200:
                    status = "ACTIVE"
                elif resp.status in [401, 403]:
                    return {"url": url, "status": "INACCESSIBLE", "code": resp.status, "total": 0, "online": 0, "offline": 0}
                else:
                    return {"url": url, "status": "ERROR", "code": resp.status, "total": 0, "online": 0, "offline": 0}

            async with session.get(f"{url}/clients.json", timeout=8) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except Exception:
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
                        status = "NO_CLIENTS_NODE"
                else:
                    status = "NO_CLIENTS_NODE"

            return {
                "url": url, "status": status, "code": 200,
                "total": total_dev, "online": online_dev, "offline": offline_dev
            }
        except asyncio.TimeoutError:
            return {"url": url, "status": "TIMEOUT", "code": "ERR", "total": 0, "online": 0, "offline": 0}
        except Exception as e:
            return {"url": url, "status": "ERROR", "code": str(e)[:10], "total": 0, "online": 0, "offline": 0}

# ================= COMMAND HANDLERS =================
@dp.message(CommandStart())
async def start_cmd(message: Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("🚫 Aap is bot ko use nahi kar sakte. Ye private hai.")
    
    await message.answer(
        f"👑 <b>Welcome Owner!</b>\n\n"
        f"📁 Mujhe ek <code>.txt</code> file bhejein jisme Firebase URLs hain.\n"
        f"Ya direct ek Firebase URL bhejein scan karne ke liye.",
        parse_mode="HTML"
    )

@dp.message(F.document)
async def handle_txt_file(message: Message):
    if message.from_user.id != OWNER_ID:
        return await message.answer("🚫 Aap authorized nahi hain.")
    
    if not message.document.file_name.endswith('.txt'):
        return await message.answer("❌ Sirf <code>.txt</code> file allowed hai!", parse_mode="HTML")
    
    msg = await message.answer("⏳ File download ho rahi hai...")
    
    file_path = f"temp_{message.document.file_id}.txt"
    await main_bot.download(message.document, destination=file_path)
    
    with open(file_path, 'r', encoding='utf-8') as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
    
    os.remove(file_path)

    if not urls:
        return await msg.edit_text("❌ File mein koi valid URL nahi mila.")

    total_urls = len(urls)
    await msg.edit_text(f"🔍 Total {total_urls} URLs mil gaye. Scanning shuru...\n(Chunks mein scan hoga, RAM safe rahegi)")

    asyncio.create_task(send_audit_log(
        message.from_user.id, message.from_user.username, 
        "FILE_SCAN", f"File: {message.document.file_name} | Total URLs: {total_urls}"
    ))

    CHUNK_SIZE = 100
    all_results = []
    
    async with aiohttp.ClientSession() as session:
        for i in range(0, total_urls, CHUNK_SIZE):
            chunk = urls[i:i + CHUNK_SIZE]
            tasks = [scan_single_firebase(session, url) for url in chunk]
            chunk_results = await asyncio.gather(*tasks)
            all_results.extend([r for r in chunk_results if r is not None])
            
            await asyncio.sleep(1) 
            
            if total_urls > CHUNK_SIZE:
                try:
                    await msg.edit_text(f"⏳ Scanning in progress... ({min(i + CHUNK_SIZE, total_urls)}/{total_urls} URLs processed)")
                except:
                    pass 

    total_found = len(all_results)
    total_online = sum(r['online'] for r in all_results)
    total_offline = sum(r['offline'] for r in all_results)
    total_devices = sum(r['total'] for r in all_results)

    report = f"🔥 <b>FIREBASE SCAN REPORT</b> 🔥\n"
    report += f"━━━━━━━━━━━━━━━━━━\n"
    report += f"📄 <b>Total URLs:</b> {total_found}\n"
    report += f"📱 <b>Total Devices:</b> {total_devices}\n"
    report += f"🟢 <b>Total Online:</b> {total_online}\n"
    report += f"⚪ <b>Total Offline:</b> {total_offline}\n"
    report += f"━━━━━━━━━━━━━━━━━━\n\n"
    report += f"<b>PER-FIREBASE STATUS:</b>\n\n"

    for i, res in enumerate(all_results, 1):
        if res['status'] == "ACTIVE":
            report += (
                f"{i}. 🔥 <code>{res['url']}</code>\n"
                f"├ 🟢 Status: ACTIVE\n"
                f"├ 📱 Devices: {res['total']}\n"
                f"├ 🟢 Online: {res['online']}\n"
                f"└ ⚪ Offline: {res['offline']}\n\n"
            )
        else:
            report += (
                f"{i}. 🔥 <code>{res['url']}</code>\n"
                f"└ 🔴 Status: {res['status']} ({res['code']})\n\n"
            )

    # HTML tags escape karna zaroori hai agar URL mein special characters hon
    # Par as a quick fix, hum bas file bhej denge agar bohot bada ho.
    
    if len(report) > 4000:
        file_name = f"scan_report_{int(time.time())}.txt"
        with open(file_name, 'w', encoding='utf-8') as f:
            f.write(report.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
        await msg.delete()
        await message.answer_document(FSInputFile(file_name), caption="📄 Scan Report (File document mein bheji hai kyunki report bohot badi thi)")
        os.remove(file_name)
    else:
        await msg.edit_text(report, parse_mode="HTML")

@dp.message(F.text)
async def handle_direct_url(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    
    url = message.text.strip()
    if "firebase" not in url.lower():
        return await message.answer("❌ Ye valid Firebase URL nahi lag raha.")

    msg = await message.answer("⏳ Scanning URL...")
    
    asyncio.create_task(send_audit_log(
        message.from_user.id, message.from_user.username, 
        "DIRECT_URL_SCAN", f"URL: {url}"
    ))

    async with aiohttp.ClientSession() as session:
        res = await scan_single_firebase(session, url)

    if res:
        if res['status'] == "ACTIVE":
            text = (
                f"🔥 <b>FIREBASE SCAN REPORT</b> 🔥\n\n"
                f"🔗 <b>URL:</b> <code>{res['url']}</code>\n"
                f"🟢 <b>Status:</b> {res['status']}\n"
                f"📱 <b>Total Devices:</b> {res['total']}\n"
                f"🟢 <b>Online Devices:</b> {res['online']}\n"
                f"⚪ <b>Offline Devices:</b> {res['offline']}\n"
            )
        else:
            text = (
                f"🔥 <b>FIREBASE SCAN REPORT</b> 🔥\n\n"
                f"🔗 <b>URL:</b> <code>{res['url']}</code>\n"
                f"🔴 <b>Status:</b> {res['status']} (HTTP: {res['code']})\n"
            )
        await msg.edit_text(text, parse_mode="HTML")
    else:
        await msg.edit_text("❌ Invalid Firebase URL.")

# ================= RENDER DUMMY WEB SERVER =================
async def web_server():
    """Render Web Service ko zinda rakhne ke liye chota sa server"""
    async def health_check(request):
        return web.Response(text="Bot is running!")

    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    log.info(f"Web server started on port {PORT}")

# ================= MAIN RUNNER =================
async def main():
    log.info("Starting Firebase Scanner Bot...")
    # Web server start karein
    asyncio.create_task(web_server())
    # Bot polling start karein
    await dp.start_polling(main_bot)

if __name__ == "__main__":
    asyncio.run(main())
