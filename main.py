"""Ticket-based, non-custodial Discord swap bot for FixedFloat (ff.io).
Admin runs /setup once in the main channel -> panel with "Start a swap" button -> private ticket
channel -> menu (pick coins, enter details) -> confirm -> invoice -> live status + txids -> emergency button.
Env: DISCORD_TOKEN, FF_API_KEY, FF_API_SECRET, DB_PATH (on your Railway volume), FF_RATE_TYPE (default float)
"""
import os, hmac, hashlib, json, sqlite3, time, asyncio
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
""")
db.commit()

class FFError(Exception): pass

bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
session: aiohttp.ClientSession
ccies: list = []
opening: set = set()


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
                                                    embed_links=True, manage_channels=True),
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
            e = discord.Embed(title="Your swap ticket", color=0x5865F2, description=(
                "Press **Start swap** below, pick the coin you send and the coin you receive from the menus, "
                "then enter the amount and your receiving address.\n\n"
                "I'll post your invoice and every status update, with transaction IDs, in this channel.\n\n"
                + TOS_TEXT))
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
        w = Wizard(i.user)
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
        e = discord.Embed(title="Crypto Swaps", color=0x57F287, description=(
            "Swap between hundreds of coins at the best available rate. Press the button to open a "
            "private ticket.\n\n" + TOS_TEXT))
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


class AmountModal(discord.ui.Modal):
    def __init__(self, w):
        super().__init__(title="How much?")
        self.w = w
        self.amount = discord.ui.TextInput(label=f"Amount of {w.send} to send"[:45], max_length=24)
        self.add_item(self.amount)

    async def on_submit(self, i: discord.Interaction):
        w = self.w
        try:
            val = float(self.amount.value.replace(",", ".").strip())
        except ValueError:
            val = 0
        amt = f"{val:.8f}".rstrip("0").rstrip(".")
        if val <= 0 or not amt:
            return await i.response.send_message("That isn't a valid amount. Press Enter amount and try again.", ephemeral=True)
        w.amount = amt
        await i.response.defer()
        try:
            await w.refresh_quote()
        except (FFError, KeyError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            return await w._go(i, "amount", f"Can't quote that amount: {e}", deferred=True)
        await w._go(i, "address", deferred=True)


class AddressModal(discord.ui.Modal):
    def __init__(self, w):
        super().__init__(title="Receiving address")
        self.w = w
        self.address = discord.ui.TextInput(label=f"Your {w.recv} address"[:45], max_length=200)
        self.add_item(self.address)
        self.memo = None
        tag = (w.dst() or {}).get("tag")
        if tag:
            self.memo = discord.ui.TextInput(label=f"{tag} (required)"[:45], max_length=100)
            self.add_item(self.memo)

    async def on_submit(self, i: discord.Interaction):
        w = self.w
        w.address = self.address.value.strip()
        w.memo = self.memo.value.strip() if self.memo else None
        await i.response.defer()
        try:
            await w.refresh_quote()  # re-quote so the confirmation shows a fresh estimate
        except (FFError, KeyError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            return await w._go(i, "amount", f"Can't quote that swap: {e}", deferred=True)
        await w._go(i, "confirm", deferred=True)


class Wizard(discord.ui.View):
    """One message in the ticket that walks through: send coin -> receive coin -> amount -> address -> confirm."""
    TITLES = {"send": "Step 1/4 · Choose the coin you SEND", "recv": "Step 2/4 · Choose the coin you RECEIVE",
              "amount": "Step 3/4 · How much do you want to send?", "address": "Step 4/4 · Where should we send it?",
              "confirm": "Confirm your swap"}
    PREV = {"recv": "send", "amount": "recv", "address": "amount", "confirm": "address"}

    def __init__(self, user):
        super().__init__(timeout=900)
        self.user = user
        self.step, self.page, self.pages, self.query, self.notice = "send", 0, 1, "", None
        self.send = self.recv = self.amount = self.address = self.memo = self.est = None
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
        main = {"amount": ("Enter amount", self.ask_amount), "address": ("Enter address", self.ask_address),
                "confirm": ("Confirm swap", self.confirm)}[self.step]
        self._btn(main[0], main[1], discord.ButtonStyle.success)
        self._btn("Back", self.back)
        self._btn("Cancel", self.cancel, discord.ButtonStyle.danger)

    def embed(self):
        e = discord.Embed(title=self.TITLES[self.step], color=0x5865F2)
        lines = []
        if self.notice: lines.append(f"⚠️ {self.notice}")
        if self.step in ("send", "recv"):
            lines.append("Press a coin below, or use Search for others." if not self.query
                         else f'Showing results for "{self.query}". Search with an empty box to see all coins.')
            e.set_footer(text=f"Page {self.page + 1}/{self.pages}")
        elif self.step == "amount":
            lines.append(f"Press **Enter amount** and type how much {self.send} you want to send.")
        elif self.step == "address":
            lines.append(f"Press **Enter address** and paste the **{coin_label(self.recv)}** address that should "
                         "receive your coins. Double-check it: swaps can't be reversed.")
        else:
            e.set_footer(text=f"Rate: {RATE_TYPE}. The final amount can differ slightly on float rates. "
                              "By confirming you accept the terms.")
        e.description = "\n".join(lines) or None
        if self.send: e.add_field(name="You send", value=coin_label(self.send) if not self.amount
                                  else f"{self.amount} {self.send}")
        if self.recv: e.add_field(name="You receive", value=coin_label(self.recv) if not self.est
                                  else f"≈ {self.est} {self.recv}")
        if self.address and self.step in ("confirm",):
            e.add_field(name="To address", value=f"```{self.address}```", inline=False)
            if self.memo: e.add_field(name="Memo / tag", value=f"```{self.memo}```", inline=False)
        return e

    async def _go(self, i, step, notice=None, deferred=False):
        self.step, self.notice = step, notice
        self.render()
        if deferred: await i.edit_original_response(embed=self.embed(), view=self)
        else: await i.response.edit_message(embed=self.embed(), view=self)

    async def refresh_quote(self):
        q = await ff("price", fromCcy=self.send, toCcy=self.recv, amount=self.amount, direction="from", type=RATE_TYPE)
        if q.get("errors"): raise FFError(str(q["errors"]))
        self.est = q["to"]["amount"]

    def _pick(self, code):
        async def cb(i):
            self.query, self.page = "", 0
            if self.step == "send": self.send = code; await self._go(i, "recv")
            else: self.recv = code; await self._go(i, "amount")
        return cb

    async def prev(self, i): self.page -= 1; await self._go(i, self.step)
    async def next(self, i): self.page += 1; await self._go(i, self.step)
    async def open_search(self, i): await i.response.send_modal(SearchModal(self))
    async def ask_amount(self, i): await i.response.send_modal(AmountModal(self))
    async def ask_address(self, i): await i.response.send_modal(AddressModal(self))

    async def back(self, i):
        self.query, self.page = "", 0
        await self._go(i, self.PREV[self.step])

    async def cancel(self, i):
        self.stop()
        await i.response.edit_message(content="Cancelled.", embed=None, view=None)

    async def confirm(self, i: discord.Interaction):
        if latest_order(i.channel_id):
            self.stop()
            return await i.response.edit_message(content="This ticket already has a swap.", embed=None, view=None)
        await i.response.edit_message(embed=discord.Embed(title="Creating your order...", color=0x5865F2), view=None)
        try:
            params = dict(fromCcy=self.send, toCcy=self.recv, toAddress=self.address, amount=self.amount,
                          direction="from", type=RATE_TYPE)
            if self.memo: params["tag"] = self.memo
            o = await ff("create", **params)
            ok = (o["from"]["code"] == self.send and o["to"]["code"] == self.recv
                  and o["to"]["address"].lower() == self.address.lower()
                  and o["from"]["address"] and abs(float(o["from"]["amount"]) - float(self.amount)) < 1e-9)
        except (FFError, KeyError, ValueError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            return await self._go(i, "address", f"Order failed (nothing to pay): {e}. Check the address and try again.",
                                  deferred=True)
        self.stop()
        if not ok:
            print("MISMATCH", o.get("id"))
            return await i.edit_original_response(view=None, embed=discord.Embed(
                title="Order didn't match your request", color=0xED4245,
                description="For safety I won't show a deposit address. Do NOT send funds. Press Start swap to try again."))
        db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,NULL)",
                   (o["id"], o["token"], i.user.id, i.channel_id, o["status"], time.time())); db.commit()
        try:
            await i.channel.send(i.user.mention, embed=invoice_embed(o), view=EmergencyView())
            await i.edit_original_response(view=None, embed=discord.Embed(
                title="Invoice posted in this ticket ✅", color=0x57F287))
        except discord.HTTPException:
            await i.edit_original_response(embed=invoice_embed(o), view=None)


def invoice_embed(o):
    f, t = o["from"], o["to"]
    left = int(o.get("time", {}).get("left") or 0)
    e = discord.Embed(title=f'Invoice · order {o["id"]}', color=0xFEE75C, url=f'https://ff.io/order/{o["id"]}')
    e.add_field(name="1. Send EXACTLY", value=f'```{f["amount"]} {f["code"]}```', inline=False)
    e.add_field(name="2. To this address", value=f'```{f["address"]}```', inline=False)
    if f.get("tagValue"):
        e.add_field(name=f'Deposit {f.get("tag") or "memo"} (required)', value=f'```{f["tagValue"]}```', inline=False)
    e.add_field(name="You receive (est.)", value=f'{t["amount"]} {t["code"]}')
    e.add_field(name="Payout address", value=f'`{t["address"]}`', inline=False)
    if left: e.add_field(name="Expires", value=f"<t:{int(time.time()) + left}:R>")
    e.set_footer(text=f'Send only {f["code"]} on the {f.get("network") or f["code"]} network. Wrong amount/coin may delay or lose funds.')
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


class EmergencyView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

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
async def notify(uid, ch_id, o):
    st = o["status"]
    color = 0x57F287 if st == "DONE" else 0xED4245 if st in ("EXPIRED", "EMERGENCY") else 0x5865F2
    e = discord.Embed(title=f'Order {o["id"]}: {STATUS.get(st, st)}', color=color, url=f'https://ff.io/order/{o["id"]}')
    tx = o.get("tx") or {}
    tin, tout = (tx.get("in") or {}), (tx.get("out") or {})
    if tin.get("id"): e.add_field(name="Deposit TxID", value=f'```{tin["id"]}```', inline=False)
    if tout.get("id"):
        e.add_field(name="Payout TxID", value=f'```{tout["id"]}```', inline=False)
        e.add_field(name="Received", value=f'{tout.get("amount") or o["to"]["amount"]} {o["to"]["code"]}')
    if st in FINAL: e.set_footer(text=f"This ticket closes in {CLOSE_DELAY // 60} minutes.")
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
async def setup_hook():
    global session
    session = aiohttp.ClientSession()
    await refresh()
    if not ccies: raise SystemExit("Could not load currencies: check FF_API_KEY / FF_API_SECRET")
    for v in (PanelView(), TicketView(), EmergencyView()): bot.add_view(v)
    refresh.start(); poll.start()
    await bot.tree.sync()


bot.run(os.environ["DISCORD_TOKEN"])
