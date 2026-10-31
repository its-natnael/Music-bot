import os, re, glob, time, random, shutil, tempfile, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import telebot
from telebot import types
import yt_dlp

CHANNEL = "@justforlore"
bot = telebot.TeleBot(os.environ["BOT_TOKEN"], threaded=True, num_threads=4)
BOT_USERNAME = bot.get_me().username

lang = {}          # user id -> "am" / "en"
pending = {}       # user id -> video id (from a shared link)
file_cache = {}    # video id -> (telegram file_id, title)
member_cache = {}  # user id -> time of last positive check
DL_SLOTS = threading.Semaphore(2)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
LINK_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")

# Edit these anytime: (button label, YouTube search)
MOODS = [
    ("🎶 ትዝታ Tizita", "Tizita Ethiopian classic songs"),
    ("🔥 New Habesha Hits", "new Ethiopian music 2026"),
    ("💃 እስክስታ Eskista", "Ethiopian Eskista party music"),
    ("🙏 መዝሙር Mezmur", "Ethiopian orthodox mezmur"),
]

T = {
    "am": {
        "welcome": "🎵 እንኳን ደህና መጡ! የዘፈን ስም ወይም የዩቲዩብ ሊንክ ላኩልኝ፣ ወይም ከታች ይምረጡ 👇",
        "join": "መጀመሪያ ቻናላችንን ይቀላቀሉ፣ ከዚያ ✅ ተቀላቅያለሁ ይጫኑ።",
        "joinbtn": "📢 ቻናሉን ተቀላቀል",
        "donebtn": "✅ ተቀላቅያለሁ",
        "notjoined": "አልተቀላቀሉም። መጀመሪያ ቻናሉን ይቀላቀሉ።",
        "searching": "🔎 በመፈለግ ላይ...",
        "pick": "ይምረጡ 👇",
        "noresults": "ምንም አልተገኘም። ሌላ ስም ይሞክሩ።",
        "fail": "ይቅርታ፣ ዘፈኑን ማግኘት አልቻልኩም። ሌላ ይሞክሩ።",
        "soon": "ይህ ባህሪ በቅርቡ ይመጣል።",
        "busy": "አሁን ብዙ ጥያቄዎች አሉ። ከደቂቃ በኋላ ይሞክሩ።",
        "toolong": "ይህ ዘፈን በጣም ረጅም ነው (ከ10 ደቂቃ በላይ)። ሌላ ይሞክሩ።",
        "share": "🎁 ለጓደኛ ላክ",
        "random": "🎲 አስገርመኝ",
        "footer": "📢 ተቀላቀሉ " + CHANNEL,
    },
    "en": {
        "welcome": "🎵 Welcome! Send me a song name or a YouTube link, or pick below 👇",
        "join": "Join our channel first, then tap ✅ I joined.",
        "joinbtn": "📢 Join channel",
        "donebtn": "✅ I joined",
        "notjoined": "You haven't joined yet. Join the channel first.",
        "searching": "🔎 Searching...",
        "pick": "Pick one 👇",
        "noresults": "No results. Try another name.",
        "fail": "Sorry, I couldn't get that song. Try another.",
        "soon": "This feature is coming soon.",
        "busy": "Lots of requests right now. Try again in a minute.",
        "toolong": "That one is too long (over 10 minutes). Try another.",
        "share": "🎁 Send to a friend",
        "random": "🎲 Surprise me",
        "footer": "📢 Join " + CHANNEL,
    },
}

def t(uid, key):
    return T[lang.get(uid, "en")][key]

class TooLong(Exception):
    pass

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
    deno = shutil.which("deno") or os.path.join(
        os.path.dirname(sys.executable), "deno")
    if os.path.exists(deno):
        opts["js_runtimes"] = {"deno": {"path": deno}}
    else:
        print("DENO NOT FOUND", flush=True)
    if os.path.exists(SECRET_COOKIES):
        try:
            shutil.copy(SECRET_COOKIES, WORK_COOKIES)
            opts["cookiefile"] = WORK_COOKIES
        except Exception as e:
            print("COOKIE COPY ERROR:", e, flush=True)
    return opts

# ---- helpers ----
def is_member(uid):
    if time.time() - member_cache.get(uid, 0) < 300:
        return True
    try:
        s = bot.get_chat_member(CHANNEL, uid).status
        ok = s in ("member", "administrator", "creator")
        if ok:
            member_cache[uid] = time.time()
        return ok
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
    if uid not in lang:
        ask_language(chat_id)
        return False
    if not is_member(uid):
        send_join(chat_id, uid)
        return False
    return True

def show_menu(chat_id, uid):
    kb = types.InlineKeyboardMarkup()
    for i, (label, _) in enumerate(MOODS):
        kb.add(types.InlineKeyboardButton(label, callback_data="mood:%d" % i))
    kb.add(types.InlineKeyboardButton(t(uid, "random"), callback_data="random"))
    bot.send_message(chat_id, t(uid, "welcome"), reply_markup=kb)

def after_gate(chat_id, uid):
    vid = pending.pop(uid, None)
    if vid:
        send_song(chat_id, uid, vid)
    else:
        show_menu(chat_id, uid)

def search(q):
    opts = {"quiet": True, "extract_flat": True}
    opts.update(cookie_opts())
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info("ytsearch5:" + q, download=False)
    return [(e["id"], e.get("title") or "?") for e in info.get("entries", [])
            if e.get("id")]

def show_results(chat_id, uid, q):
    try:
        results = search(q)
    except Exception as e:
        print("SEARCH ERROR:", e, flush=True)
        results = []
    if not results:
        bot.send_message(chat_id, t(uid, "noresults"))
        return
    kb = types.InlineKeyboardMarkup()
    for vid, title in results:
        kb.add(types.InlineKeyboardButton("🎵 " + title[:55],
                                          callback_data="dl:" + vid))
    bot.send_message(chat_id, t(uid, "pick"), reply_markup=kb)

def download(vid):
    folder = tempfile.mkdtemp()
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": folder + "/%(title)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "max_filesize": 48 * 1024 * 1024,
        "match_filter": yt_dlp.utils.match_filter_func("duration <= 600"),
    }
    opts.update(cookie_opts())
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info("https://www.youtube.com/watch?v=" + vid,
                              download=True)
    if info is None:
        shutil.rmtree(folder, ignore_errors=True)
        raise TooLong()
    path = glob.glob(folder + "/*")[0]
    uploader = (info.get("artist") or info.get("uploader") or "")
    uploader = uploader.replace(" - Topic", "").strip()
    return path, info.get("title"), info.get("duration"), uploader

def caption(uid):
    return t(uid, "footer") + "\n🤖 @" + BOT_USERNAME

def share_kb(uid, vid, title):
    link = "https://t.me/%s?start=s_%s" % (BOT_USERNAME, vid)
    text = "🎵 %s — %s" % (title or "", CHANNEL)
    url = ("https://t.me/share/url?url=" + urllib.parse.quote(link)
           + "&text=" + urllib.parse.quote(text))
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(t(uid, "share"), url=url))
    return kb

def send_song(chat_id, uid, vid):
    # 1) instant send from Telegram's servers if we have it cached
    if vid in file_cache:
        fid, title = file_cache[vid]
        try:
            bot.send_audio(chat_id, fid, caption=caption(uid),
                           reply_markup=share_kb(uid, vid, title))
            return
        except Exception as e:
            print("CACHE SEND ERROR:", e, flush=True)
            file_cache.pop(vid, None)
    # 2) otherwise download (max 2 at a time)
    if not DL_SLOTS.acquire(blocking=False):
        bot.send_message(chat_id, t(uid, "busy"))
        return
    wait = None
    folder = None
    try:
        wait = bot.send_message(chat_id, t(uid, "searching"))
        path, title, dur, uploader = download(vid)
        folder = os.path.dirname(path)
        performer = (uploader + " | " + CHANNEL) if uploader else CHANNEL
        with open(path, "rb") as f:
            msg = bot.send_audio(chat_id, f, title=title, performer=performer,
                                 duration=dur, caption=caption(uid),
                                 reply_markup=share_kb(uid, vid, title))
        try:
            file_cache[vid] = (msg.audio.file_id, title)
        except Exception:
            pass
    except TooLong:
        bot.send_message(chat_id, t(uid, "toolong"))
    except Exception as e:
        print("DOWNLOAD ERROR:", e, flush=True)
        bot.send_message(chat_id, t(uid, "fail"))
    finally:
        DL_SLOTS.release()
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        if wait:
            try:
                bot.delete_message(chat_id, wait.message_id)
            except Exception:
                pass

# ---- handlers ----
@bot.message_handler(commands=["start"])
def start(m):
    uid = m.from_user.id
    parts = m.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("s_"):
        vid = parts[1][2:]
        if ID_RE.match(vid):
            pending[uid] = vid
    if gate(m.chat.id, uid):
        after_gate(m.chat.id, uid)

@bot.message_handler(commands=["language"])
def change_language(m):
    ask_language(m.chat.id)

@bot.callback_query_handler(func=lambda c: True)
def on_button(c):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    data = c.data
    if data.startswith("lang:"):
        lang[uid] = data[5:]
        bot.answer_callback_query(c.id)
        if gate(chat_id, uid):
            after_gate(chat_id, uid)
    elif data == "check":
        if is_member(uid):
            bot.answer_callback_query(c.id)
            after_gate(chat_id, uid)
        else:
            bot.answer_callback_query(c.id, t(uid, "notjoined"), show_alert=True)
    elif data.startswith("dl:"):
        bot.answer_callback_query(c.id)
        vid = data[3:]
        if ID_RE.match(vid) and gate(chat_id, uid):
            send_song(chat_id, uid, vid)
    elif data.startswith("mood:"):
        bot.answer_callback_query(c.id)
        if gate(chat_id, uid):
            try:
                show_results(chat_id, uid, MOODS[int(data[5:])][1])
            except Exception as e:
                print("MOOD ERROR:", e, flush=True)
    elif data == "random":
        bot.answer_callback_query(c.id)
        if gate(chat_id, uid):
            show_results(chat_id, uid, random.choice(MOODS)[1])

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
        found = LINK_RE.search(q)
        if found:
            send_song(m.chat.id, uid, found.group(1))
        else:
            bot.reply_to(m, t(uid, "noresults"))
        return
    show_results(m.chat.id, uid, q)

threading.Thread(target=web, daemon=True).start()
bot.infinity_polling()
