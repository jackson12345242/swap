"""Ticket-based, non-custodial Discord swap bot for FixedFloat (ff.io).
Admin runs /setup once in the main channel -> panel with "Start a swap" button -> private ticket
channel -> menu (pick coins, enter details) -> confirm -> invoice -> live status + txids -> emergency button.
Env: DISCORD_TOKEN, FF_API_KEY, FF_API_SECRET, DB_PATH (on your Railway volume), FF_RATE_TYPE (default float)
"""
import os, re, hmac, hashlib, json, sqlite3, time, asyncio
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
CHAT_INPUT = os.getenv("CHAT_INPUT", "1") == "1"  # type the USD amount in chat (needs Message Content Intent)
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
""")
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


# ---------- panel + tickets ----------
class PanelView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="Start a swap", style=discord.ButtonStyle.green, custom_id="ff:open")
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
            e = discord.Embed(title="🎫 Your swap ticket", color=BLURPLE, description=(
                "Welcome! Here's how this works:\n\n"
                "**1️⃣** Press **Start swap** below\n"
                "**2️⃣** Pick the coin you send and the coin you receive\n"
                "**3️⃣** Type the amount in USD, then your receiving address\n"
                "**4️⃣** Confirm, send the deposit and watch the status update live here"))
            e.add_field(name="📜 Terms", value=TOS_TEXT, inline=False)
            e.set_footer(text="Private ticket · only you and staff can see this channel")
            await ch.send(i.user.mention, embed=e, view=TicketView())
            await i.followup.send(f"Your ticket: {ch.mention}", ephemeral=True)
        finally:
            opening.discard(uid)


class TicketView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="Start swap", style=discord.ButtonStyle.green, custom_id="ff:start")
    async def start(self, i: discord.Interaction, b):
        t = db.execute("SELECT user_id FROM tickets WHERE channel_id=?", (i.channel_id,)).fetchone()
        if not t or t[0] != i.user.id:
            return await i.response.send_message("Only the ticket owner can start a swap.", ephemeral=True)
        if latest_order(i.channel_id):
            return await i.response.send_message("This ticket already has a swap. Open a new ticket once it finishes.", ephemeral=True)
        if not ccies:
            return await i.response.send_message("Coin list is still loading, try again in a moment.", ephemeral=True)
        w = Wizard(i.user, i.channel_id)
        w.last_i = i
        await i.response.send_message(embed=w.embed(), view=w)

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.grey, custom_id="ff:close")
    async def close(self, i: discord.Interaction, b):
        t = db.execute("SELECT user_id FROM tickets WHERE channel_id=?", (i.channel_id,)).fetchone()
        if not t or (i.user.id != t[0] and not i.permissions.manage_channels):
            return await i.response.send_message("Only the ticket owner can close this.", ephemeral=True)
        o = latest_order(i.channel_id)
        if o and o[3] not in FINAL:
            return await i.response.send_message("A swap is still in progress. Wait until it finishes "
                                                 "(or use the red button if it's stuck).", ephemeral=True)
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
        e = discord.Embed(title="💱 Crypto Swaps", color=BLURPLE, description=(
            "Swap between hundreds of coins at the best available rate. Fast, private and non-custodial."))
        e.add_field(name="How it works", value=("1️⃣ Press **Start a swap**\n2️⃣ Pick your coins and type an amount in USD\n"
                                                "3️⃣ Send the deposit and track it live"), inline=False)
        e.add_field(name="Why use it", value="⚡ Hundreds of coins\n🔒 We never hold your funds\n🎫 A private ticket for every swap",
                    inline=False)
        e.add_field(name="📜 Terms", value=TOS_TEXT, inline=False)
        e.set_footer(text="Powered by FixedFloat")
        await inter.channel.send(embed=e, view=PanelView())
    except discord.HTTPException as ex:
        return await inter.followup.send(f"Setup failed (check my permissions: Manage Channels, "
                                         f"Send Messages, Embed Links): {ex}", ephemeral=True)
    db.execute("INSERT OR REPLACE INTO config VALUES(?,?,?)",
               (g.id, category.id, staff_role.id if staff_role else None)); db.commit()
    await inter.followup.send("Panel posted.", ephemeral=True)


# ---------- swap ----------
POPULAR = ["BTC", "ETH", "LTC", "XMR", "SOL", "USDTTRC", "USDTETH", "USDTBSC", "USDTSOL", "USDCETH",
           "BNB", "XRP", "DOGE", "TRX", "TON", "ADA", "BCH", "DASH", "ZEC", "AVAX", "DOT", "XLM", "ETC",
           "BTCLN", "MATIC"]


def ordered(field):
    ok = [c for c in ccies if c.get(field)]
    by = {c["code"]: c for c in ok}
    return [by[k] for k in POPULAR if k in by] + [c for c in ok if c["code"] not in POPULAR]


def search(field, q):
    q = q.lower().strip()
    return [c for c in ordered(field) if q in c["code"].lower() or q in c["name"].lower()
            or q in c["network"].lower()]


def btn_label(c):
    coin = c.get("coin") or c["code"]
    return (coin if c["network"] in (coin, c["code"]) else f'{coin} ({c["network"]})')[:80]


def coin_label(code):
    c = next((c for c in ccies if c["code"] == code), None)
    return f'{c["name"]} ({c["network"]})' if c else (code or "-")


class SearchModal(discord.ui.Modal):
    def __init__(self, w):
        super().__init__(title="Search coins")
        self.w = w
        self.q = discord.ui.TextInput(label="Coin name, ticker or network (empty = all)", required=False, max_length=40)
        self.add_item(self.q)

    async def on_submit(self, i: discord.Interaction):
        w, text = self.w, self.q.value.strip()
        field = "send" if w.step == "send" else "recv"
        if text and not search(field, text):
            return await i.response.send_message("No coins found. Try another name.", ephemeral=True)
        w.query, w.page = text, 0
        await w._go(i, w.step)


def money(x): return f"${x:,.2f}"


def fmt(x): return f"{x:.8f}".rstrip("0").rstrip(".")


def parse_usd(text):
    t = text.strip()
    m = re.fullmatch(r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)\s*(?:usd|dollars?|\$)?", t, re.I)
    if not m: return None
    n = m.group(1)
    n = n.replace(",", "") if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", n) else n.replace(",", ".")
    return float(n)


async def usd_to_coin(code, usd):
    """How much of `code` is worth `usd` dollars: mid-point of FixedFloat quotes against USDT."""
    if code.startswith(("USDT", "USDC", "DAI", "TUSD", "FDUSD", "PYUSD")): return fmt(usd)
    ref = next((k for k in ("USDTTRC", "USDTETH", "USDTBSC", "USDTSOL")
                if any(c["code"] == k and c.get("send") and c.get("recv") for c in ccies)), None)
    if not ref: raise FFError("USD pricing is unavailable right now")
    hi = await ff("price", fromCcy=code, toCcy=ref, amount=fmt(usd), direction="to", type="float")
    lo = await ff("price", fromCcy=ref, toCcy=code, amount=fmt(usd), direction="from", type="float")
    for q in (hi, lo):
        if q.get("errors"): raise FFError(str(q["errors"]))
    coin = (float(hi["from"]["amount"]) + float(lo["to"]["amount"])) / 2
    if coin <= 0: raise FFError("couldn't price that amount")
    return fmt(coin)


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
    """One message in the ticket that walks through: send coin -> receive coin -> amount -> address -> confirm."""
    TITLES = {"send": "Choose the coin you send", "recv": "Choose the coin you receive",
              "amount": "How much do you want to swap?", "address": "Where should we send it?",
              "memo": "One more detail: memo / tag", "confirm": "Review your swap"}
    STEPNO = {"send": 1, "recv": 2, "amount": 3, "address": 4, "memo": 4, "confirm": 4}
    PREV = {"recv": "send", "amount": "recv", "address": "amount", "memo": "address", "confirm": "address"}

    def __init__(self, user, channel_id=None):
        super().__init__(timeout=900)
        self.user, self.channel_id, self.last_i, self.busy, self.usd = user, channel_id, None, False, None
        self.step, self.page, self.pages, self.query, self.notice = "send", 0, 1, "", None
        self.send = self.recv = self.amount = self.address = self.memo = self.est = self.limits = None
        self.render()

    async def interaction_check(self, i):
        if i.user.id == self.user.id: return True
        await i.response.send_message("Only the ticket owner can use this menu.", ephemeral=True)
        return False

    def dst(self): return next((c for c in ccies if c["code"] == self.recv), None)

    def _btn(self, label, cb, style=discord.ButtonStyle.secondary, row=None, disabled=False):
        b = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        b.callback = cb
        self.add_item(b)

    def render(self):
        self.clear_items()
        self.saved = get_saved(self.user.id, self.recv) if self.step == "address" and self.recv else None
        if self.step in ("send", "recv"):
            field = "send" if self.step == "send" else "recv"
            pool = search(field, self.query) if self.query else ordered(field)
            if self.step == "recv": pool = [c for c in pool if c["code"] != self.send]
            self.pages = max(1, -(-len(pool) // 20))
            self.page = max(0, min(self.page, self.pages - 1))
            for n, c in enumerate(pool[self.page * 20:(self.page + 1) * 20]):
                self._btn(btn_label(c), self._pick(c["code"]), row=n // 5)
            self._btn("◀", self.prev, row=4, disabled=self.page == 0)
            self._btn("▶", self.next, row=4, disabled=self.page >= self.pages - 1)
            self._btn("🔍 Search", self.open_search, row=4)
            if self.step == "recv": self._btn("Back", self.back, row=4)
            self._btn("Cancel", self.cancel, discord.ButtonStyle.danger, row=4)
            return
        if self.step == "address" and self.saved:
            self._btn("✅ Use saved address", self.use_saved, discord.ButtonStyle.success)
            self._btn("Enter new address", self.ask_address)
            self._btn("Back", self.back)
            self._btn("Cancel", self.cancel, discord.ButtonStyle.danger)
            self._btn("🗑 Forget saved", self.forget)
            return
        main = {"amount": ("Enter amount", self.ask_amount), "address": ("Enter address", self.ask_address),
                "memo": ("Enter memo", self.ask_memo), "confirm": ("Confirm swap", self.confirm)}[self.step]
        self._btn(main[0], main[1], discord.ButtonStyle.success)
        self._btn("Back", self.back)
        self._btn("Cancel", self.cancel, discord.ButtonStyle.danger)

    def embed(self):
        n = self.STEPNO[self.step]
        e = discord.Embed(title=self.TITLES[self.step], color=GREEN if self.step == "confirm" else BLURPLE)
        e.set_author(name=f"Crypto Swaps  ·  Step {n} of 4   {'▰' * n}{'▱' * (4 - n)}")
        e.set_footer(text="Cancel starts over")
        lines = []
        if self.notice: lines.append(f"⚠️ **{self.notice}**")
        if self.step in ("send", "recv"):
            lines.append("Tap a coin below, or use **🔍 Search** to find any other coin." if not self.query
                         else f'Results for **"{self.query}"**. Search with an empty box to see all coins.')
            e.set_footer(text=f"Page {self.page + 1} of {self.pages}  ·  Cancel starts over")
        elif self.step == "amount":
            lines.append(("💬 **Type the amount in USD in this chat** (for example `100`), or press **Enter amount**."
                          if CHAT_INPUT else "Press **Enter amount** and type how much you want to swap in USD.")
                         + f"\nI'll work out how much {self.send} that is.")
        elif self.step == "address":
            what = f"your **{coin_label(self.recv)}** address"
            lines.append((f"💬 **Type {what} in this chat**, or press **Enter " + ("new " if self.saved else "") + "address**."
                          if CHAT_INPUT else f"Press **Enter address** and paste {what}.")
                         + "\nDouble-check it: swaps can't be reversed.")
        elif self.step == "memo":
            tag = (self.dst() or {}).get("tag") or "memo"
            lines.append(f"{coin_label(self.recv)} needs a **{tag}**. Press **Enter memo** and paste it exactly: "
                         "a wrong or missing one can lose funds.")
        else:
            lines.append("Check everything below, then press **Confirm swap**.")
            e.set_footer(text=f"Rate: {RATE_TYPE} (the final amount can differ slightly)  ·  By confirming you accept the terms")
        e.description = "\n\n".join(lines) or None
        if self.send:
            val = f"**{self.amount}** {self.send}" if self.amount else coin_label(self.send)
            if self.amount and self.usd: val += f"\n≈ {money(self.usd)}"
            e.add_field(name="📤 You send", value=val)
        if self.recv:
            e.add_field(name="📥 You receive", value=f"≈ **{self.est}** {self.recv}" if self.est else coin_label(self.recv))
        if self.step == "amount" and self.limits:
            mn, mx, mnc, mxc = self.limits
            parts = []
            if mn is not None: parts.append(f"▫️ **Minimum** {money(mn)} ({mnc} {self.send})")
            if mx is not None: parts.append(f"▫️ **Maximum** {money(mx)} ({mxc} {self.send})")
            if parts: e.add_field(name="📏 Limits for this swap", value="\n".join(parts), inline=False)
        if self.step == "address" and self.saved:
            e.add_field(name="💾 Saved address", value=f"```{self.saved['address']}```"
                        + (f"\nMemo: `{self.saved['memo']}`" if self.saved["memo"] else ""), inline=False)
        if self.step == "confirm":
            e.add_field(name="📮 Payout address", value=f"```{self.address}```", inline=False)
            if self.memo: e.add_field(name="🏷️ Memo / tag", value=f"```{self.memo}```", inline=False)
        return e

    async def _go(self, i, step, notice=None, deferred=False):
        self.step, self.notice, self.last_i = step, notice, i
        if step in ("amount", "address"): waiting[self.channel_id] = self
        elif waiting.get(self.channel_id) is self: del waiting[self.channel_id]
        self.render()
        if deferred: await i.edit_original_response(embed=self.embed(), view=self)
        else: await i.response.edit_message(embed=self.embed(), view=self)

    async def load_limits(self):
        """Probe a ~$100 quote for this pair and read FixedFloat's min/max (converted to USD)."""
        self.limits = None
        try:
            probe = float(await usd_to_coin(self.send, 100))
            q = await ff("price", fromCcy=self.send, toCcy=self.recv, amount=fmt(probe),
                         direction="from", type=RATE_TYPE)
            f, per = q["from"], 100 / probe  # per = USD value of one coin
            mn, mx = f.get("min"), f.get("max")
            mn_usd = float(mn) * per if mn not in (None, "") else None
            mx_usd = float(mx) * per if mx not in (None, "") else None
            if mn_usd is not None or mx_usd is not None:
                self.limits = (mn_usd, mx_usd, fmt(float(mn)) if mn_usd is not None else None,
                               fmt(float(mx)) if mx_usd is not None else None)
        except (FFError, KeyError, ValueError, ZeroDivisionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            print("limits unavailable", self.send, self.recv, e)

    async def set_usd(self, usd):
        """Convert USD to a coin amount and quote it. Returns an error string, or None on success."""
        try:
            self.usd = usd
            if self.limits:
                if self.limits[0] and usd < self.limits[0] * 0.98:
                    raise FFError(f"below the minimum of {money(self.limits[0])}")
                if self.limits[1] and usd > self.limits[1] * 1.02:
                    raise FFError(f"above the maximum of {money(self.limits[1])}")
            self.amount = await usd_to_coin(self.send, usd)
            await self.refresh_quote()
        except (FFError, KeyError, ValueError, ZeroDivisionError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.amount = self.est = None
            return f"Can't price ${usd:,.2f}: {e}"

    def needs_memo(self): return bool((self.dst() or {}).get("tag"))

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
                    await self._go(self.last_i, "amount" if err else "address", err, deferred=True)
                else:
                    self.address, self.memo = txt, None
                    await self.to_confirm(self.last_i)
            except discord.HTTPException:
                waiting.pop(self.channel_id, None)
                await m.channel.send(f"{m.author.mention} this menu expired. Press **Start swap** again.")
        finally:
            self.busy = False

    async def on_timeout(self):
        if waiting.get(self.channel_id) is self: del waiting[self.channel_id]

    async def refresh_quote(self):
        q = await ff("price", fromCcy=self.send, toCcy=self.recv, amount=self.amount, direction="from", type=RATE_TYPE)
        if q.get("errors"): raise FFError(str(q["errors"]))
        self.est = q["to"]["amount"]

    def _pick(self, code):
        async def cb(i):
            self.query, self.page = "", 0
            if self.step == "send": self.send = code; await self._go(i, "recv")
            else:
                self.recv = code
                await i.response.defer()
                await self.load_limits()
                await self._go(i, "amount", deferred=True)
        return cb

    async def prev(self, i): self.page -= 1; await self._go(i, self.step)
    async def next(self, i): self.page += 1; await self._go(i, self.step)
    async def open_search(self, i): await i.response.send_modal(SearchModal(self))
    async def ask_amount(self, i): await i.response.send_modal(AmountModal(self))
    async def ask_address(self, i): await i.response.send_modal(AddressModal(self))
    async def ask_memo(self, i): await i.response.send_modal(MemoModal(self))

    async def back(self, i):
        self.query, self.page = "", 0
        await self._go(i, self.PREV[self.step])

    async def cancel(self, i):
        self.send = self.recv = self.amount = self.address = self.memo = self.est = self.usd = self.limits = None
        self.query, self.page = "", 0
        await self._go(i, "send")

    async def confirm(self, i: discord.Interaction):
        if latest_order(i.channel_id):
            self.stop()
            return await i.response.edit_message(content="This ticket already has a swap.", embed=None, view=None)
        await i.response.edit_message(embed=discord.Embed(title="⏳ Creating your order…", color=BLURPLE,
                                                         description="Hang tight, this only takes a few seconds."), view=None)
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
            return await i.edit_original_response(view=None, embed=discord.Embed(
                title="🚫 Order didn't match your request", color=RED,
                description="For safety I won't show a deposit address. **Do NOT send funds.** Press **Start swap** to try again."))
        db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,NULL)",
                   (o["id"], o["token"], i.user.id, i.channel_id, o["status"], time.time()))
        db.execute("INSERT OR REPLACE INTO addresses VALUES(?,?,?,?,?)",
                   (i.user.id, self.recv, self.address, self.memo, time.time())); db.commit()
        try:
            await i.channel.send(i.user.mention, embed=invoice_embed(o), view=EmergencyView())
            await i.edit_original_response(view=None, embed=discord.Embed(
                title="✅ Invoice posted below", color=GREEN,
                description=f"Your {self.recv} address was saved for next time. I'll post every update in this ticket."))
        except discord.HTTPException:
            await i.edit_original_response(embed=invoice_embed(o), view=None)


def invoice_embed(o):
    f, t = o["from"], o["to"]
    left = int(o.get("time", {}).get("left") or 0)
    e = discord.Embed(title=f'🧾 Invoice · {f["code"]} ➜ {t["code"]}', color=GOLD, url=f'https://ff.io/order/{o["id"]}',
                      description="Send the **exact amount** below to the **deposit address**. "
                                  "Your swap starts automatically once the deposit arrives.")
    e.set_author(name=f'Crypto Swaps  ·  Order {o["id"]}')
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
    return e


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
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id

    async def interaction_check(self, i): return i.user.id == self.user_id

    @discord.ui.button(label="Yes, cancel & close", style=discord.ButtonStyle.danger)
    async def yes(self, i: discord.Interaction, b):
        row = latest_order(i.channel_id)
        if not row:
            return await i.response.edit_message(content="Nothing to cancel.", view=None)
        await i.response.defer()
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
    e.set_author(name=f'Crypto Swaps  ·  Order {o["id"]}  ·  {f.get("code", "?")} ➜ {t.get("code", "?")}')
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
    return e


async def notify(uid, ch_id, o):
    st = o["status"]
    e = status_embed(o)
    try:
        ch = bot.get_channel(ch_id) if ch_id else None
        if ch: await ch.send(f"<@{uid}>", embed=e, view=None if st in FINAL else EmergencyView())
        else: await (await bot.fetch_user(uid)).send(embed=e)  # ticket gone: fall back to DM
    except discord.HTTPException as ex:
        print("notify failed", uid, o["id"], ex)


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
        await asyncio.sleep(0.3)


@poll.before_loop
async def _wait(): await bot.wait_until_ready()


@tasks.loop(minutes=10)
async def refresh():
    global ccies
    try: ccies = await ff("ccies")
    except Exception as e: print("ccies refresh failed", e)


@bot.event
async def on_message(m: discord.Message):
    if m.author.bot or not m.guild: return
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
    for v in (PanelView(), TicketView(), EmergencyView()): bot.add_view(v)
    refresh.start(); poll.start()
    await bot.tree.sync()


bot.run(os.environ["DISCORD_TOKEN"])
