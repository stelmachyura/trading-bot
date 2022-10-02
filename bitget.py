import asyncio
import hashlib
import hmac
import json
import traceback
from time import time
from typing import Union, List, Dict
from urllib.parse import urlencode
from uuid import uuid4

import aiohttp
import base64
import numpy as np
import pprint

from njit_funcs import round_
from passivbot import Bot
from procedures import print_async_exception, print_
from pure_funcs import ts_to_date, sort_dict_keys, date_to_ts


def first_capitalized(s: str):
    return s[0].upper() + s[1:].lower()


def truncate_float(x: float, d: int) -> float:
    xs = str(x)
    return float(xs[: xs.find(".") + d + 1])


class BitgetBot(Bot):
    def __init__(self, config: dict):
        self.exchange = "bitget"
        super().__init__(config)
        self.base_endpoint = "https://api.bitget.com"
        self.endpoints = {
            "exchange_info": "/api/mix/v1/market/contracts",
            "funds_transfer": "/asset/v1/private/transfer",
            "position": "/api/mix/v1/position/singlePosition",
            "balance": "/api/mix/v1/account/accounts",
            "ticker": "/api/mix/v1/market/ticker",
            "open_orders": "/api/mix/v1/order/current",
            "create_order": "/api/mix/v1/order/placeOrder",
            "cancel_order": "/api/mix/v1/order/cancel-order",
            "ticks": "/api/mix/v1/market/fills",
            "fills": "/api/mix/v1/order/fills",
            "ohlcvs": "/api/mix/v1/market/candles",
            "websocket_market": "wss://ws.bitget.com/mix/v1/stream",
            "websocket_user": "wss://ws.bitget.com/mix/v1/stream",
            "set_margin_mode": "/api/mix/v1/account/setMarginMode",
            "set_leverage": "/api/mix/v1/account/setLeverage",
        }
        self.order_side_map = {
            "buy": {"long": "open_long", "short": "close_short"},
            "sell": {"long": "close_long", "short": "open_short"},
        }
        self.fill_side_map = {
            "close_long": "sell",
            "open_long": "buy",
            "close_short": "buy",
            "open_short": "sell",
        }
        self.interval_map = {
            "1m": "60",
            "5m": "300",
            "15m": "900",
            "30m": "1800",
            "1h": "3600",
            "4h": "14400",
            "12h": "43200",
            "1d": "86400",
            "1w": "604800",
        }
        self.broker_code = "Passivbot"
        self.session = aiohttp.ClientSession()

    def init_market_type(self):
        self.symbol_stripped = self.symbol
        if self.symbol.endswith("USDT"):
            print("linear perpetual")
            self.symbol += "_UMCBL"
            self.market_type += "_linear_perpetual"
            self.product_type = "umcbl"
            self.inverse = self.config["inverse"] = False
            self.min_cost = self.config["min_cost"] = 5.0
        elif self.symbol.endswith("USD"):
            print("inverse perpetual")
            self.symbol += "_DMCBL"
            self.market_type += "_inverse_perpetual"
            self.product_type = "dmcbl"
            self.inverse = self.config["inverse"] = False
            self.min_cost = self.config[
                "min_cost"
            ] = 6.0  # will complain with $5 even if order cost > $5
        else:
            raise NotImplementedError("not yet implemented")

    async def _init(self):
        self.init_market_type()
        info = await self.fetch_exchange_info()
        for e in info["data"]:
            if e["symbol"] == self.symbol:
                break
        else:
            raise Exception(f"symbol missing {self.symbol}")
        self.coin = e["baseCoin"]
        self.quote = e["quoteCoin"]
        self.price_step = self.config["price_step"] = round_(
            (10 ** (-int(e["pricePlace"]))) * int(e["priceEndStep"]), 0.00000001
        )
        self.price_rounding = int(e["pricePlace"])
        self.qty_step = self.config["qty_step"] = round_(10 ** (-int(e["volumePlace"])), 0.00000001)
        self.min_qty = self.config["min_qty"] = float(e["minTradeNum"])
        self.margin_coin = self.coin if self.product_type == "dmcbl" else self.quote
        await super()._init()
        await self.init_order_book()
        await self.update_position()

    async def fetch_exchange_info(self):
        info = await self.public_get(
            self.endpoints["exchange_info"], params={"productType": self.product_type}
        )
        return info

    async def fetch_ticker(self, symbol=None):
        ticker = await self.public_get(
            self.endpoints["ticker"], params={"symbol": self.symbol if symbol is None else symbol}
        )
        return {
            "symbol": ticker["data"]["symbol"],
            "bid": float(ticker["data"]["bestBid"]),
            "ask": float(ticker["data"]["bestAsk"]),
            "last": float(ticker["data"]["last"]),
        }

    async def init_order_book(self):
        ticker = await self.fetch_ticker()
        self.ob = [
            ticker["bid"],
            ticker["ask"],
        ]
        self.price = ticker["last"]

    async def fetch_open_orders(self) -> [dict]:
        fetched = await self.private_get(self.endpoints["open_orders"], {"symbol": self.symbol})
        return [
            {
                "order_id": elm["orderId"],
                "custom_id": elm["clientOid"],
                "symbol": elm["symbol"],
                "price": float(elm["price"]),
                "qty": float(elm["size"]),
                "side": "buy" if elm["side"] in ["close_short", "open_long"] else "sell",
                "position_side": elm["posSide"],
                "timestamp": float(elm["cTime"]),
            }
            for elm in fetched["data"]
        ]

    async def public_get(self, url: str, params: dict = {}) -> dict:
        async with self.session.get(self.base_endpoint + url, params=params) as response:
            result = await response.text()
        return json.loads(result)

    async def private_(
        self, type_: str, base_endpoint: str, url: str, params: dict = {}, json_: bool = False
    ) -> dict:

        timestamp = int(time() * 1000)
        params = {
            k: ("true" if v else "false") if type(v) == bool else str(v) for k, v in params.items()
        }
        if type_ == "get":
            url = url + "?" + urlencode(sort_dict_keys(params))
            to_sign = str(timestamp) + type_.upper() + url
        elif type_ == "post":
            to_sign = str(timestamp) + type_.upper() + url + json.dumps(params)
        signature = base64.b64encode(
            hmac.new(
                self.secret.encode("utf-8"),
                to_sign.encode("utf-8"),
                digestmod="sha256",
            ).digest()
        ).decode("utf-8")
        header = {
            "Content-Type": "application/json",
            "locale": "en-US",
            "ACCESS-KEY": self.key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": str(timestamp),
            "ACCESS-PASSPHRASE": self.passphrase,
        }
        if type_ == "post":
            async with getattr(self.session, type_)(
                base_endpoint + url, headers=header, data=json.dumps(params)
            ) as response:
                result = await response.text()
        elif type_ == "get":
            async with getattr(self.session, type_)(base_endpoint + url, headers=header) as response:
                result = await response.text()
        return json.loads(result)

    async def private_get(self, url: str, params: dict = {}, base_endpoint: str = None) -> dict:
        return await self.private_(
            type_="get",
            base_endpoint=self.base_endpoint if base_endpoint is None else base_endpoint,
            url=url,
            params=params,
        )

    async def private_post(self, url: str, params: dict = {}, base_endpoint: str = None) -> dict:
        return await self.private_(
            type_="post",
            base_endpoint=self.base_endpoint if base_endpoint is None else base_endpoint,
            url=url,
            params=params,
        )

    async def transfer_from_derivatives_to_spot(self, coin: str, amount: float):
        raise NotImplementedError("not implemented")
        params = {
            "coin": coin,
            "amount": str(amount),
            "from_account_type": "CONTRACT",
            "to_account_type": "SPOT",
            "transfer_id": str(uuid4()),
        }
        return await self.private_(
            "post", self.base_endpoint, self.endpoints["funds_transfer"], params=params, json_=True
        )

    async def get_server_time(self):
        now = await self.public_get("/api/spot/v1/public/time")
        return float(now["data"])

    async def fetch_position(self) -> dict:
        """
        returns {"long": {"size": float, "price": float, "liquidation_price": float},
                 "short": {...},
                 "wallet_balance": float}
        """
        position = {
            "long": {"size": 0.0, "price": 0.0, "liquidation_price": 0.0},
            "short": {"size": 0.0, "price": 0.0, "liquidation_price": 0.0},
            "wallet_balance": 0.0,
        }
        fetched_pos, fetched_balance = await asyncio.gather(
            self.private_get(
                self.endpoints["position"], {"symbol": self.symbol, "marginCoin": self.margin_coin}
            ),
            self.private_get(self.endpoints["balance"], {"productType": self.product_type}),
        )
        for elm in fetched_pos["data"]:
            if elm["holdSide"] == "long":
                position["long"] = {
                    "size": round_(float(elm["total"]), self.qty_step),
                    "price": truncate_float(float(elm["averageOpenPrice"]), self.price_rounding),
                    "liquidation_price": float(elm["liquidationPrice"]),
                }

            elif elm["holdSide"] == "short":
                position["short"] = {
                    "size": -abs(round_(float(elm["total"]), self.qty_step)),
                    "price": truncate_float(float(elm["averageOpenPrice"]), self.price_rounding),
                    "liquidation_price": float(elm["liquidationPrice"]),
                }
        for elm in fetched_balance["data"]:
            if elm["marginCoin"] == self.margin_coin:
                if self.product_type == "dmcbl":
                    # convert balance to usd using mean of emas as price
                    all_emas = list(self.emas_long) + list(self.emas_short)
                    if any(ema == 0.0 for ema in all_emas):
                        # catch case where any ema is zero
                        all_emas = self.ob
                    position["wallet_balance"] = float(elm["available"]) * np.mean(all_emas)
                else:
                    position["wallet_balance"] = float(elm["available"])
                break

        return position

    async def execute_order(self, order: dict) -> dict:
        o = None
        try:
            params = {
                "symbol": self.symbol,
                "marginCoin": self.margin_coin,
                "size": str(order["qty"]),
                "side": self.order_side_map[order["side"]][order["position_side"]],
                "orderType": order["type"],
                "presetTakeProfitPrice": "",
                "presetStopLossPrice": "",
            }
            if params["orderType"] == "limit":
                params["timeInForceValue"] = "post_only"
                params["price"] = str(order["price"])
            else:
                params["timeInForceValue"] = "normal"
            random_str = f"{str(int(time() * 1000))[-6:]}_{int(np.random.random() * 10000)}"
            custom_id = order["custom_id"] if "custom_id" in order else "0"
            params["clientOid"] = f"{self.broker_code}#{custom_id}_{random_str}"
            o = await self.private_post(self.endpoints["create_order"], params)
            if o["data"]:
                # print('debug execute order', o)
                return {
                    "symbol": self.symbol,
                    "side": order["side"],
                    "order_id": o["data"]["orderId"],
                    "position_side": order["position_side"],
                    "type": order["type"],
                    "qty": order["qty"],
                    "price": order["price"],
                }
            else:
                return o, order
        except Exception as e:
            print(f"error executing order {order} {e}")
            print_async_exception(o)
            traceback.print_exc()
            return {}

    async def execute_cancellation(self, order: dict) -> dict:
        cancellation = None
        try:
            cancellation = await self.private_post(
                self.endpoints["cancel_order"],
                {"symbol": self.symbol, "marginCoin": self.margin_coin, "orderId": order["order_id"]},
            )
            return {
                "symbol": self.symbol,
                "side": order["side"],
                "order_id": cancellation["data"]["orderId"],
                "position_side": order["position_side"],
                "qty": order["qty"],
                "price": order["price"],
            }
        except Exception as e:
            print(f"error cancelling order {order} {e}")
            print_async_exception(cancellation)
            traceback.print_exc()
            self.ts_released["force_update"] = 0.0
            return {}

    async def fetch_account(self):
        raise NotImplementedError("not implemented")
        try:
            resp = await self.private_get(
                self.endpoints["spot_balance"], base_endpoint=self.spot_base_endpoint
            )
            return resp["result"]
        except Exception as e:
            print("error fetching account: ", e)
            return {"balances": []}

    async def fetch_ticks(self, from_id: int = None, do_print: bool = True):
        params = {"symbol": self.symbol, "limit": 100}
        try:
            ticks = await self.public_get(self.endpoints["ticks"], params)
        except Exception as e:
            print("error fetching ticks", e)
            return []
        try:
            trades = [
                {
                    "trade_id": int(tick["tradeId"]),
                    "price": float(tick["price"]),
                    "qty": float(tick["size"]),
                    "timestamp": float(tick["timestamp"]),
                    "is_buyer_maker": tick["side"] == "sell",
                }
                for tick in ticks["data"]
            ]
            if do_print:
                print_(
                    [
                        "fetched trades",
                        self.symbol,
                        trades[0]["trade_id"],
                        ts_to_date(float(trades[0]["timestamp"]) / 1000),
                    ]
                )
        except:
            trades = []
            if do_print:
                print_(["fetched no new trades", self.symbol])
        return trades

    async def fetch_ohlcvs(self, symbol: str = None, start_time: int = None, interval="1m"):
        # m -> minutes, h -> hours, d -> days, w -> weeks
        assert interval in self.interval_map, f"unsupported interval {interval}"
        params = {
            "symbol": self.symbol if symbol is None else symbol,
            "granularity": self.interval_map[interval],
        }
        limit = 100
        seconds = float(self.interval_map[interval])
        if start_time is None:
            server_time = await self.get_server_time()
            params["startTime"] = int(round(float(server_time)) - 1000 * seconds * limit)
        else:
            params["startTime"] = int(round(start_time))
        params["endTime"] = int(round(params["startTime"] + 1000 * seconds * limit))
        fetched = await self.public_get(self.endpoints["ohlcvs"], params)
        return [
            {
                "timestamp": float(e[0]),
                "open": float(e[1]),
                "high": float(e[2]),
                "low": float(e[3]),
                "close": float(e[4]),
                "volume": float(e[5]),
            }
            for e in fetched
        ]

    async def get_all_income(
        self,
        symbol: str = None,
        start_time: int = None,
        income_type: str = "Trade",
        end_time: int = None,
    ):
        raise NotImplementedError("not implemented")
        if symbol is None:
            all_income = []
            all_positions = await self.private_get(self.endpoints["position"], params={"symbol": ""})
            symbols = sorted(
                set(
                    [
                        x["data"]["symbol"]
                        for x in all_positions["result"]
                        if float(x["data"]["size"]) > 0
                    ]
                )
            )
            for symbol in symbols:
                all_income += await self.get_all_income(
                    symbol=symbol, start_time=start_time, income_type=income_type, end_time=end_time
                )
            return sorted(all_income, key=lambda x: x["timestamp"])
        limit = 50
        income = []
        page = 1
        while True:
            fetched = await self.fetch_income(
                symbol=symbol,
                start_time=start_time,
                income_type=income_type,
                limit=limit,
                page=page,
            )
            if len(fetched) == 0:
                break
            print_(["fetched income", symbol, ts_to_date(fetched[0]["timestamp"])])
            if fetched == income[-len(fetched) :]:
                break
            income += fetched
            if len(fetched) < limit:
                break
            page += 1
        income_d = {e["transaction_id"]: e for e in income}
        return sorted(income_d.values(), key=lambda x: x["timestamp"])

    async def fetch_income(
        self,
        symbol: str = None,
        income_type: str = None,
        limit: int = 50,
        start_time: int = None,
        end_time: int = None,
        page=None,
    ):
        raise NotImplementedError("not implemented")
        params = {"limit": limit, "symbol": self.symbol if symbol is None else symbol}
        if start_time is not None:
            params["start_time"] = int(start_time / 1000)
        if end_time is not None:
            params["end_time"] = int(end_time / 1000)
        if income_type is not None:
            params["exec_type"] = first_capitalized(income_type)
        if page is not None:
            params["page"] = page
        fetched = None
        try:
            fetched = await self.private_get(self.endpoints["income"], params)
            if fetched["result"]["data"] is None:
                return []
            return sorted(
                [
                    {
                        "symbol": e["symbol"],
                        "income_type": e["exec_type"].lower(),
                        "income": float(e["closed_pnl"]),
                        "token": self.margin_coin,
                        "timestamp": float(e["created_at"]) * 1000,
                        "info": {"page": fetched["result"]["current_page"]},
                        "transaction_id": float(e["id"]),
                        "trade_id": e["order_id"],
                    }
                    for e in fetched["result"]["data"]
                ],
                key=lambda x: x["timestamp"],
            )
        except Exception as e:
            print("error fetching income: ", e)
            traceback.print_exc()
            print_async_exception(fetched)
            return []

    async def fetch_fills(
        self,
        symbol=None,
        limit: int = 100,
        from_id: int = None,
        start_time: int = None,
        end_time: int = None,
    ):
        params = {"symbol": self.symbol if symbol is None else symbol}
        if from_id is not None:
            params["lastEndId"] = max(0, from_id - 1)
        if start_time is None:
            server_time = await self.get_server_time()
            params["startTime"] = int(round(server_time - 1000 * 60 * 60 * 24))
        else:
            params["startTime"] = int(round(start_time))

        # always fetch as many fills as possible
        params["endTime"] = int(round(time() + 60 * 60 * 24) * 1000)
        try:
            fetched = await self.private_get(self.endpoints["fills"], params)
            fills = [
                {
                    "symbol": x["symbol"],
                    "id": int(x["tradeId"]),
                    "order_id": int(x["orderId"]),
                    "side": self.fill_side_map[x["side"]],
                    "price": float(x["price"]),
                    "qty": float(x["sizeQty"]),
                    "realized_pnl": float(x["profit"]),
                    "cost": float(x["fillAmount"]),
                    "fee_paid": float(x["fee"]),
                    "fee_token": self.quote,
                    "timestamp": int(x["cTime"]),
                    "position_side": "long" if "long" in x["side"] else "short",
                    "is_maker": "unknown",
                }
                for x in fetched["data"]
            ]
        except Exception as e:
            print("error fetching fills", e)
            traceback.print_exc()
            return []
        return fills

    async def init_exchange_config(self):
        try:
            # set margin mode
            res = await self.private_post(
                self.endpoints["set_margin_mode"],
                params={
                    "symbol": self.symbol,
                    "marginCoin": self.margin_coin,
                    "marginMode": "crossed",
                },
            )
            print(res)
            # set leverage
            res = await self.private_post(
                self.endpoints["set_leverage"],
                params={"symbol": self.symbol, "marginCoin": self.margin_coin, "leverage": 20},
            )
            print(res)
        except Exception as e:
            print(e)

    def standardize_market_stream_event(self, data: dict) -> [dict]:
        if "action" not in data or data["action"] != "update":
            return []
        ticks = []
        for e in data["data"]:
            try:
                ticks.append(
                    {
                        "timestamp": int(e[0]),
                        "price": float(e[1]),
                        "qty": float(e[2]),
                        "is_buyer_maker": e[3] == "sell",
                    }
                )
            except Exception as ex:
                print("error in websocket tick", e, ex)
        return ticks

    async def beat_heart_user_stream(self) -> None:
        while True:
            await asyncio.sleep(27)
            try:
                await self.ws_user.send(json.dumps({"op": "ping"}))
            except Exception as e:
                traceback.print_exc()
                print_(["error sending heartbeat user", e])

    async def beat_heart_market_stream(self) -> None:
        while True:
            await asyncio.sleep(27)
            try:
                await self.ws_market.send(json.dumps({"op": "ping"}))
            except Exception as e:
                traceback.print_exc()
                print_(["error sending heartbeat market", e])

    async def subscribe_to_market_stream(self, ws):
        await ws.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "args": [
                        {
                            "instType": "mc",
                            "channel": "trade",
                            "instId": self.symbol_stripped,
                        }
                    ],
                }
            )
        )

    async def subscribe_to_user_stream(self, ws):
        timestamp = int(time())
        signature = base64.b64encode(
            hmac.new(
                self.secret.encode("utf-8"),
                f"{timestamp}GET/user/verify".encode("utf-8"),
                digestmod="sha256",
            ).digest()
        ).decode("utf-8")
        res = await ws.send(
            json.dumps(
                {
                    "op": "login",
                    "args": [
                        {
                            "apiKey": self.key,
                            "passphrase": self.passphrase,
                            "timestamp": timestamp,
                            "sign": signature,
                        }
                    ],
                }
            )
        )
        print(res)
        res = await ws.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "args": [
                        {
                            "instType": self.product_type.upper(),
                            "channel": "account",
                            "instId": "default",
                        }
                    ],
                }
            )
        )
        print(res)
        res = await ws.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "args": [
                        {
                            "instType": self.product_type.upper(),
                            "channel": "positions",
                            "instId": "default",
                        }
                    ],
                }
            )
        )
        print(res)
        res = await ws.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "args": [
                        {
                            "channel": "orders",
                            "instType": self.product_type.upper(),
                            "instId": "default",
                        }
                    ],
                }
            )
        )
        print(res)

    async def transfer(self, type_: str, amount: float, asset: str = "USDT"):
        return {"code": "-1", "msg": "Transferring funds not supported for Bybit"}

    def standardize_user_stream_event(
        self, event: Union[List[Dict], Dict]
    ) -> Union[List[Dict], Dict]:

        events = []
        if "arg" in event and "data" in event and "channel" in event["arg"]:
            if event["arg"]["channel"] == "orders":
                for elm in event["data"]:
                    if elm["instId"] == self.symbol and "status" in elm:
                        standardized = {}
                        if elm["status"] == "cancelled":
                            standardized["deleted_order_id"] = elm["ordId"]
                        elif elm["status"] == "new":
                            standardized["new_open_order"] = {
                                "order_id": elm["ordId"],
                                "symbol": elm["instId"],
                                "price": float(elm["px"]),
                                "qty": float(elm["sz"]),
                                "type": elm["ordType"],
                                "side": elm["side"],
                                "position_side": elm["posSide"],
                                "timestamp": elm["uTime"],
                            }
                        elif elm["status"] == "partial-fill":
                            standardized["deleted_order_id"] = elm["ordId"]
                            standardized["partially_filled"] = True
                        elif elm["status"] == "full-fill":
                            standardized["deleted_order_id"] = elm["ordId"]
                            standardized["filled"] = True
                        events.append(standardized)
            if event["arg"]["channel"] == "positions":
                for elm in event["data"]:
                    if elm["instId"] == self.symbol and "averageOpenPrice" in elm:
                        standardized = {
                            f"psize_{elm['holdSide']}": round_(
                                abs(float(elm["total"])), self.qty_step
                            )
                            * (-1 if elm["holdSide"] == "short" else 1),
                            f"pprice_{elm['holdSide']}": truncate_float(
                                float(elm["averageOpenPrice"]), self.price_rounding
                            ),
                        }
                        events.append(standardized)

            if event["arg"]["channel"] == "account":
                for elm in event["data"]:
                    if elm["marginCoin"] == self.quote:
                        events.append({"wallet_balance": float(elm["available"])})
        return events
