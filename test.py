"""Bot de copy trading. Execute com python test.py."""
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from dotenv import load_dotenv
from polymarket import PublicClient, SecureClient, TradeActivity
from polymarket.errors import PolymarketError, RateLimitError, RequestRejectedError

load_dotenv()
TARGET_WALLET = os.getenv("TARGET_WALLET", "").strip()
DRY_RUN = True
WHITELIST_WORDS = ["Bitcoin", "Ethereum", "XRP"]
BET_MODE = "FIXED"
FIXED_AMOUNT = Decimal("0.50")
COPY_RATIO = Decimal("0.10")
MAX_USDC_PER_TRADE = Decimal("5.00")
MIN_USDC_PER_TRADE = Decimal("0.10")
POLL_SECONDS = 1
OVERLAP_SECONDS = 60
CENT = Decimal("0.01")


def open_state(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY, tx TEXT NOT NULL, asset TEXT NOT NULL,
            timestamp INTEGER NOT NULL, title TEXT NOT NULL, price TEXT NOT NULL,
            amount TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            retry_at INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS queue ON trades(status, retry_at)")
    return db


def collect(client, db, wallet, start, end):
    pages = client.list_activity(user=wallet, activity_types=["TRADE"], side="BUY",
                                 sort_direction="ASC", start=start, end=end)
    occurrences = Counter()
    # Commit the whole window or retry it. A partial page walk never advances the cursor.
    with db:
        for page in pages:
            for trade in page.items:
                if not isinstance(trade, TradeActivity) or trade.side != "BUY":
                    continue
                title = trade.title or ""
                if WHITELIST_WORDS and not any(w.lower() in title.lower() for w in WHITELIST_WORDS):
                    continue
                ts = int(trade.timestamp.timestamp())
                # The activity feed has no fill ID. Count even identical fills in the same tx.
                identity = json.dumps([trade.transaction_hash.lower(), ts, trade.asset_id,
                                       trade.side, *[str(n.normalize()) for n in
                                                     (trade.price, trade.shares, trade.amount)]])
                occurrences[identity] += 1
                key = hashlib.sha256(identity.encode()).hexdigest() + f"-{occurrences[identity]}"
                db.execute("""INSERT INTO trades(id, tx, asset, timestamp, title, price, amount)
                           VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING""",
                           (key, trade.transaction_hash, trade.asset_id, ts, title,
                            str(trade.price), str(trade.amount)))
            if page.limit_reached:
                raise ValueError("A API interrompeu a paginação antes de confirmar o fim.")


class SkipTrade(ValueError):
    pass


def prepare_order(client, trade):
    observed = Decimal(trade["price"])
    if not observed.is_finite() or not 0 < observed < 1:
        raise SkipTrade("Preço fora do intervalo (0, 1).")
    budget = FIXED_AMOUNT if BET_MODE == "FIXED" else Decimal(trade["amount"]) * COPY_RATIO
    budget = min(budget, MAX_USDC_PER_TRADE).quantize(CENT, rounding=ROUND_DOWN)
    if budget < MIN_USDC_PER_TRADE:
        raise SkipTrade("Valor abaixo do mínimo configurado.")
    book = client.get_order_book(asset_id=trade["asset"])
    tick = book.tick_size
    if tick <= 0 or book.min_order_size <= 0 or book.asset_id != trade["asset"]:
        raise ValueError("Livro de ordens inconsistente.")
    # A price averaged across fills may fall between ticks. Never bid above it.
    price = (observed // tick) * tick
    if price < tick or price > 1 - tick:
        raise SkipTrade("Preço fora da grade do mercado.")
    size = (budget / price).quantize(CENT, rounding=ROUND_DOWN)
    if size < book.min_order_size or size * price < MIN_USDC_PER_TRADE:
        raise SkipTrade(f"{size} cotas a {price} fora dos mínimos do mercado ou da configuração.")
    return price, size


def transition(db, key, previous, status, detail, retry_at=0):
    with db:
        changed = db.execute("""UPDATE trades SET status=?, detail=?, retry_at=?
                                WHERE id=? AND status=?""",
                             (status, detail, retry_at, key, previous)).rowcount
    if changed:
        print(f"[{status}] {key[:12]} {detail}", flush=True)
    return bool(changed)


def defer(db, trade, previous, reason):
    delay = max(5, getattr(reason, "retry_after", None) or 0)
    transition(db, trade["id"], previous, "pending", str(reason), time.time() + delay)


def process_queue(client, db, dry_run):
    rows = db.execute("""SELECT * FROM trades WHERE status='pending' AND retry_at<=?
                         ORDER BY timestamp, rowid""", (time.time(),)).fetchall()
    for trade in rows:
        key = trade["id"]
        try:
            price, size = prepare_order(client, trade)
            summary = f'{trade["title"]} | {size} cotas a {price} | {price * size} pUSD'
            if dry_run:
                transition(db, key, "pending", "simulated", summary)
                continue
            balance = client.get_balance_allowance(asset_type="COLLATERAL").balance
            if price * size > Decimal(balance) / 1_000_000:
                raise SkipTrade("Saldo insuficiente.")
            signed = client.create_limit_order(asset_id=trade["asset"], price=price, size=size, side="BUY")
        except SkipTrade as exc:
            transition(db, key, "pending", "skipped", str(exc))
            continue
        except (PolymarketError, ValueError) as exc:
            defer(db, trade, "pending", exc)
            continue

        # Commit before POST. A crash or a lost reply must never cause a second signature/send.
        if not transition(db, key, "pending", "unconfirmed", summary):
            continue
        try:
            reply = client.post_order(signed)
        except RateLimitError as exc:
            defer(db, trade, "unconfirmed", exc)
        except RequestRejectedError as exc:
            if exc.status in (425, 429):
                defer(db, trade, "unconfirmed", exc)
            elif 400 <= exc.status < 500 and exc.status not in (408, 409):
                transition(db, key, "unconfirmed", "rejected", str(exc))
            else:
                print(f"Envio incerto ({exc}). Confira a ordem na carteira: {summary}", flush=True)
        except PolymarketError as exc:
            print(f"Envio incerto ({exc}). Confira a ordem na carteira: {summary}", flush=True)
        else:
            if reply.ok:
                transition(db, key, "unconfirmed", "accepted", f"{reply.order_id} ({reply.status})")
            elif reply.code in ("market_not_ready", "post_only_mode"):
                defer(db, trade, "unconfirmed", reply.message)
            else:
                transition(db, key, "unconfirmed", "rejected", reply.message)


def main():
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", TARGET_WALLET):
        raise SystemExit("Configure TARGET_WALLET com o endereço público a acompanhar.")
    if BET_MODE not in ("FIXED", "RATIO") or not 0 < MIN_USDC_PER_TRADE <= MAX_USDC_PER_TRADE:
        raise SystemExit("Confira BET_MODE e os limites por operação.")
    if DRY_RUN:
        client = PublicClient()
        account = "sim"
    else:
        key, funder = os.getenv("POLY_PRIVATE_KEY"), os.getenv("POLY_FUNDER")
        if not key or not funder:
            raise SystemExit("Configure POLY_PRIVATE_KEY e POLY_FUNDER no .env.")
        client = SecureClient.create(private_key=key, wallet=funder)
        account = client.wallet.lower()

    path = Path(__file__).with_name(f"copy_{TARGET_WALLET.lower()}_{account}.sqlite3")
    start = int(time.time()) + 1
    window = start
    with client, closing(open_state(path)) as db:
        print(f"Alvo {TARGET_WALLET} | {'SIMULAÇÃO' if DRY_RUN else 'ORDENS REAIS'} | {path.name}")
        for row in db.execute("SELECT tx, asset, detail FROM trades WHERE status='unconfirmed'"):
            print(f'Confira o envio na carteira: {row["tx"]} | ativo {row["asset"]} | {row["detail"]}')
        while True:
            end = int(time.time())
            delay = POLL_SECONDS
            if end >= window:
                try:
                    collect(client, db, TARGET_WALLET, window, end)
                except (PolymarketError, ValueError) as exc:
                    print(f"Consulta de atividade falhou: {exc}", flush=True)
                    delay = max(5, getattr(exc, "retry_after", None) or 0)
                else:
                    window = max(start, end - OVERLAP_SECONDS)
            process_queue(client, db, DRY_RUN)
            time.sleep(delay)


if __name__ == "__main__":
    main()
