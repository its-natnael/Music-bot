import os, glob, shutil, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import telebot
from telebot import types
import yt_dlp

CHANNEL = "@justforlore"
bot = telebot.TeleBot(os.environ["BOT_TOKEN"])
lang = {}  # user id -> "am" or "en"

T = {
    "am": {
        "welcome": "🎵 እንኳን ደህና መጡ! የዘፈን ስም ወይም የዩቲዩብ ሊንክ ላኩልኝ።",
        "join": "መጀመሪያ ቻናላችንን ይቀላቀሉ፣ ከዚያ ✅ ተቀላቅያለሁ ይጫኑ።",
        "joinbtn": "📢 ቻናሉን ተቀላቀል",
        "donebtn": "✅ ተቀላቅያለሁ",
        "notjoined": "አልተቀላቀሉም። መጀመሪያ ቻናሉን ይቀላቀሉ።",
        "searching": "🔎 በመፈለግ ላይ...",
        "pick": "ይምረጡ 👇",
        "noresults": "ምንም አልተገኘም። ሌላ ስም ይሞክሩ።",
        "fail": "ይቅርታ፣ ዘፈኑን ማግኘት አልቻልኩም። ሌላ ይሞክሩ።",
        "soon": "ይህ ባህሪ በቅርቡ ይመጣል።",
        "footer": "📢 ተቀላቀሉ " + CHANNEL,
    },
    "en": {
        "welcome": "🎵 Welcome! Send me a song name or a YouTube link.",
        "join": "Join our channel first, then tap ✅ I joined.",
        "joinbtn": "📢 Join channel",
        "donebtn": "✅ I joined",
        "notjoined": "You haven't joined yet. Join the channel first.",
        "searching": "🔎 Searching...",
        "pick": "Pick one 👇",
        "noresults": "No results. Try another name.",
        "fail": "Sorry, I couldn't get that song. Try another.",
        "soon": "This feature is coming soon.",
        "footer": "📢 Join " + CHANNEL,
    },
}

def t(uid, key):
    return T[lang.get(uid, "en")][key]

# ---- tiny web server so Render keeps the service alive ----
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a):
        pass

def web():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()

# ---- cookies (Render Secret File) + JS runtime ----
SECRET_COOKIES = "/etc/secrets/cookies.txt"
WORK_COOKIES = "/tmp/cookies.txt"

def cookie_opts():
    import sys
    opts = {}
    # JS runtime so yt-dlp can solve YouTube's challenge
    deno = shutil.which("deno") or os.path.join(
        os.path.dirname(sys.executable), "deno")
    if os.path.exists(deno):
        opts["js_runtimes"] = {"deno": {"path": deno}}
    else:
        print("DENO NOT FOUND", flush=True)
    # cookies: copy to a writable place, because yt-dlp may update the file
    if os.path.exists(SECRET_COOKIES):
        try:
            shutil.copy(SECRET_COOKIES, WORK_COOKIES)
            opts["cookiefile"] = WORK_COOKIES
        except Exception as e:
            print("COOKIE COPY ERROR:", e, flush=True)
    return opts

# ---- helpers ----
def is_member(uid):
    try:
        s = bot.get_chat_member(CHANNEL, uid).status
        return s in ("member", "administrator", "creator")
    except Exception as e:
        print("MEMBER CHECK ERROR:", e, flush=True)
        return False

def ask_language(chat_id):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("አማርኛ", callback_data="lang:am"),
        types.InlineKeyboardButton("English", callback_data="lang:en"),
    )
    bot.send_message(chat_id, "ቋንቋ ይምረጡ / Choose language", reply_markup=kb)

def send_join(chat_id, uid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        t(uid, "joinbtn"), url="https://t.me/" + CHANNEL.lstrip("@")))
    kb.add(types.InlineKeyboardButton(t(uid, "donebtn"), callback_data="check"))
    bot.send_message(chat_id, t(uid, "join"), reply_markup=kb)

def gate(chat_id, uid):
    """True if user may continue. Otherwise asks for language or channel join."""
    if uid not in lang:
        ask_language(chat_id)
        return False
    if not is_member(uid):
        send_join(chat_id, uid)
        return False
    return True

def search(q):
    opts = {"quiet": True, "extract_flat": True}
    opts.update(cookie_opts())
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info("ytsearch5:" + q, download=False)
    return [(e["id"], e["title"]) for e in info.get("entries", []) if e.get("id")]

def download(url):
    folder = tempfile.mkdtemp()
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": folder + "/%(title)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "max_filesize": 48 * 1024 * 1024,
    }
    opts.update(cookie_opts())
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    path = glob.glob(folder + "/*")[0]
    return path, info.get("title"), info.get("duration")

def send_song(chat_id, uid, url):
    wait = bot.send_message(chat_id, t(uid, "searching"))
    folder = None
    try:
        path, title, dur = download(url)
        folder = os.path.dirname(path)
        with open(path, "rb") as f:
            bot.send_audio(chat_id, f, title=title, duration=dur,
                           caption=t(uid, "footer"))
    except Exception as e:
        print("DOWNLOAD ERROR:", e, flush=True)
        bot.send_message(chat_id, t(uid, "fail"))
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass

# ---- handlers ----
@bot.message_handler(commands=["start"])
def start(m):
    uid = m.from_user.id
    if gate(m.chat.id, uid):
        bot.send_message(m.chat.id, t(uid, "welcome"))

@bot.callback_query_handler(func=lambda c: True)
def on_button(c):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    data = c.data
    if data.startswith("lang:"):
        lang[uid] = data[5:]
        bot.answer_callback_query(c.id)
        if gate(chat_id, uid):
            bot.send_message(chat_id, t(uid, "welcome"))
    elif data == "check":
        if is_member(uid):
            bot.answer_callback_query(c.id)
            bot.send_message(chat_id, t(uid, "welcome"))
        else:
            bot.answer_callback_query(c.id, t(uid, "notjoined"), show_alert=True)
    elif data.startswith("dl:"):
        bot.answer_callback_query(c.id)
        if gate(chat_id, uid):
            send_song(chat_id, uid,
                      "https://www.youtube.com/watch?v=" + data[3:])

@bot.message_handler(content_types=["voice", "video", "audio", "video_note"])
def media(m):
    uid = m.from_user.id
    if gate(m.chat.id, uid):
        bot.reply_to(m, t(uid, "soon"))

@bot.message_handler(content_types=["text"])
def text(m):
    uid = m.from_user.id
    if not gate(m.chat.id, uid):
        return
    q = m.text.strip()
    if "youtube.com" in q or "youtu.be" in q:
        send_song(m.chat.id, uid, q)
        return
    try:
        results = search(q)
    except Exception as e:
        print("SEARCH ERROR:", e, flush=True)
        results = []
    if not results:
        bot.reply_to(m, t(uid, "noresults"))
        return
    kb = types.InlineKeyboardMarkup()
    for vid, title in results:
        kb.add(types.InlineKeyboardButton("🎵 " + title[:55],
                                          callback_data="dl:" + vid))
    bot.send_message(m.chat.id, t(uid, "pick"), reply_markup=kb)

threading.Thread(target=web, daemon=True).start()
bot.infinity_polling()
