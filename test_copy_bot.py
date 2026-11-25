"""Regressões do monitor e do envio. Todos os dados de rede são simulados."""
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from polymarket import PublicClient, SecureClient
from polymarket.errors import TransportError, UnexpectedResponseError

import test as bot


WALLET = "0x" + "12" * 20


def activity(asset="1", **changes):
    return dict({
        "proxy_wallet": WALLET, "transaction_hash": "0x" + "34" * 32,
        "condition_id": "0x" + "56" * 32, "type": "TRADE", "side": "BUY",
        "timestamp": 1767842561, "size": "20", "price": "0.1", "usdc_size": "2",
        "token_id": asset, "title": "Bitcoin",
    }, **changes)


class CopyTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "state.sqlite3"
        self.db = self.enterContext(closing(bot.open_state(self.path)))
        self.enterContext(redirect_stdout(StringIO()))
        self.client = Mock(spec=SecureClient)
        self.book = SimpleNamespace(asset_id="1", tick_size=Decimal(".01"), min_order_size=Decimal(5))
        self.client.get_order_book.return_value = self.book
        self.client.get_balance_allowance.return_value.balance = 5_000_000
        self.client.post_order.return_value = SimpleNamespace(ok=True, order_id="order1", status="live")

    def collect(self, pages):
        requests = []

        def send(_, req, **kwargs):
            self.assertEqual(req.url.path, "/v2/activity")
            cursor = int(req.url.params.get("cursor", 0))
            requests.append(cursor)
            rows = pages[cursor]
            more = cursor + 1 < len(pages)
            body = {"data": rows, "pagination": {
                "has_more": more, "next_cursor": str(cursor + 1) if more else None}}
            return httpx.Response(200, json=body, request=req)

        with PublicClient() as client, patch.object(httpx.Client, "send", send):
            bot.collect(client, self.db, WALLET, 1767842560, 1767842562)
        return requests

    def states(self):
        return [row[0] for row in self.db.execute("SELECT status FROM trades ORDER BY rowid")]

    def test_all_pages_same_second_and_overlapping_poll(self):
        page = [activity(str(i)) for i in range(1, 21)]
        pages = [page, [activity("21"), activity("21")]]
        self.assertEqual(self.collect(pages), [0, 1])
        self.collect(pages)
        self.assertEqual(len(self.states()), 22)

    def test_broken_page_rolls_back_then_can_be_read_again(self):
        with self.assertRaises(UnexpectedResponseError):
            self.collect([[activity()], None])
        self.assertEqual(self.states(), [])
        self.collect([[activity()], [activity("2")]])
        self.assertEqual(self.states(), ["pending", "pending"])

    def test_preflight_failure_retries_after_restart(self):
        self.collect([[activity()]])
        self.client.get_order_book.side_effect = [TransportError("offline"), self.book]
        with patch.object(bot.time, "time", return_value=100):
            bot.process_queue(self.client, self.db, False)
        self.assertEqual(self.states(), ["pending"])
        with closing(bot.open_state(self.path)) as reopened, patch.object(bot.time, "time", return_value=110):
            bot.process_queue(self.client, reopened, False)
        self.assertEqual(self.states(), ["accepted"])
        self.client.post_order.assert_called_once()

    def test_lost_ack_survives_restart_without_second_post(self):
        self.collect([[activity()]])
        self.client.post_order.side_effect = TransportError("resposta perdida")
        bot.process_queue(self.client, self.db, False)
        self.collect([[activity()]])
        with closing(bot.open_state(self.path)) as reopened:
            bot.process_queue(self.client, reopened, False)
        self.assertEqual(self.states(), ["unconfirmed"])
        self.client.post_order.assert_called_once()

    def test_dry_run_validates_price_and_market_minimum(self):
        self.collect([[activity(price="0"), activity(price="0.5"), activity()]])
        bot.process_queue(self.client, self.db, True)
        self.assertEqual(self.states(), ["skipped", "skipped", "simulated"])
        self.client.get_balance_allowance.assert_not_called()
        self.client.create_limit_order.assert_not_called()
        self.client.post_order.assert_not_called()

    def test_tick_size_and_spending_cap(self):
        self.collect([[activity(price="0.33000003")]])
        self.book.min_order_size = Decimal(1)
        with patch.object(bot, "BET_MODE", "RATIO"), patch.object(bot, "COPY_RATIO", Decimal(9)):
            bot.process_queue(self.client, self.db, False)
        params = self.client.create_limit_order.call_args.kwargs
        self.assertEqual(params["price"], Decimal(".33"))
        self.assertEqual(params["size"], Decimal("15.15"))
        self.assertLessEqual(params["size"] * params["price"], bot.MAX_USDC_PER_TRADE)

    def test_insufficient_balance_never_posts(self):
        self.collect([[activity()]])
        self.client.get_balance_allowance.return_value.balance = 499_999
        bot.process_queue(self.client, self.db, False)
        self.assertEqual(self.states(), ["skipped"])
        self.client.post_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
