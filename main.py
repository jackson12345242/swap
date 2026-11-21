"""JK's Crypto Exchange: ticket-based, non-custodial Discord swap bot for FixedFloat (ff.io).
Admin runs /setup once in the main channel -> panel with "Start a swap" + "Available coins" buttons ->
private ticket channel -> (terms, first swap only) -> menu (pick coins, enter details) -> confirm ->
invoice -> live status + txids -> emergency button.
Env: DISCORD_TOKEN, FF_API_KEY, FF_API_SECRET, DB_PATH (on your Railway volume), FF_RATE_TYPE (default float),
     BRAND_IMAGE (direct URL of your logo, shown as a thumbnail on every embed),
     RULES_CHANNEL (channel id people must read before swapping)
"""
import os, re, math, hmac, hashlib, json, sqlite3, time, asyncio
from datetime import datetime, timezone
import aiohttp, discord
from discord import app_commands
from discord.ext import commands, tasks

API = "https://ff.io/api/v2/"
KEY, SECRET = os.environ["FF_API_KEY"], os.environ["FF_API_SECRET"]
RATE_TYPE = os.getenv("FF_RATE_TYPE", "float")
DB_PATH = os.getenv("DB_PATH", "/data/swaps.db")
MAX_AGE = 3 * 86400      # stop polling orders older than this
CLOSE_DELAY = 600        # seconds before a finished ticket channel is deleted
FINAL = ("DONE", "EXPIRED")
BLURPLE, GREEN, GOLD, RED = 0x5865F2, 0x57F287, 0xFEE75C, 0xED4245
MIN_BUFFER_USD = float(os.getenv("MIN_BUFFER_USD", "0.01"))  # added on top of FixedFloat's minimum
CHAT_INPUT = os.getenv("CHAT_INPUT", "1") == "1"  # type the USD amount in chat (needs Message Content Intent)
BRAND = "JK's Crypto Exchange"
BRAND_IMAGE = os.getenv("BRAND_IMAGE", "https://imagepaste.org/i/s6vga5pa.jpg")  # logo; set BRAND_IMAGE to override
RULES_CHANNEL = int(os.getenv("RULES_CHANNEL", "1573162844189495336"))
LOG_CHANNEL = int(os.getenv("LOG_CHANNEL", "1568114348952649809"))      # finished swaps are logged here
VOUCH_CHANNEL = int(os.getenv("VOUCH_CHANNEL", "1560432284103086080"))  # where customers post +vouch
VOUCH_REQUIRED = os.getenv("VOUCH_REQUIRED", "1") == "1" and CHAT_INPUT  # reading vouches needs Message Content Intent
LOG_TXIDS = os.getenv("LOG_TXIDS", "0") == "1"  # also show tx ids in the log (it may be a public channel)
TOS_TEXT = (
    "**By using this service you agree to the terms:** swaps are executed by a third party "
    "(FixedFloat). This bot never holds your funds. You are responsible for the receiving address, "
    "coin and network. A wrong amount, coin or network can cause delays, refunds or loss, and we "
    "cannot reverse swaps. Float rates can change before your swap executes."
)  # replace with your lawyer-reviewed text
STATUS = {
    "NEW": "Waiting for your deposit", "PENDING": "Deposit detected, confirming",
    "EXCHANGE": "Exchanging", "WITHDRAW": "Sending to your address", "DONE": "Complete",
    "EXPIRED": "Expired (no deposit received)", "EMERGENCY": "Needs action: press the red button",
}

db = sqlite3.connect(DB_PATH)
db.executescript("""
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, token TEXT, user_id INT, channel_id INT,
    status TEXT, created REAL, close_at REAL);
CREATE TABLE IF NOT EXISTS tickets(channel_id INT PRIMARY KEY, user_id INT, guild_id INT, created REAL);
CREATE TABLE IF NOT EXISTS config(guild_id INT PRIMARY KEY, category_id INT, staff_role_id INT);
CREATE TABLE IF NOT EXISTS addresses(user_id INT, code TEXT, address TEXT, memo TEXT, updated REAL,
    PRIMARY KEY(user_id, code));
CREATE TABLE IF NOT EXISTS agreed(user_id INT PRIMARY KEY, ts REAL);
""")


def add_col(table, col, typ):  # tiny migration helper: safe to run on every start
    try: db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}"); return True
    except sqlite3.OperationalError: return False


add_col("orders", "vouch_text", "TEXT")   # the +vouch line we hand the customer
add_col("orders", "log_msg", "INT")        # id of the swap-log message (edited when they vouch)
if add_col("orders", "vouched", "INT DEFAULT 0"):
    db.execute("UPDATE orders SET vouched=1")  # swaps from before vouching existed never block anyone
add_col("orders", "usd", "REAL")          # USD value of each swap (for the panel record)
add_col("config", "panel_channel", "INT")  # where the panel lives, so the record can be updated
add_col("config", "panel_msg", "INT")
db.commit()

class FFError(Exception): pass

intents = discord.Intents.default()
intents.message_content = CHAT_INPUT
bot = commands.Bot(command_prefix="!", intents=intents)
session: aiohttp.ClientSession
ccies: list = []
opening: set = set()
waiting: dict = {}  # channel_id -> Wizard waiting for a typed USD amount


async def ff(method, **params):
    body = json.dumps(params, separators=(",", ":"))
    sign = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-API-KEY": KEY, "X-API-SIGN": sign}
    async with session.post(API + method, data=body, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
        j = await r.json(content_type=None)
    if j.get("code") != 0:
        raise FFError(j.get("msg") or "FixedFloat API error")
    return j["data"]


def latest_order(channel_id):
    return db.execute("SELECT id, token, user_id, status FROM orders WHERE channel_id=? "
                      "ORDER BY created DESC LIMIT 1", (channel_id,)).fetchone()


def get_saved(uid, code):
    r = db.execute("SELECT address, memo FROM addresses WHERE user_id=? AND code=?", (uid, code)).fetchone()
    return {"address": r[0], "memo": r[1]} if r else None


def has_agreed(uid):
    return db.execute("SELECT 1 FROM agreed WHERE user_id=?", (uid,)).fetchone() is not None


def brand(e):
    """Put the logo on an embed (if BRAND_IMAGE is set)."""
    if BRAND_IMAGE: e.set_thumbnail(url=BRAND_IMAGE)
    return e


def money(x): return f"${x:,.2f}"


# ---------- panel ----------
def panel_embed():
    n, usd = db.execute("SELECT COUNT(*), COALESCE(SUM(usd), 0) FROM orders WHERE status='DONE'").fetchone()
    e = discord.Embed(title=f"{BRAND} 💱", color=BLURPLE, description=(
        "Click below to create your own private crypto swap ticket.\n\n"
        "💹 **Record**\n"
        f"**{n:,}** swaps completed  ·  **{money(usd)}** exchanged\n\n"
        "1️⃣ **Press Start a swap**\n"
        "2️⃣ **Pick your coins and type an amount in USD**\n"
        "3️⃣ **Send the deposit and track it live**\n\n"
        f"📖 Read <#{RULES_CHANNEL}> before every swap"))
    e.set_footer(text="Powered by FixedFloat  ·  non-custodial")
    return brand(e)


async def update_panels():
    """Refresh the Record line on every posted panel."""
    for ch_id, msg_id in db.execute("SELECT panel_channel, panel_msg FROM config "
                                    "WHERE panel_msg IS NOT NULL").fetchall():
        ch = bot.get_channel(ch_id)
        if not ch: continue
        try: await ch.get_partial_message(msg_id).edit(embed=panel_embed())
        except discord.HTTPException as e: print("panel update failed", e)


class PanelView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="Start a swap", emoji="💱", style=discord.ButtonStyle.green, custom_id="ff:open")
    async def open(self, i: discord.Interaction, b):
        g, uid = i.guild, i.user.id
        cfg = db.execute("SELECT category_id, staff_role_id FROM config WHERE guild_id=?", (g.id,)).fetchone()
        if not cfg:
            return await i.response.send_message("Not set up yet. An admin must run /setup.", ephemeral=True)
        row = db.execute("SELECT channel_id FROM tickets WHERE user_id=? AND guild_id=?", (uid, g.id)).fetchone()
        if row:
            if g.get_channel(row[0]):
                return await i.response.send_message(f"You already have a ticket: <#{row[0]}>", ephemeral=True)
            db.execute("DELETE FROM tickets WHERE channel_id=?", (row[0],)); db.commit()
        if uid in opening: return await i.response.defer()
        opening.add(uid)
        try:
            await i.response.defer(ephemeral=True)
            pv = await pending_vouch(uid)  # must vouch for the last finished swap first
            if pv:
                await i.followup.send(embed=vouch_required_embed(pv), ephemeral=True)
                return await i.followup.send(pv, ephemeral=True)  # plain text so it's easy to copy
            ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
                  g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                    embed_links=True, manage_channels=True,
                                                    manage_messages=True),
                  i.user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                      read_message_history=True)}
            role = g.get_role(cfg[1]) if cfg[1] else None
            if role: ow[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            try:
                ch = await g.create_text_channel(f"swap-{i.user.name}"[:90], category=g.get_channel(cfg[0]),
                                                 overwrites=ow)
            except discord.HTTPException as e:
                return await i.followup.send(f"Couldn't create your ticket: {e}", ephemeral=True)
            db.execute("INSERT INTO tickets VALUES(?,?,?,?)", (ch.id, uid, g.id, time.time())); db.commit()
            e = discord.Embed(title="🎫 Your private swap ticket", color=BLURPLE, description=(
                "Only you and staff can see this channel.\n"
                f"📖 Read <#{RULES_CHANNEL}> before your swap.\n\n"
                "Your swap menu is right below. Lost it? Press **Open menu**."))
            await ch.send(i.user.mention, embed=brand(e), view=TicketView())
            await send_menu(ch, i.user)  # terms first if this is their first swap, otherwise straight to the menu
            await i.followup.send(f"Your ticket: {ch.mention}", ephemeral=True)
        finally:
            opening.discard(uid)

    @discord.ui.button(label="Available coins", emoji="🪙", style=discord.ButtonStyle.secondary, custom_id="ff:coins")
    async def coins(self, i: discord.Interaction, b):
        if not ccies:
            return await i.response.send_message("Coin list is still loading, try again in a moment.", ephemeral=True)
        v = CoinsView()
        await i.response.send_message(embed=v.embed(), view=v, ephemeral=True)


# ---------- vouches + swap log ----------
def usd_text(x): return f"${x:,.0f}" if abs(x - round(x)) < 0.005 else f"${x:,.2f}"


def swap_label(code):
    c = next((c for c in ccies if c["code"] == code), None)
    if not c: return (code or "?").lower()
    t = ticker(c).lower()
    return f'{t} {c["network"].lower()}' if c["network"] and multi_net(ticker(c)) else t


def is_vouch(text): return (text or "").lstrip().lower().startswith("+vouch")


async def mark_vouched(uid, upto):
    """A +vouch posted at time `upto` clears every finished swap of that user made before it."""
    rows = db.execute("SELECT id, log_msg FROM orders WHERE user_id=? AND status='DONE' AND vouched=0 "
                      "AND created<=?", (uid, upto)).fetchall()
    for oid, log_msg in rows:
        db.execute("UPDATE orders SET vouched=1 WHERE id=?", (oid,)); db.commit()
        ch = bot.get_channel(LOG_CHANNEL)
        if ch and log_msg:
            try:
                msg = await ch.fetch_message(log_msg)
                e = msg.embeds[0]
                e.color = GREEN
                e.set_footer(text="✅ Vouched")
                await msg.edit(embed=e)
            except (discord.HTTPException, IndexError): pass
    return bool(rows)


_scan_errs: set = set()


async def scan_vouches(uid=None):
    """Read the vouch channel (and the log channel) and clear every customer who posted a +vouch after
    their swap. Returns False if no channel could be read. Only looks back to the oldest pending swap."""
    sql, args = "SELECT user_id, MIN(created) FROM orders WHERE status='DONE' AND vouched=0", ()
    if uid: sql, args = sql + " AND user_id=?", (uid,)
    pending = dict(db.execute(sql + " GROUP BY user_id", args).fetchall())
    if not pending: return True
    after = datetime.fromtimestamp(min(pending.values()) - 5, tz=timezone.utc)
    ok = False
    for cid in {VOUCH_CHANNEL, LOG_CHANNEL}:
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
            async for m in ch.history(limit=500, after=after, oldest_first=False):
                if m.author.id in pending and is_vouch(m.content) \
                        and await mark_vouched(m.author.id, m.created_at.timestamp()):
                    try: await m.add_reaction("✅")
                    except discord.HTTPException: pass
            ok = True
        except (discord.HTTPException, AttributeError) as e:
            if (cid, str(e)) not in _scan_errs:  # print each distinct problem once, not every 5 seconds
                _scan_errs.add((cid, str(e))); print("vouch scan failed", cid, e)
    return ok


async def pending_vouch(uid):
    """The +vouch line of the user's unvouched finished swap, or None. Scans the channel first."""
    if not VOUCH_REQUIRED: return None
    if not await scan_vouches(uid):
        print("vouch channel unreadable, not enforcing vouches"); return None
    r = db.execute("SELECT vouch_text FROM orders WHERE user_id=? AND status='DONE' AND vouched=0 "
                   "ORDER BY created DESC LIMIT 1", (uid,)).fetchone()
    return (r[0] or "+vouch") if r else None


def vouch_required_embed(text):
    e = discord.Embed(title="⭐ Vouch first", color=GOLD, description=(
        "Before you start another swap, please vouch for your last one.\n\n"
        f"Paste this in <#{VOUCH_CHANNEL}>:\n```{text}```\nThen press **Start a swap** again."))
    return brand(e)


def thanks_embed(text):
    e = discord.Embed(title="🎉 Thank you for swapping!", color=GREEN, description=(
        "Your swap is complete. We'd really appreciate a vouch!\n\n"
        f"1️⃣ Press **Vouch** below to get your message\n2️⃣ Paste it in <#{VOUCH_CHANNEL}>\n\n"
        f"```{text}```"
        + ("\n⚠️ You need to vouch before you can make another swap." if VOUCH_REQUIRED else "")))
    return brand(e)


class VouchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        vc = bot.get_channel(VOUCH_CHANNEL)
        if vc and getattr(vc, "guild", None):
            self.add_item(discord.ui.Button(label="Go to vouches", style=discord.ButtonStyle.link,
                                            url=f"https://discord.com/channels/{vc.guild.id}/{VOUCH_CHANNEL}"))

    @discord.ui.button(label="Vouch", emoji="⭐", style=discord.ButtonStyle.success, custom_id="ff:vouch")
    async def vouch(self, i: discord.Interaction, b):
        r = db.execute("SELECT vouch_text FROM orders WHERE user_id=? AND status='DONE' AND vouch_text IS NOT NULL "
                       "ORDER BY created DESC LIMIT 1", (i.user.id,)).fetchone()
        if not r:
            return await i.response.send_message("I couldn't find a finished swap for you.", ephemeral=True)
        await i.response.send_message(r[0], ephemeral=True)  # plain text so it's easy to copy
        await i.followup.send(f"Copy the message above and paste it in <#{VOUCH_CHANNEL}>. Thank you! 💙", ephemeral=True)


async def send_thanks(uid, ch_id, o):
    r = db.execute("SELECT vouch_text FROM orders WHERE id=?", (o["id"],)).fetchone()
    if not r or not r[0]: return
    e, view = thanks_embed(r[0]), VouchView()
    try:
        ch = bot.get_channel(ch_id) if ch_id else None
        if ch: await ch.send(f"<@{uid}>", embed=e, view=view)
        else: await (await bot.fetch_user(uid)).send(embed=e, view=view)
    except discord.HTTPException as ex:
        print("thanks failed", uid, o["id"], ex)


def log_embed(uid, o, usd, vouched=False):
    f, t = o.get("from") or {}, o.get("to") or {}
    tx = o.get("tx") or {}
    tin, tout = (tx.get("in") or {}), (tx.get("out") or {})
    e = discord.Embed(title="✅ Swap completed", color=GREEN if vouched else GOLD, url=f'https://ff.io/order/{o["id"]}')
    e.set_author(name=f'{BRAND}  ·  Order {o["id"]}')
    e.add_field(name="👤 Customer", value=f"<@{uid}>\n`{uid}`")
    e.add_field(name="💱 Pair", value=f'{short(f.get("code", "?"))} ➜ {short(t.get("code", "?"))}')
    if usd: e.add_field(name="💵 Value", value=money(usd))
    e.add_field(name="📤 Sent", value=f'**{f.get("amount", "?")}** {f.get("code", "")}', inline=False)
    e.add_field(name="📥 Received", value=f'**{tout.get("amount") or t.get("amount", "?")}** {t.get("code", "")}')
    if LOG_TXIDS:
        if tin.get("id"): e.add_field(name="📥 Deposit TxID", value=f'```{tin["id"]}```', inline=False)
        if tout.get("id"): e.add_field(name="📤 Payout TxID", value=f'```{tout["id"]}```', inline=False)
    e.set_footer(text="✅ Vouched" if vouched else "⏳ Waiting for vouch")
    e.timestamp = discord.utils.utcnow()
    return brand(e)


async def log_swap(uid, o):
    ch = bot.get_channel(LOG_CHANNEL)
    if not ch:
        print("log channel not visible", LOG_CHANNEL); return
    r = db.execute("SELECT usd FROM orders WHERE id=?", (o["id"],)).fetchone()
    try:
        m = await ch.send(embed=log_embed(uid, o, r[0] if r else None))
        db.execute("UPDATE orders SET log_msg=? WHERE id=?", (m.id, o["id"])); db.commit()
    except discord.HTTPException as ex:
        print("log failed", o["id"], ex)


# ---------- terms (first swap only) ----------
def terms_embed():
    e = discord.Embed(title="📜 Terms of service", color=GOLD, description=(
        f"{TOS_TEXT}\n\n📖 Also read <#{RULES_CHANNEL}>.\n"
        "You only need to agree **once**. After that you go straight to swapping."))
    return brand(e)


class TermsView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=900)
        self.user = user

    async def interaction_check(self, i):
        if i.user.id == self.user.id: return True
        await i.response.send_message("Only the ticket owner can use this.", ephemeral=True)
        return False

    @discord.ui.button(label="I agree", emoji="✅", style=discord.ButtonStyle.success)
    async def agree(self, i: discord.Interaction, b):
        db.execute("INSERT OR IGNORE INTO agreed VALUES(?,?)", (i.user.id, time.time())); db.commit()
        self.stop()
        w = Wizard(i.user, i.channel_id)
        w.msg, w.last_i = i.message, i
        await i.response.edit_message(embed=w.embed(), view=w)  # this message turns into the swap menu

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.grey)
    async def decline(self, i: discord.Interaction, b):
        await i.response.send_message("No problem. You need to accept the terms to swap. "
                                      "You can close this ticket any time.", ephemeral=True)


async def send_menu(ch, user):
    """Post the terms (first swap) or the swap menu in a ticket channel."""
    if has_agreed(user.id):
        w = Wizard(user, ch.id)
        w.msg = await ch.send(embed=w.embed(), view=w)
    else:
        await ch.send(embed=terms_embed(), view=TermsView(user))


class TicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="Open menu", emoji="💱", style=discord.ButtonStyle.green, custom_id="ff:start")
    async def start(self, i: discord.Interaction, b):
        t = db.execute("SELECT user_id FROM tickets WHERE channel_id=?", (i.channel_id,)).fetchone()
        if not t or t[0] != i.user.id:
            return await i.response.send_message("Only the ticket owner can start a swap.", ephemeral=True)
        if latest_order(i.channel_id):
            return await i.response.send_message("This ticket already has a swap. Open a new ticket once it finishes.", ephemeral=True)
        if not ccies:
            return await i.response.send_message("Coin list is still loading, try again in a moment.", ephemeral=True)
        if not has_agreed(i.user.id):
            return await i.response.send_message(embed=terms_embed(), view=TermsView(i.user))
        w = Wizard(i.user, i.channel_id)
        w.last_i = i
        await i.response.send_message(embed=w.embed(), view=w)
        try: w.msg = await i.channel.fetch_message((await i.original_response()).id)
        except discord.HTTPException: pass

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.grey, custom_id="ff:close")
    async def close(self, i: discord.Interaction, b):
        t = db.execute("SELECT user_id FROM tickets WHERE channel_id=?", (i.channel_id,)).fetchone()
        if not t or (i.user.id != t[0] and not i.permissions.manage_channels):
            return await i.response.send_message("Only the ticket owner can close this.", ephemeral=True)
        o = latest_order(i.channel_id)
        if o and o[3] not in FINAL:
            await i.response.defer(ephemeral=True)
            try:
                st = (await ff("order", id=o[0], token=o[1]))["status"]  # live status, not the saved one
            except (FFError, KeyError, aiohttp.ClientError, asyncio.TimeoutError):
                st = None
            if st == "NEW":
                text = ("Cancel this order and close the ticket?\n**Don't send a deposit to its address afterwards.** "
                        "If you've already sent one, press Keep order.")
            elif st in FINAL:
                text = "This order is finished. Close the ticket?"
            elif st is None:
                text = ("I couldn't check this order's status right now. Closing the ticket doesn't cancel anything on "
                        "FixedFloat, and I'll keep DMing you updates. If you've already sent a deposit, press Keep order.")
            else:
                return await i.followup.send("A swap is in progress (your deposit was detected). Wait until it finishes, "
                                             "or use the red button if it's stuck.", ephemeral=True)
            return await i.followup.send(text, view=CancelConfirm(i.user.id, force=st is None), ephemeral=True)
        await i.response.send_message("Closing...", ephemeral=True)
        db.execute("DELETE FROM tickets WHERE channel_id=?", (i.channel_id,))
        db.execute("UPDATE orders SET channel_id=NULL WHERE channel_id=?", (i.channel_id,)); db.commit()
        await i.channel.delete(reason="ticket closed")


@bot.tree.command(name="setup", description="Post the swap panel in this channel")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@app_commands.describe(category="Category for ticket channels (created if empty)", staff_role="Role that can see tickets")
async def setup(inter: discord.Interaction, category: discord.CategoryChannel | None = None,
                staff_role: discord.Role | None = None):
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    try:
        if category is None:
            category = await g.create_category("Swap Tickets", overwrites={
                g.default_role: discord.PermissionOverwrite(view_channel=False),
                g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)})
        msg = await inter.channel.send(embed=panel_embed(), view=PanelView())
    except discord.HTTPException as ex:
        return await inter.followup.send(f"Setup failed (check my permissions: Manage Channels, "
                                         f"Send Messages, Embed Links): {ex}", ephemeral=True)
    db.execute("INSERT OR REPLACE INTO config(guild_id, category_id, staff_role_id, panel_channel, panel_msg) "
               "VALUES(?,?,?,?,?)",
               (g.id, category.id, staff_role.id if staff_role else None, inter.channel.id, msg.id)); db.commit()
    await inter.followup.send("Panel posted.", ephemeral=True)


# ---------- swap ----------
POP = ["BTC", "ETH", "LTC", "USDT", "USDC", "XMR", "SOL", "BNB", "XRP", "DOGE", "TRX", "TON", "ADA", "BCH",
       "DASH", "ZEC", "AVAX", "DOT", "XLM", "ETC", "MATIC", "DAI", "SHIB", "LINK", "ATOM"]
NET_ORDER = ["TRC20", "BEP20", "ERC20", "SOL", "POLYGON", "ARBITRUM", "OPTIMISM", "BASE", "AVAXC", "TON"]
NET_INFO = {  # network key -> (full name, how people write it)
    "BEP20": ("BNB Smart Chain", "BSC / BNB / BEP20"),
    "TRC20": ("Tron", "TRX / Tron / TRC20"),
    "ERC20": ("Ethereum", "ETH / Ethereum / ERC20"),
    "SOL": ("Solana", "SOL / Solana / SPL"),
    "POLYGON": ("Polygon", "MATIC / POL / Polygon"),
    "ARBITRUM": ("Arbitrum One", "ARB / Arbitrum"),
    "OPTIMISM": ("Optimism", "OP / Optimism"),
    "BASE": ("Base", "Base / Coinbase L2"),
    "AVAXC": ("Avalanche C-Chain", "AVAX / AVAXC / C-Chain"),
    "TON": ("TON", "TON / The Open Network"),
}
NET_ALIAS = {"BEP20": ("BEP20", "BSC", "BNB"), "TRC20": ("TRC20", "TRX", "TRON"), "ERC20": ("ERC20", "ETH"),
             "SOL": ("SOL", "SOLANA"), "POLYGON": ("POLYGON", "MATIC", "POL"), "ARBITRUM": ("ARBITRUM", "ARB"),
             "OPTIMISM": ("OPTIMISM", "OP"), "BASE": ("BASE",), "AVAXC": ("AVAXC", "AVAX"), "TON": ("TON",)}
COIN_INFO = {
    "BTC": "Bitcoin · the original cryptocurrency", "ETH": "Ethereum · smart-contract platform",
    "LTC": "Litecoin · fast, low-fee Bitcoin alternative", "USDT": "Tether · US dollar stablecoin (1 USDT ≈ $1)",
    "USDC": "USD Coin · US dollar stablecoin (1 USDC ≈ $1)", "XMR": "Monero · private digital cash",
    "SOL": "Solana · fast smart-contract blockchain", "BNB": "BNB · native coin of BNB Smart Chain",
    "XRP": "XRP · fast payments coin (may need a destination tag)", "DOGE": "Dogecoin · the original meme coin",
    "TRX": "Tron · low-fee smart-contract blockchain", "TON": "Toncoin · Telegram's blockchain",
    "ADA": "Cardano · proof-of-stake blockchain", "BCH": "Bitcoin Cash · Bitcoin fork with low fees",
    "DASH": "Dash · fast digital cash", "ZEC": "Zcash · privacy-focused coin",
    "AVAX": "Avalanche · fast smart-contract blockchain", "DOT": "Polkadot · multi-chain network",
    "XLM": "Stellar · low-cost payments network (may need a memo)", "ETC": "Ethereum Classic · the original Ethereum chain",
    "MATIC": "Polygon · Ethereum scaling network", "DAI": "Dai · decentralized US dollar stablecoin",
    "SHIB": "Shiba Inu · meme token", "LINK": "Chainlink · oracle network", "ATOM": "Cosmos · network of connected chains",
}
# Popular pairs shown on the start screen: ((coin, network or None), (coin, network or None)). Edit freely.
PAIRS = [(("LTC", None), ("USDT", "BEP20")), (("USDT", "BEP20"), ("LTC", None)),
         (("BTC", None), ("USDT", "TRC20")), (("USDT", "TRC20"), ("BTC", None))]


def ticker(c): return (c.get("coin") or c["code"])


def groups(field):
    d = {}
    for c in ccies:
        if c.get(field): d.setdefault(ticker(c), []).append(c)
    return d


def multi_net(tick): return len({c["network"] for c in ccies if ticker(c) == tick}) > 1


def find(field, tick, net=None):
    nets = NET_ALIAS.get(net, (net,)) if net else ()
    for c in ccies:
        if not c.get(field) or ticker(c).upper() != tick: continue
        if not net or c["network"].upper() in nets or c["code"].upper() in [tick + n for n in nets]: return c


def short(code):
    c = next((c for c in ccies if c["code"] == code), None)
    if not c: return code or "-"
    t = ticker(c)
    return f'{t} ({c["network"]})' if c["network"] and multi_net(t) else t


def ticker_of(code):
    c = next((c for c in ccies if c["code"] == code), None)
    return ticker(c) if c else code


def net_key(n):
    u = (n or "").upper()
    return next((k for k, al in NET_ALIAS.items() if u in al), u)


def net_desc(n):
    info = NET_INFO.get(net_key(n))
    return f"{info[1]} · {info[0]}" if info else None


def clean_name(n): return re.sub(r"\s*\(.*?\)\s*$", "", n or "")


def coin_pool(field, query=""):
    g, q = groups(field), query.lower().strip()
    keys = [k for k, v in g.items() if not q or q in k.lower() or any(
        q in x["name"].lower() or q in x["network"].lower() or q in x["code"].lower() for x in v)]
    keys.sort(key=lambda k: (POP.index(k) if k in POP else 999, k))
    return keys[:25], len(keys)


def coin_label(code):
    c = next((c for c in ccies if c["code"] == code), None)
    return f'{c["name"]} ({c["network"]})' if c else (code or "-")


# ---------- available coins (ephemeral browser) ----------
def coin_catalog(query=""):
    d = {}
    for c in ccies: d.setdefault(ticker(c), []).append(c)
    q = query.lower().strip()
    keys = [k for k, v in d.items() if not q or q in k.lower() or any(
        q in x["name"].lower() or q in x["network"].lower() for x in v)]
    keys.sort(key=lambda k: (POP.index(k) if k in POP else 999, k))
    return d, keys


class CoinSearch(discord.ui.Modal):
    def __init__(self, v):
        super().__init__(title="Search coins")
        self.v = v
        self.q = discord.ui.TextInput(label="Coin name or ticker (empty = all)", required=False, max_length=40)
        self.add_item(self.q)

    async def on_submit(self, i: discord.Interaction):
        self.v.query, self.v.page = self.q.value.strip(), 0
        self.v.sync()
        await i.response.edit_message(embed=self.v.embed(), view=self.v)


class CoinsView(discord.ui.View):
    """Paged list of every coin we can swap. Only the person who pressed the button sees it."""
    PER = 12

    def __init__(self, query=""):
        super().__init__(timeout=300)
        self.query, self.page, self.pages = query, 0, 1
        self.sync()

    def sync(self):
        _, keys = coin_catalog(self.query)
        self.pages = max(1, math.ceil(len(keys) / self.PER))
        self.page = max(0, min(self.page, self.pages - 1))
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages - 1

    def embed(self):
        d, keys = coin_catalog(self.query)
        chunk = keys[self.page * self.PER:(self.page + 1) * self.PER]
        lines = []
        for k in chunk:
            es = d[k]
            s, r = any(c.get("send") for c in es), any(c.get("recv") for c in es)
            flag = "↔️" if s and r else "📤" if s else "📥"
            line = f"{flag} **{k}** · {clean_name(es[0]['name'])}"
            nets = sorted({c["network"] for c in es if c["network"]}, key=lambda n: (
                NET_ORDER.index(n.upper()) if n.upper() in NET_ORDER else 99, n))
            if len(nets) > 1: line += "\n┗ " + "  ·  ".join(nets)
            lines.append(line)
        title = f"🪙 Available coins ({len(keys)})" + (f' · "{self.query}"' if self.query else "")
        e = discord.Embed(title=title, color=BLURPLE, description="\n".join(lines) or "No coins found. Try another name.")
        e.set_footer(text=f"Page {self.page + 1}/{self.pages}  ·  ↔️ send & receive  ·  📤 send only  ·  📥 receive only")
        return brand(e)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, i: discord.Interaction, b):
        self.page -= 1; self.sync()
        await i.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, i: discord.Interaction, b):
        self.page += 1; self.sync()
        await i.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Search", emoji="🔍", style=discord.ButtonStyle.primary)
    async def search(self, i: discord.Interaction, b):
        await i.response.send_modal(CoinSearch(self))


class SearchModal(discord.ui.Modal):
    def __init__(self, w):
        super().__init__(title="Search coins")
        self.w = w
        self.q = discord.ui.TextInput(label="Coin name or ticker (empty = all)", required=False, max_length=40)
        self.add_item(self.q)

    async def on_submit(self, i: discord.Interaction):
        w, text = self.w, self.q.value.strip()
        if text and not coin_pool(w.step, text)[1]:
            return await i.response.send_message("No coins found. Try another name.", ephemeral=True)
        w.query = text
        await w._go(i, w.step)


def fmt(x): return f"{x:.8f}".rstrip("0").rstrip(".")


def parse_usd(text):
    t = text.strip()
    m = re.fullmatch(r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)\s*(?:usd|dollars?|\$)?", t, re.I)
    if not m: return None
    n = m.group(1)
    n = n.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", n) else n.replace(",", ".")
    return float(n)


FF_ERRORS = {"LIMIT_MIN": "that's below FixedFloat's minimum for this pair",
             "LIMIT_MAX": "that's above FixedFloat's maximum for this pair"}
STABLE = ("USDT", "USDC", "DAI", "TUSD", "FDUSD", "PYUSD")
_prices: dict = {}  # code -> (time, USD per coin)


def ff_errors(errs):
    if isinstance(errs, str): errs = [errs]
    return "; ".join(FF_ERRORS.get(str(e), str(e)) for e in errs)


async def coin_usd_price(code):
    """USD value of one coin: mid-point of FixedFloat quotes against USDT. Measured on a big enough
    amount to be above any pair minimum (a tiny amount can hit the reference pair's own minimum)."""
    if code.startswith(STABLE): return 1.0
    hit = _prices.get(code)
    if hit and time.time() - hit[0] < 45: return hit[1]
    ref = next((k for k in ("USDTTRC", "USDTETH", "USDTBSC", "USDTSOL")
                if any(c["code"] == k and c.get("send") and c.get("recv") for c in ccies)), None)
    if not ref: raise FFError("USD pricing is unavailable right now")
    last = None
    for probe in (100, 500, 2500):
        hi = await ff("price", fromCcy=code, toCcy=ref, amount=fmt(probe), direction="to", type="float")
        lo = await ff("price", fromCcy=ref, toCcy=code, amount=fmt(probe), direction="from", type="float")
        errs = list(hi.get("errors") or []) + list(lo.get("errors") or [])
        if errs:
            last = errs; continue
        coin = (float(hi["from"]["amount"]) + float(lo["to"]["amount"])) / 2
        if coin <= 0: raise FFError("couldn't price that coin")
        _prices[code] = (time.time(), probe / coin)
        return probe / coin
    raise FFError(ff_errors(last))


async def usd_to_coin(code, usd):
    """How much of `code` is worth `usd` dollars."""
    return fmt(usd / await coin_usd_price(code))


class AmountModal(discord.ui.Modal):
    def __init__(self, w):
        super().__init__(title="How much?")
        self.w = w
        self.amount = discord.ui.TextInput(label="Amount in USD (e.g. 100)", max_length=16)
        self.add_item(self.amount)

    async def on_submit(self, i: discord.Interaction):
        w = self.w
        usd = parse_usd(self.amount.value)
        if usd is None or usd <= 0:
            return await i.response.send_message("That isn't a valid USD amount. Press Enter amount and try again.", ephemeral=True)
        await i.response.defer()
        err = await w.set_usd(usd)
        await w._go(i, "amount" if err else "address", err, deferred=True)


class AddressModal(discord.ui.Modal):
    def __init__(self, w):
        super().__init__(title="Receiving address")
        self.w = w
        self.address = discord.ui.TextInput(label=f"Your {w.recv} address"[:45], max_length=200)
        self.add_item(self.address)

    async def on_submit(self, i: discord.Interaction):
        w = self.w
        w.address, w.memo = self.address.value.strip(), None
        await i.response.defer()
        await w.to_confirm(i)


class MemoModal(discord.ui.Modal):
    def __init__(self, w):
        super().__init__(title="Memo / tag")
        self.w = w
        tag = (w.dst() or {}).get("tag") or "memo"
        self.memo = discord.ui.TextInput(label=f"{tag}"[:45], max_length=100)
        self.add_item(self.memo)

    async def on_submit(self, i: discord.Interaction):
        self.w.memo = self.memo.value.strip()
        await i.response.defer()
        await self.w.to_confirm(i)


class Wizard(discord.ui.View):
    """One message in the ticket: start (popular pairs) -> coin -> network -> coin -> network -> amount -> address -> confirm."""
    TITLES = {"home": "💱 Start a swap", "send": "📤 Which coin are you sending?", "send_net": "🌐 Which network?",
              "recv": "📥 Which coin do you want to receive?", "recv_net": "🌐 Which network?",
              "amount": "💵 How much do you want to swap?", "address": "📮 Where should we send it?",
              "memo": "🏷️ One more detail", "confirm": "🔎 Review your swap"}
    STEPNO = {"send": 1, "send_net": 1, "recv": 2, "recv_net": 2, "amount": 3, "address": 4, "memo": 4, "confirm": 5}

    def __init__(self, user, channel_id=None):
        super().__init__(timeout=900)
        self.user, self.channel_id, self.last_i, self.busy, self.usd = user, channel_id, None, False, None
        self.step, self.query, self.notice, self.pair, self.saved = "home", "", None, False, None
        self.msg = None  # the menu message (so it can be resent at the bottom)
        self.send = self.recv = self.send_coin = self.recv_coin = None
        self.amount = self.address = self.memo = self.est = self.limits = None
        self.render()

    async def interaction_check(self, i):
        if i.user.id == self.user.id: return True
        await i.response.send_message("Only the ticket owner can use this menu.", ephemeral=True)
        return False

    def dst(self): return next((c for c in ccies if c["code"] == self.recv), None)
    def needs_memo(self): return bool((self.dst() or {}).get("tag"))

    def reset(self):
        self.query, self.pair, self.usd = "", False, None
        self.send = self.recv = self.send_coin = self.recv_coin = None
        self.amount = self.address = self.memo = self.est = self.limits = None

    def entries(self, side, tick):
        es = [c for c in ccies if c.get(side) and ticker(c) == tick]
        return sorted(es, key=lambda c: NET_ORDER.index(c["network"].upper()) if c["network"].upper() in NET_ORDER else 99)

    def pairs(self):
        out = []
        for x, y in PAIRS:
            a, b = find("send", *x), find("recv", *y)
            if a and b and a["code"] != b["code"]: out.append((a, b))
        return out

    def prev_step(self):
        st = self.step
        if st == "send": return "home"
        if st == "send_net": return "send"
        if st == "recv": return "send_net" if len(self.entries("send", self.send_coin)) > 1 else "send"
        if st == "recv_net": return "recv"
        if st == "amount":
            if self.pair: return "home"
            return "recv_net" if len(self.entries("recv", self.recv_coin)) > 1 else "recv"
        return "address" if st in ("memo", "confirm") else "amount"

    def _btn(self, label, cb, style=discord.ButtonStyle.secondary, row=None, disabled=False):
        b = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        b.callback = cb
        self.add_item(b)

    def _select(self, placeholder, options, cb, row=0):
        sel = discord.ui.Select(placeholder=placeholder, options=options, row=row)
        async def _cb(i): await cb(i, sel.values[0])
        sel.callback = _cb
        self.add_item(sel)

    def render(self):
        self.clear_items()
        st = self.step
        self.saved = get_saved(self.user.id, self.recv) if st == "address" and self.recv else None
        if st == "home":
            ps = self.pairs()
            for n, (a, b) in enumerate(ps):
                self._btn(f'{short(a["code"])} ➜ {short(b["code"])}'[:80], self._pair(a, b), row=n // 2)
            self._btn("🔎 Custom swap", self.custom, discord.ButtonStyle.primary, row=(len(ps) + 1) // 2)
            return
        if st in ("send", "recv"):
            by = groups(st)
            keys, _ = coin_pool(st, self.query)
            opts = [discord.SelectOption(label=k, description=(COIN_INFO.get(k) or clean_name(by[k][0]["name"]))[:100] or None, value=k) for k in keys]
            if opts: self._select("Choose a coin…", opts, self.pick_coin)
            self._btn("🔍 Search", self.open_search, row=1)
            self._btn("Back", self.back, row=1)
            return
        if st in ("send_net", "recv_net"):
            side = st[:-4]
            es = self.entries(side, self.send_coin if side == "send" else self.recv_coin)
            opts = [discord.SelectOption(label=c["network"] or c["code"], value=c["code"],
                                         description=net_desc(c["network"])) for c in es][:25]
            self._select("Choose a network…", opts, self.pick_net)
            self._btn("Back", self.back, row=1)
            return
        if st == "address" and self.saved:
            self._btn("✅ Use saved address", self.use_saved, discord.ButtonStyle.success)
            self._btn("Enter new address", self.ask_address)
            self._btn("Back", self.back)
            self._btn("Cancel", self.cancel, discord.ButtonStyle.danger)
            self._btn("🗑 Forget saved", self.forget)
            return
        main = {"amount": ("Enter amount", self.ask_amount), "address": ("Enter address", self.ask_address),
                "memo": ("Enter memo", self.ask_memo), "confirm": ("Confirm swap", self.confirm)}[st]
        self._btn(main[0], main[1], discord.ButtonStyle.success)
        self._btn("Back", self.back)
        self._btn("Cancel", self.cancel, discord.ButtonStyle.danger)

    def embed(self):
        st = self.step
        n = self.STEPNO.get(st)
        e = discord.Embed(title=self.TITLES[st], color=GREEN if st == "confirm" else BLURPLE)
        e.set_author(name=BRAND if n is None else f"{BRAND}  ·  Step {n} of 5   {'▰' * n}{'▱' * (5 - n)}")
        e.set_footer(text="Powered by FixedFloat" if st == "home" else
                     "Back returns to the previous step" if st in ("send", "recv", "send_net", "recv_net") else "Cancel starts over")
        brand(e)
        lines = []
        if self.notice: lines.append(f"⚠️ **{self.notice}**")
        if st == "home":
            lines.append("Pick a popular pair to start fast, or build your own swap.\n"
                         "Next you'll type the amount in USD, then your address.")
        elif st in ("send", "recv"):
            _, total = coin_pool(st, self.query)
            lines.append(f"Choose the coin you want to **{'send' if st == 'send' else 'receive'}**, or use **🔍 Search**."
                         if not self.query else f'Results for **"{self.query}"**. Search with an empty box to see all coins.')
            if total > 25: lines.append(f"Showing the first 25 of {total}. Narrow it down with Search.")
        elif st in ("send_net", "recv_net"):
            tick = self.send_coin if st == "send_net" else self.recv_coin
            if COIN_INFO.get(tick): lines.append(f"💡 **{tick}** · {COIN_INFO[tick]}")
            lines.append(f"**{tick}** runs on more than one network. Pick the network your wallet "
                         f"{'sends on' if st == 'send_net' else 'should receive on'}. The wrong network can lose funds.")
        elif st == "amount":
            lines.append(("💬 **Type the amount in USD in this chat** (for example `100`), or press **Enter amount**."
                          if CHAT_INPUT else "Press **Enter amount** and type how much you want to swap in USD.")
                         + f"\nI'll work out how much {short(self.send)} that is.")
        elif st == "address":
            what = f"your **{coin_label(self.recv)}** address"
            lines.append((f"💬 **Type {what} in this chat**, or press **Enter " + ("new " if self.saved else "") + "address**."
                          if CHAT_INPUT else f"Press **Enter address** and paste {what}.")
                         + "\nDouble-check it: swaps can't be reversed.")
        elif st == "memo":
            tag = (self.dst() or {}).get("tag") or "memo"
            lines.append(f"{coin_label(self.recv)} needs a **{tag}**. Press **Enter memo** and paste it exactly: "
                         "a wrong or missing one can lose funds.")
        else:
            lines.append("Check everything below, then press **Confirm swap**.")
            e.set_footer(text=f"Rate: {RATE_TYPE}  ·  the final amount can differ slightly")
        e.description = "\n\n".join(lines) or None
        if self.send:
            val = f"**{self.amount}** {short(self.send)}" if self.amount else short(self.send)
            if self.amount and self.usd: val += f"\n≈ {money(self.usd)}"
            e.add_field(name="📤 You send", value=val)
        if self.recv:
            e.add_field(name="📥 You receive", value=f"≈ **{self.est}** {short(self.recv)}" if self.est else short(self.recv))
        if st == "amount" and self.limits:
            mn, mx, mnc, mxc = self.limits
            parts = []
            if mn is not None: parts.append(f"▫️ **Minimum** {money(mn)} ({mnc} {ticker_of(self.send)})")
            if mx is not None: parts.append(f"▫️ **Maximum** {money(mx)} ({mxc} {ticker_of(self.send)})")
            if parts: e.add_field(name="📏 Limits for this swap", inline=False, value="\n".join(parts) + (
                "\n*The minimum includes a small buffer for network fees.*" if mn is not None else ""))
        if st in ("send_net", "recv_net"):
            tick = self.send_coin if st == "send_net" else self.recv_coin
            rows = [f"**{c['network']}** · {net_desc(c['network']) or 'network'}" for c in self.entries(st[:-4], tick)]
            if rows: e.add_field(name="Available networks", value="\n".join(rows)[:1000], inline=False)
        if st == "address" and self.saved:
            e.add_field(name="💾 Saved address", value=f"```{self.saved['address']}```"
                        + (f"\nMemo: `{self.saved['memo']}`" if self.saved["memo"] else ""), inline=False)
        if st == "confirm":
            e.add_field(name="📮 Payout address", value=f"```{self.address}```", inline=False)
            if self.memo: e.add_field(name="🏷️ Memo / tag", value=f"```{self.memo}```", inline=False)
        return e

    async def _go(self, i, step, notice=None, deferred=False):
        """Show `step`. Edits the menu in place, or resends it at the bottom if newer messages were posted."""
        self.step, self.notice = step, notice
        if i is not None: self.last_i = i
        if step in ("amount", "address"): waiting[self.channel_id] = self
        elif waiting.get(self.channel_id) is self: del waiting[self.channel_id]
        self.render()
        ch = bot.get_channel(self.channel_id)
        stale = bool(ch and self.msg and getattr(ch, "last_message_id", None) not in (None, self.msg.id))
        if stale:
            if i is not None and not i.response.is_done(): await i.response.defer()
            new = await ch.send(embed=self.embed(), view=self)
            try: await ch.get_partial_message(self.msg.id).delete()
            except discord.HTTPException: pass
            self.msg = new
        elif i is None and self.msg: await self.msg.edit(embed=self.embed(), view=self)
        elif deferred or i is None: await (i or self.last_i).edit_original_response(embed=self.embed(), view=self)
        else: await i.response.edit_message(embed=self.embed(), view=self)

    # ----- start screen / coin + network selection
    def _pair(self, a, b):
        async def cb(i):
            self.reset()
            self.send, self.recv, self.send_coin, self.recv_coin, self.pair = a["code"], b["code"], ticker(a), ticker(b), True
            await i.response.defer()
            await self.load_limits()
            await self._go(i, "amount", deferred=True)
        return cb

    async def custom(self, i):
        self.reset()
        await self._go(i, "send")

    async def pick_coin(self, i, tick):
        side = "send" if self.step == "send" else "recv"
        setattr(self, f"{side}_coin", tick)
        self.query = ""
        es = self.entries(side, tick)
        if len(es) == 1: await self.set_code(i, side, es[0]["code"])
        else: await self._go(i, f"{side}_net")

    async def pick_net(self, i, code):
        await self.set_code(i, "send" if self.step == "send_net" else "recv", code)

    async def set_code(self, i, side, code):
        if side == "send":
            self.send = code
            return await self._go(i, "recv")
        if code == self.send:
            return await self._go(i, self.step, "Pick something different from the coin you're sending.")
        self.recv = code
        await i.response.defer()
        await self.load_limits()
        await self._go(i, "amount", deferred=True)

    async def open_search(self, i): await i.response.send_modal(SearchModal(self))
    async def ask_amount(self, i): await i.response.send_modal(AmountModal(self))
    async def ask_address(self, i): await i.response.send_modal(AddressModal(self))
    async def ask_memo(self, i): await i.response.send_modal(MemoModal(self))

    async def back(self, i):
        self.query = ""
        await self._go(i, self.prev_step())

    async def cancel(self, i):
        self.reset()
        await self._go(i, "home")

    # ----- pricing
    async def load_limits(self):
        """Probe a ~$100 quote for this pair and read FixedFloat's min/max (converted to USD)."""
        self.limits = None
        try:
            per = await coin_usd_price(self.send)  # USD value of one coin
            q = await ff("price", fromCcy=self.send, toCcy=self.recv, amount=fmt(100 / per),
                         direction="from", type=RATE_TYPE)
            f = q["from"]
            mn, mx = f.get("min"), f.get("max")
            mn_usd = (round(math.ceil(round(float(mn) * per * 100, 6)) / 100 + MIN_BUFFER_USD, 2)
                      if mn not in (None, "") else None)
            mx_usd = float(mx) * per if mx not in (None, "") else None
            if mn_usd is not None or mx_usd is not None:
                self.limits = (mn_usd, mx_usd, fmt(mn_usd / per) if mn_usd is not None else None,
                               fmt(float(mx)) if mx_usd is not None else None)
        except (FFError, KeyError, ValueError, ZeroDivisionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print("limits unavailable", self.send, self.recv, e)

    async def set_usd(self, usd):
        """Convert USD to a coin amount and quote it. Returns an error string, or None on success."""
        try:
            self.usd = usd
            if self.limits:
                if self.limits[0] and usd + 1e-9 < self.limits[0]:
                    raise FFError(f"below the minimum of {money(self.limits[0])}")
                if self.limits[1] and usd > self.limits[1] * 1.02:
                    raise FFError(f"above the maximum of {money(self.limits[1])}")
            self.amount = await usd_to_coin(self.send, usd)
            await self.refresh_quote()
        except (FFError, KeyError, ValueError, ZeroDivisionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.amount = self.est = None
            if "FixedFloat's" in str(e): await self.load_limits()  # the shown limits may be out of date
            return f"Can't swap ${usd:,.2f}: {e}"

    async def refresh_quote(self):
        q = await ff("price", fromCcy=self.send, toCcy=self.recv, amount=self.amount, direction="from", type=RATE_TYPE)
        if q.get("errors"): raise FFError(ff_errors(q["errors"]))
        self.est = q["to"]["amount"]

    # ----- address / memo / typed input
    async def to_confirm(self, i):
        """Ask for a memo if the coin needs one, otherwise re-price and show the confirmation."""
        if self.needs_memo() and not self.memo:
            return await self._go(i, "memo", deferred=True)
        err = await self.set_usd(self.usd)
        await self._go(i, "amount" if err else "confirm", err, deferred=True)

    async def use_saved(self, i):
        if not self.saved:
            return await i.response.send_message("No saved address for this coin.", ephemeral=True)
        self.address, self.memo = self.saved["address"], self.saved["memo"]
        await i.response.defer()
        await self.to_confirm(i)

    async def forget(self, i):
        db.execute("DELETE FROM addresses WHERE user_id=? AND code=?", (self.user.id, self.recv)); db.commit()
        await self._go(i, "address", "Saved address deleted.")

    async def on_text(self, m: discord.Message):
        txt = m.content.strip()
        if not txt or self.busy: return
        if self.step == "amount":
            usd = parse_usd(txt)
            if usd is None or usd <= 0: return
        elif not (10 <= len(txt) <= 200 and not any(c.isspace() for c in txt)):
            return  # chatter, not an address
        self.busy = True
        try:
            try: await m.delete()
            except discord.HTTPException: pass
            try:
                if self.step == "amount":
                    err = await self.set_usd(usd)
                    await self._go(None, "amount" if err else "address", err)
                else:
                    self.address, self.memo = txt, None
                    await self.to_confirm(None)
            except discord.HTTPException:
                waiting.pop(self.channel_id, None)
                await m.channel.send(f"{m.author.mention} this menu expired. Press **Open menu** above.")
        finally:
            self.busy = False

    async def on_timeout(self):
        if waiting.get(self.channel_id) is self: del waiting[self.channel_id]

    # ----- create the order
    async def confirm(self, i: discord.Interaction):
        if latest_order(i.channel_id):
            self.stop()
            return await i.response.edit_message(content="This ticket already has a swap.", embed=None, view=None)
        await i.response.edit_message(embed=brand(discord.Embed(title="⏳ Creating your order…", color=BLURPLE,
                                                               description="Hang tight, this only takes a few seconds.")),
                                      view=None)
        try:
            params = dict(fromCcy=self.send, toCcy=self.recv, toAddress=self.address, amount=self.amount,
                          direction="from", type=RATE_TYPE)
            if self.memo: params["tag"] = self.memo
            o = await ff("create", **params)
            ok = (o["from"]["code"] == self.send and o["to"]["code"] == self.recv
                  and o["to"]["address"].lower() == self.address.lower()
                  and o["from"]["address"]
                  and abs(float(o["from"]["amount"]) - float(self.amount)) <= 0.001 * float(self.amount) + 1e-9)
        except (FFError, KeyError, ValueError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            return await self._go(i, "address", f"Order failed (nothing to pay): {e}. Check the address and try again.",
                                  deferred=True)
        self.stop()
        if not ok:
            print("MISMATCH", o.get("id"))
            return await i.edit_original_response(view=None, embed=brand(discord.Embed(
                title="🚫 Order didn't match your request", color=RED,
                description="For safety I won't show a deposit address. **Do NOT send funds.** Press **Open menu** to try again.")))
        vouch = f"+vouch {usd_text(self.usd)} {swap_label(self.send)} to {swap_label(self.recv)}"
        db.execute("INSERT INTO orders(id, token, user_id, channel_id, status, created, usd, vouch_text) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (o["id"], o["token"], i.user.id, i.channel_id, o["status"], time.time(), self.usd, vouch))
        db.execute("INSERT OR REPLACE INTO addresses VALUES(?,?,?,?,?)",
                   (i.user.id, self.recv, self.address, self.memo, time.time())); db.commit()
        try:
            await i.channel.send(i.user.mention, embed=invoice_embed(o), view=EmergencyView())
            await i.edit_original_response(view=None, embed=brand(discord.Embed(
                title="✅ Invoice posted below", color=GREEN,
                description=f"Your {short(self.recv)} address was saved for next time. I'll post every update in this ticket.")))
        except discord.HTTPException:
            await i.edit_original_response(embed=invoice_embed(o), view=None)


def invoice_embed(o):
    f, t = o["from"], o["to"]
    left = int(o.get("time", {}).get("left") or 0)
    e = discord.Embed(title="🧾 Your invoice", color=GOLD, url=f'https://ff.io/order/{o["id"]}',
                      description=f'**{short(f["code"])}** ➜ **{short(t["code"])}**\n'
                                  "Send the **exact amount** to the **deposit address** below. "
                                  "Your swap starts automatically once the deposit arrives.")
    e.set_author(name=f'{BRAND}  ·  Order {o["id"]}')
    e.add_field(name="📤 Send exactly", value=f'```{f["amount"]} {f["code"]}```', inline=False)
    e.add_field(name="📮 Deposit address", value=f'```{f["address"]}```', inline=False)
    if f.get("tagValue"):
        e.add_field(name=f'🏷️ Deposit {f.get("tag") or "memo"} (required)', value=f'```{f["tagValue"]}```', inline=False)
    e.add_field(name="📥 You receive (est.)", value=f'**{t["amount"]}** {t["code"]}')
    if left: e.add_field(name="⏳ Expires", value=f"<t:{int(time.time()) + left}:R>")
    e.add_field(name="🏦 Payout address", value=f'`{t["address"]}`', inline=False)
    e.set_footer(text=f'⚠️ Send only {f["code"]} on the {f.get("network") or f["code"]} network. '
                      "A wrong amount, coin or network may delay or lose funds.")
    e.timestamp = discord.utils.utcnow()
    return brand(e)


# ---------- emergency ----------
async def run_emergency(i: discord.Interaction, **p):
    await i.response.defer(ephemeral=True)
    try:
        await ff("emergency", **p)
    except FFError as e:
        return await i.followup.send(f"Couldn't apply that: {e}", ephemeral=True)
    await i.followup.send("Done. I'll post the next status update in your ticket.", ephemeral=True)


class RefundModal(discord.ui.Modal):
    def __init__(self, oid, tok):
        super().__init__(title="Refund address"); self.oid, self.tok = oid, tok
        self.addr = discord.ui.TextInput(label="Address to refund to", max_length=200)
        self.tag = discord.ui.TextInput(label="Memo/tag (only if needed)", required=False, max_length=100)
        self.add_item(self.addr); self.add_item(self.tag)

    async def on_submit(self, i: discord.Interaction):
        p = dict(id=self.oid, token=self.tok, choice="REFUND", address=self.addr.value.strip())
        if self.tag.value: p["tag"] = self.tag.value.strip()
        await run_emergency(i, **p)


class ResolveView(discord.ui.View):
    def __init__(self, oid, tok):
        super().__init__(timeout=180); self.oid, self.tok = oid, tok

    @discord.ui.button(label="Continue the exchange", style=discord.ButtonStyle.green)
    async def cont(self, i: discord.Interaction, b):
        await run_emergency(i, id=self.oid, token=self.tok, choice="EXCHANGE")

    @discord.ui.button(label="Refund me", style=discord.ButtonStyle.red)
    async def refund(self, i: discord.Interaction, b):
        await i.response.send_modal(RefundModal(self.oid, self.tok))


async def close_ticket(ch):
    db.execute("DELETE FROM tickets WHERE channel_id=?", (ch.id,))
    db.execute("UPDATE orders SET channel_id=NULL WHERE channel_id=?", (ch.id,)); db.commit()
    await ch.delete(reason="order cancelled")


class CancelConfirm(discord.ui.View):
    def __init__(self, user_id, force=False):
        super().__init__(timeout=120)
        self.user_id, self.force = user_id, force

    async def interaction_check(self, i): return i.user.id == self.user_id

    @discord.ui.button(label="Yes, cancel & close", style=discord.ButtonStyle.danger)
    async def yes(self, i: discord.Interaction, b):
        row = latest_order(i.channel_id)
        if not row:
            return await i.response.edit_message(content="Nothing to cancel.", view=None)
        await i.response.defer()
        if not self.force:
            try:
                o = await ff("order", id=row[0], token=row[1])  # re-check: a deposit may have arrived meanwhile
            except FFError as e:
                return await i.edit_original_response(content=f"Couldn't check the order: {e}", view=None)
            if o["status"] not in ("NEW",) + FINAL:
                return await i.edit_original_response(
                    content="A deposit was just detected, so this order can't be cancelled now. Keep the ticket open.", view=None)
        self.stop()
        await i.edit_original_response(content="Order cancelled. Closing this ticket...", view=None)
        try: await close_ticket(i.channel)
        except discord.HTTPException as e: print("close failed", e)

    @discord.ui.button(label="Keep order", style=discord.ButtonStyle.secondary)
    async def no(self, i: discord.Interaction, b):
        self.stop()
        await i.response.edit_message(content="Okay, your order is still open.", view=None)


class EmergencyView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="📋 Copy details", style=discord.ButtonStyle.primary, custom_id="ff:copy")
    async def copy(self, i: discord.Interaction, b):
        row = latest_order(i.channel_id)
        if not row or row[2] != i.user.id:
            return await i.response.send_message("Only the ticket owner can use this.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            o = await ff("order", id=row[0], token=row[1])
        except FFError as e:
            return await i.followup.send(f"Couldn't load the details: {e}", ephemeral=True)
        f = o.get("from") or {}
        if o["status"] != "NEW" or not f.get("address"):
            return await i.followup.send(f'This order is: {STATUS.get(o["status"], o["status"])}. '
                                         "Don't send a deposit for it.", ephemeral=True)
        # plain text, one message each: address first, then amount (then the deposit memo only if the coin needs it)
        for text in (f["address"], f["amount"], f.get("tagValue")):
            if text in (None, ""): continue
            text = str(text)
            if any(c in text for c in "_*~|`"): text = f"`{text}`"  # keep markdown characters from changing the text
            await i.followup.send(text, ephemeral=True)

    @discord.ui.button(label="✖ Cancel order", style=discord.ButtonStyle.secondary, custom_id="ff:cancel")
    async def cancel_order(self, i: discord.Interaction, b):
        row = latest_order(i.channel_id)
        if not row or row[2] != i.user.id:
            return await i.response.send_message("Only the ticket owner can use this.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            o = await ff("order", id=row[0], token=row[1])
        except FFError as e:
            return await i.followup.send(f"Couldn't check the order: {e}", ephemeral=True)
        st = o["status"]
        if st not in ("NEW",) + FINAL:
            return await i.followup.send("Your deposit has already been detected, so this order can't be cancelled. "
                                         "If something looks wrong, use the red button.", ephemeral=True)
        text = ("Cancel this order and close the ticket?\n**Don't send a deposit to its address afterwards.** "
                "If you've already sent one, press Keep order." if st == "NEW" else "This order is finished. Close the ticket?")
        await i.followup.send(text, view=CancelConfirm(i.user.id), ephemeral=True)

    @discord.ui.button(label="Something went wrong?", style=discord.ButtonStyle.danger, custom_id="ff:emergency")
    async def em(self, i: discord.Interaction, b):
        row = latest_order(i.channel_id)
        if not row or row[2] != i.user.id:
            return await i.response.send_message("Only the ticket owner can use this.", ephemeral=True)
        await i.response.defer(ephemeral=True)
        try:
            o = await ff("order", id=row[0], token=row[1])
        except FFError as e:
            return await i.followup.send(f"Couldn't check the order: {e}", ephemeral=True)
        if o["status"] != "EMERGENCY":
            return await i.followup.send(f'Order status: **{STATUS.get(o["status"], o["status"])}**. Nothing to '
                                         f'resolve yet. Details: https://ff.io/order/{row[0]}', ephemeral=True)
        await i.followup.send("This order needs a decision. You can continue the exchange at the current "
                              "market rate, or get a refund:", view=ResolveView(row[0], row[1]), ephemeral=True)


# ---------- status polling ----------
FLOW = ["Deposit", "Confirming", "Exchange", "Payout"]
IDX = {"NEW": 0, "PENDING": 1, "EXCHANGE": 2, "WITHDRAW": 3, "DONE": 4}
ICON = {"NEW": "⏳", "PENDING": "🔄", "EXCHANGE": "🔁", "WITHDRAW": "📤", "DONE": "✅", "EXPIRED": "⌛", "EMERGENCY": "🚨"}


def tracker(st):
    n = IDX.get(st, 0)
    return "  ›  ".join(("✅" if k < n else "🔵" if k == n else "⚪") + f" {name}" for k, name in enumerate(FLOW))


def status_embed(o):
    st = o["status"]
    f, t = o.get("from") or {}, o.get("to") or {}
    e = discord.Embed(title=f'{ICON.get(st, "ℹ️")} {STATUS.get(st, st)}', url=f'https://ff.io/order/{o["id"]}',
                      color=GREEN if st == "DONE" else RED if st in ("EXPIRED", "EMERGENCY") else BLURPLE)
    e.set_author(name=f'{BRAND}  ·  Order {o["id"]}  ·  {short(f.get("code", "?"))} ➜ {short(t.get("code", "?"))}')
    e.description = ("No deposit arrived in time, so this order was cancelled. Nothing was charged." if st == "EXPIRED" else
                     "This order needs your decision. Press the red **Something went wrong?** button." if st == "EMERGENCY"
                     else tracker(st))
    tx = o.get("tx") or {}
    tin, tout = (tx.get("in") or {}), (tx.get("out") or {})
    if tin.get("id"): e.add_field(name="📥 Deposit TxID", value=f'```{tin["id"]}```', inline=False)
    if tout.get("id"):
        e.add_field(name="📤 Payout TxID", value=f'```{tout["id"]}```', inline=False)
        e.add_field(name="💰 You received", value=f'**{tout.get("amount") or t.get("amount")}** {t.get("code", "")}')
    if st in FINAL: e.set_footer(text=f"This ticket closes in {CLOSE_DELAY // 60} minutes.")
    e.timestamp = discord.utils.utcnow()
    return brand(e)


async def notify(uid, ch_id, o):
    st = o["status"]
    e = status_embed(o)
    try:
        ch = bot.get_channel(ch_id) if ch_id else None
        if ch: await ch.send(f"<@{uid}>", embed=e, view=None if st in FINAL else EmergencyView())
        else: await (await bot.fetch_user(uid)).send(embed=e)  # ticket gone: fall back to DM
    except discord.HTTPException as ex:
        print("notify failed", uid, o["id"], ex)
    if st == "DONE": await send_thanks(uid, ch_id, o)


@tasks.loop(seconds=20)
async def poll():
    for (cid,) in db.execute("SELECT DISTINCT channel_id FROM orders WHERE close_at IS NOT NULL AND "
                             "close_at<? AND channel_id IS NOT NULL", (time.time(),)).fetchall():
        ch = bot.get_channel(cid)
        if ch:
            try: await ch.delete(reason="swap finished")
            except discord.HTTPException as e: print("close failed", cid, e)
        db.execute("UPDATE orders SET channel_id=NULL WHERE channel_id=?", (cid,))
        db.execute("DELETE FROM tickets WHERE channel_id=?", (cid,)); db.commit()
    rows = db.execute("SELECT id, token, user_id, channel_id, status FROM orders WHERE status NOT IN "
                      "('DONE','EXPIRED') AND created>?", (time.time() - MAX_AGE,)).fetchall()
    for oid, tok, uid, cid, last in rows:
        try:
            o = await ff("order", id=oid, token=tok)
        except Exception as e:
            print("poll error", oid, e); continue
        if o["status"] != last:
            close_at = time.time() + CLOSE_DELAY if o["status"] in FINAL else None
            db.execute("UPDATE orders SET status=?, close_at=? WHERE id=?", (o["status"], close_at, oid)); db.commit()
            await notify(uid, cid, o)
            if o["status"] == "DONE":
                await log_swap(uid, o)     # swap details -> log channel
                await update_panels()      # bump the Record line on the panel
        await asyncio.sleep(0.3)


@poll.before_loop
async def _wait():
    await bot.wait_until_ready()
    await update_panels()  # sync the Record line on startup


@tasks.loop(seconds=5)
async def vouch_poll():
    if not VOUCH_REQUIRED: return
    try: await scan_vouches()
    except Exception as e: print("vouch poll error", e)


@vouch_poll.before_loop
async def _wait_vouch(): await bot.wait_until_ready()


@tasks.loop(minutes=10)
async def refresh():
    global ccies
    try: ccies = await ff("ccies")
    except Exception as e: print("ccies refresh failed", e)


@bot.event
async def on_message(m: discord.Message):
    if m.author.bot or not m.guild: return
    if VOUCH_REQUIRED and m.channel.id in (VOUCH_CHANNEL, LOG_CHANNEL) and is_vouch(m.content):
        if await mark_vouched(m.author.id, m.created_at.timestamp()):
            try: await m.add_reaction("✅")
            except discord.HTTPException: pass
        return
    w = waiting.get(m.channel.id)
    if not w: return
    if w.is_finished(): waiting.pop(m.channel.id, None); return
    if w.user.id == m.author.id and w.step in ("amount", "address"):
        await w.on_text(m)


@bot.event
async def setup_hook():
    global session
    session = aiohttp.ClientSession()
    await refresh()
    if not ccies: raise SystemExit("Could not load currencies: check FF_API_KEY / FF_API_SECRET")
    for v in (PanelView(), TicketView(), EmergencyView(), VouchView()): bot.add_view(v)
    refresh.start(); poll.start(); vouch_poll.start()
    await bot.tree.sync()


bot.run(os.environ["DISCORD_TOKEN"])
