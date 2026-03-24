import os
import time
import logging
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from eth_account import Account
from web3 import Web3

# ========= LOGGING =========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ========= CONFIG =========
PRIVATE_KEY = "2c9198f6e8b255d0bc953248177116576bbd02de638c6c4ea698beb45e6f7a8e"

class TradingConfig:
    PROXY_URL = os.getenv("PROXY_URL", "")  # e.g. socks5://user:pass@host:port or http://host:port
    MAX_TRADE_SIZE = float(os.getenv("MAX_TRADE_SIZE", "10"))
    MAX_POSITION = float(os.getenv("MAX_POSITION", "20"))
    MIN_BALANCE_USDC = float(os.getenv("MIN_BALANCE_USDC", "50"))

    USE_DYNAMIC_PRICES = os.getenv("USE_DYNAMIC_PRICES", "True") == "True"
    FIXED_BUY_PRICE = float(os.getenv("FIXED_BUY_PRICE", "0.45"))
    FIXED_SELL_PRICE = float(os.getenv("FIXED_SELL_PRICE", "0.55"))
    DYNAMIC_BUY_OFFSET = float(os.getenv("DYNAMIC_BUY_OFFSET", "0.02"))
    DYNAMIC_SELL_OFFSET = float(os.getenv("DYNAMIC_SELL_OFFSET", "0.02"))

    TARGET_PROFIT_SPREAD = float(os.getenv("TARGET_PROFIT_SPREAD", "0.07"))
    POLYMARKET_FEE = float(os.getenv("POLYMARKET_FEE", "0.02"))

    USE_STOP_LOSS = os.getenv("USE_STOP_LOSS", "True") == "True"
    STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", "0.15"))
    TRAILING_STOP = os.getenv("TRAILING_STOP", "True") == "True"
    TRAILING_STOP_PERCENT = float(os.getenv("TRAILING_STOP_PERCENT", "0.05"))

    IMBALANCE_THRESHOLD = float(os.getenv("IMBALANCE_THRESHOLD", "3"))
    REBALANCE_SIZE = float(os.getenv("REBALANCE_SIZE", "1"))

    SLEEP_BETWEEN_CYCLES = float(os.getenv("SLEEP_BETWEEN_CYCLES", "3"))

    MIN_VOLUME = float(os.getenv("MIN_VOLUME", "1000"))
    MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "100"))
    MAX_MARKETS_TO_SCAN = int(os.getenv("MAX_MARKETS_TO_SCAN", "200"))

config = TradingConfig()

# ========= PROXY SETUP =========
# RPC-адреса идут напрямую (без прокси) — Web3 быстрый и без таймаутов
_NO_PROXY_HOSTS = (
    "polygon.llamarpc.com,"
    "polygon-bor.publicnode.com,"
    "rpc-mainnet.maticvigil.com,"
    "matic-mainnet.chainstacklabs.com,"
    "api.polygonscan.com"
)

if config.PROXY_URL:
    os.environ["HTTP_PROXY"] = config.PROXY_URL
    os.environ["HTTPS_PROXY"] = config.PROXY_URL
    os.environ["NO_PROXY"] = _NO_PROXY_HOSTS
    logging.info(f"🔀 Прокси для CLOB: {config.PROXY_URL.split('@')[-1]}")
    logging.info("⏩ RPC идут напрямую (NO_PROXY)")

# Сессия requests с прокси для наших запросов (гамма API, геоблок)
_session = requests.Session()
if config.PROXY_URL:
    _session.proxies = {"http": config.PROXY_URL, "https": config.PROXY_URL, "no": _NO_PROXY_HOSTS}

# ========= CLIENTS =========
RPC_URLS = [
    "https://polygon.llamarpc.com",
    "https://polygon-bor.publicnode.com",
    "https://rpc-mainnet.maticvigil.com",
    "https://matic-mainnet.chainstacklabs.com",
]

def get_web3_connection():
    for url in RPC_URLS:
        try:
            w = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
            if w.is_connected():
                return w
        except Exception:
            continue
    return None

w3 = get_web3_connection()

USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]

# ========= POLYMARKET CONTRACT ADDRESSES =========
POLYMARKET_ADDRESSES = {
    "USDC":              "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "CTF":               "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
    "CTF_EXCHANGE":      "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "NEG_RISK_EXCHANGE": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
}

ERC20_APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    }
]

ERC1155_APPROVE_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved",  "type": "bool"}
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "type": "function"
    },
    {
        "inputs": [
            {"name": "account",  "type": "address"},
            {"name": "operator", "type": "address"}
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    }
]

MAX_UINT256 = 2 ** 256 - 1


def _send_tx(w3_conn, acct, tx_dict):
    signed = acct.sign_transaction(tx_dict)
    tx_hash = w3_conn.eth.send_raw_transaction(signed.raw_transaction)
    logging.info(f"Tx sent: {tx_hash.hex()}")
    receipt = w3_conn.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status == 1:
        logging.info(f"Tx confirmed: {tx_hash.hex()}")
    else:
        logging.error(f"Tx failed: {tx_hash.hex()}")
    return receipt


def setup_approvals(w3_conn, acct):
    if not w3_conn:
        logging.warning("No Web3 connection — skipping on-chain approvals")
        return

    addr = acct.address
    logging.info(f"Checking approvals for {addr}")

    usdc = w3_conn.eth.contract(
        address=Web3.to_checksum_address(POLYMARKET_ADDRESSES["USDC"]),
        abi=ERC20_APPROVE_ABI
    )

    for exchange_key in ("CTF_EXCHANGE", "NEG_RISK_EXCHANGE"):
        spender = Web3.to_checksum_address(POLYMARKET_ADDRESSES[exchange_key])
        allowance = usdc.functions.allowance(addr, spender).call()
        if allowance < MAX_UINT256 // 2:
            logging.info(f"Approving USDC for {exchange_key} ...")
            tx = usdc.functions.approve(spender, MAX_UINT256).build_transaction({
                "from": addr,
                "nonce": w3_conn.eth.get_transaction_count(addr),
                "gas": 80_000,
                "maxFeePerGas": w3_conn.eth.gas_price * 2,
                "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
                "chainId": 137,
            })
            _send_tx(w3_conn, acct, tx)
        else:
            logging.info(f"USDC already approved for {exchange_key}")

    ctf = w3_conn.eth.contract(
        address=Web3.to_checksum_address(POLYMARKET_ADDRESSES["CTF"]),
        abi=ERC1155_APPROVE_ABI
    )

    for exchange_key in ("CTF_EXCHANGE", "NEG_RISK_EXCHANGE"):
        operator = Web3.to_checksum_address(POLYMARKET_ADDRESSES[exchange_key])
        approved = ctf.functions.isApprovedForAll(addr, operator).call()
        if not approved:
            logging.info(f"Approving CTF for {exchange_key} ...")
            tx = ctf.functions.setApprovalForAll(operator, True).build_transaction({
                "from": addr,
                "nonce": w3_conn.eth.get_transaction_count(addr),
                "gas": 80_000,
                "maxFeePerGas": w3_conn.eth.gas_price * 2,
                "maxPriorityFeePerGas": Web3.to_wei(30, "gwei"),
                "chainId": 137,
            })
            _send_tx(w3_conn, acct, tx)
        else:
            logging.info(f"CTF already approved for {exchange_key}")

    logging.info("Approval setup complete")


account = Account.from_key(PRIVATE_KEY)

client = ClobClient(
    "https://clob.polymarket.com",
    key=PRIVATE_KEY,
    chain_id=137,
)
def _init_api_creds(clob_client):
    """Создаём/обновляем API-ключи и проверяем, что они работают."""
    creds = clob_client.create_or_derive_api_creds()
    clob_client.set_api_creds(creds)
    logging.info(f"API key: {creds.api_key}")

    try:
        # Проверяем ключ через запрос открытых ордеров (требует авторизации)
        clob_client.get_orders()
        logging.info("✅ API ключ работает")
    except Exception as e:
        logging.warning(f"⚠️ API ключ не прошёл проверку ({e}), пересоздаём...")
        try:
            creds = clob_client.create_or_derive_api_creds()
            clob_client.set_api_creds(creds)
            clob_client.get_orders()
            logging.info(f"✅ Новый API ключ работает: {creds.api_key}")
        except Exception as e2:
            logging.error(f"❌ API ключ по-прежнему не работает: {e2}")

_init_api_creds(client)

GAMMA_API = "https://gamma-api.polymarket.com"


def check_geoblock():
    """Проверка геоблока через прокси — Polymarket видит IP прокси, а не Replit."""
    try:
        r = _session.get("https://polymarket.com/api/geoblock", timeout=15)
        data = r.json()
        if data.get("blocked"):
            logging.error(f"❌ IP {data.get('ip')} заблокирован в {data.get('country')}")
            return False
        logging.info(f"✅ IP {data.get('ip')} разрешён ({data.get('country')})")
        return True
    except Exception as e:
        logging.warning(f"⚠️ Геоблок не проверен: {e}. Продолжаем.")
        return True


_last_known_balance: float = 0.0

def get_usdc_balance() -> float:
    global _last_known_balance

    # Уровень 1: Web3 напрямую (идёт через NO_PROXY — быстро)
    if w3:
        try:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_CONTRACT),
                abi=USDC_ABI
            )
            raw = contract.functions.balanceOf(account.address).call()
            balance = raw / 1e6
            _last_known_balance = balance
            return balance
        except Exception as e:
            logging.warning(f"Web3 balance failed: {e}, пробуем PolygonScan...")

    # Уровень 2: PolygonScan API (идёт напрямую через NO_PROXY)
    try:
        resp = requests.get(
            "https://api.polygonscan.com/api",
            params={
                "module": "account",
                "action": "tokenbalance",
                "contractaddress": USDC_CONTRACT,
                "address": account.address,
                "tag": "latest",
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("status") == "1":
            balance = int(data.get("result", 0)) / 1e6
            _last_known_balance = balance
            logging.info(f"💰 PolygonScan баланс: ${balance:.2f}")
            return balance
        else:
            logging.warning(f"PolygonScan ответил: {data.get('message')}")
    except Exception as e:
        logging.warning(f"PolygonScan balance failed: {e}")

    # Уровень 3: Последний известный баланс (не останавливаем бота)
    if _last_known_balance > 0:
        logging.warning(f"⚠️ Используем последний известный баланс: ${_last_known_balance:.2f}")
        return _last_known_balance

    logging.error("❌ Не удалось получить баланс ни одним методом")
    return 0.0


def get_active_market():
    try:
        resp = _session.get(
            f"{GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": config.MAX_MARKETS_TO_SCAN,
                "order": "volumeNum",
                "ascending": "false",
            },
            timeout=10,
        )
        markets = resp.json()
        if isinstance(markets, dict) and "markets" in markets:
            markets = markets["markets"]
        for m in markets:
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
            liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            if vol >= config.MIN_VOLUME and liq >= config.MIN_LIQUIDITY:
                tokens = m.get("tokens") or m.get("outcomes") or []
                if len(tokens) >= 2:
                    return m
        return None
    except Exception as e:
        logging.error(f"Market fetch error: {e}")
        return None


class BalancedMM:
    def __init__(self):
        self.YES = None
        self.NO = None
        self.market = None
        self.pos_yes = 0.0
        self.pos_no = 0.0
        self.entry_price_yes = 0.0
        self.entry_price_no = 0.0
        self.usdc_balance = 0.0
        self.highest_yes_price = 0.0
        self.highest_no_price = 0.0

    def check_balance(self):
        self.usdc_balance = get_usdc_balance()
        # 0.0 означает что все методы получения баланса упали — не останавливаем бота
        if self.usdc_balance == 0.0:
            logging.warning("⚠️ Баланс недоступен, продолжаем с последним известным значением")
            return True
        if self.usdc_balance < config.MIN_BALANCE_USDC:
            logging.error(f"❌ Баланс слишком мал: ${self.usdc_balance:.2f} < ${config.MIN_BALANCE_USDC:.2f}")
            return False
        logging.info(f"💰 Баланс: ${self.usdc_balance:.2f}")
        return True

    def get_best_price(self, token_id, side):
        try:
            ob = client.get_order_book(token_id)
            if side == "buy" and ob.asks:
                return float(ob.asks[0].price)
            elif side == "sell" and ob.bids:
                return float(ob.bids[0].price)
        except Exception as e:
            logging.warning(f"Order book error: {e}")
        return None

    def market_make(self):
        if not self.YES or not self.NO:
            return

        for token_id, label in [(self.YES, "YES"), (self.NO, "NO")]:
            mid = self.get_best_price(token_id, "buy")
            if mid is None:
                continue

            if config.USE_DYNAMIC_PRICES:
                buy_price = round(mid - config.DYNAMIC_BUY_OFFSET, 4)
                sell_price = round(mid + config.DYNAMIC_SELL_OFFSET, 4)
            else:
                buy_price = config.FIXED_BUY_PRICE
                sell_price = config.FIXED_SELL_PRICE

            spread = sell_price - buy_price
            if spread < config.TARGET_PROFIT_SPREAD + config.POLYMARKET_FEE:
                logging.info(f"{label}: spread too tight ({spread:.4f}), skipping")
                continue

            try:
                client.create_order(OrderArgs(
                    token_id=token_id,
                    price=buy_price,
                    size=config.MAX_TRADE_SIZE,
                    side="BUY",
                    order_type=OrderType.GTC,
                ))
                logging.info(f"BUY {label} @ {buy_price}")
            except Exception as e:
                logging.warning(f"Buy order failed: {e}")

            try:
                client.create_order(OrderArgs(
                    token_id=token_id,
                    price=sell_price,
                    size=config.MAX_TRADE_SIZE,
                    side="SELL",
                    order_type=OrderType.GTC,
                ))
                logging.info(f"SELL {label} @ {sell_price}")
            except Exception as e:
                logging.warning(f"Sell order failed: {e}")

    def update_position(self):
        try:
            positions = client.get_positions()
            self.pos_yes = 0.0
            self.pos_no = 0.0
            for p in positions:
                if p.asset == self.YES:
                    self.pos_yes = float(p.size)
                    if self.pos_yes > 0 and self.entry_price_yes == 0:
                        self.entry_price_yes = float(p.avg_price or 0)
                elif p.asset == self.NO:
                    self.pos_no = float(p.size)
                    if self.pos_no > 0 and self.entry_price_no == 0:
                        self.entry_price_no = float(p.avg_price or 0)
        except Exception as e:
            logging.warning(f"Position update error: {e}")

    def check_stop_loss(self):
        if not config.USE_STOP_LOSS:
            return

        for token_id, label, pos, entry, attr_highest in [
            (self.YES, "YES", self.pos_yes, self.entry_price_yes, "highest_yes_price"),
            (self.NO, "NO", self.pos_no, self.entry_price_no, "highest_no_price"),
        ]:
            if pos <= 0 or entry <= 0 or not token_id:
                continue

            current_price = self.get_best_price(token_id, "sell")
            if current_price is None:
                continue

            if config.TRAILING_STOP:
                highest = getattr(self, attr_highest)
                if current_price > highest:
                    setattr(self, attr_highest, current_price)
                    highest = current_price
                stop_price = highest * (1 - config.TRAILING_STOP_PERCENT)
            else:
                stop_price = entry * (1 - config.STOP_LOSS_PERCENT)

            if current_price <= stop_price:
                logging.warning(f"Stop-loss triggered for {label} @ {current_price:.4f}")
                try:
                    client.create_order(OrderArgs(
                        token_id=token_id,
                        price=current_price,
                        size=pos,
                        side="SELL",
                        order_type=OrderType.FOK,
                    ))
                except Exception as e:
                    logging.error(f"Stop-loss order error: {e}")

    def rebalance(self):
        if self.pos_yes <= 0 or self.pos_no <= 0:
            return
        ratio = self.pos_yes / max(self.pos_no, 1e-9)
        if ratio > config.IMBALANCE_THRESHOLD:
            logging.info(f"Rebalancing YES->NO (ratio={ratio:.2f})")
            try:
                client.create_order(OrderArgs(
                    token_id=self.YES,
                    price=self.get_best_price(self.YES, "sell") or 0.5,
                    size=config.REBALANCE_SIZE,
                    side="SELL",
                    order_type=OrderType.GTC,
                ))
            except Exception as e:
                logging.warning(f"Rebalance sell error: {e}")
        elif ratio < 1 / config.IMBALANCE_THRESHOLD:
            logging.info(f"Rebalancing NO->YES (ratio={ratio:.2f})")
            try:
                client.create_order(OrderArgs(
                    token_id=self.NO,
                    price=self.get_best_price(self.NO, "sell") or 0.5,
                    size=config.REBALANCE_SIZE,
                    side="SELL",
                    order_type=OrderType.GTC,
                ))
            except Exception as e:
                logging.warning(f"Rebalance sell error: {e}")

    def update_market(self):
        market = get_active_market()
        if market:
            tokens = market.get("tokens") or market.get("outcomes") or []
            question = market.get("question") or market.get("title") or "Unknown Market"
            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), tokens[0] if tokens else None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), tokens[1] if len(tokens) > 1 else None)

            if yes_token and no_token:
                new_yes = yes_token.get("token_id") or yes_token.get("id")
                new_no = no_token.get("token_id") or no_token.get("id")
                if new_yes != self.YES or new_no != self.NO:
                    self.YES = new_yes
                    self.NO = new_no
                    self.pos_yes = 0
                    self.pos_no = 0
                    self.entry_price_yes = 0
                    self.entry_price_no = 0
                    self.highest_yes_price = 0
                    self.highest_no_price = 0
                self.market = question
                logging.info(f"Current market: {self.market}")
            else:
                self.YES = None
                self.NO = None
                self.market = None
                logging.warning("No valid tokens found in market")
        else:
            self.YES = None
            self.NO = None
            self.market = None
            logging.warning("No active market found, retrying in 5s...")

    def print_status(self):
        print(f"Market: {self.market}", flush=True)
        print(f"Balance: ${self.usdc_balance:.2f}", flush=True)
        print(f"YES: {self.pos_yes:.4f} (entry: {self.entry_price_yes:.4f})", flush=True)
        print(f"NO: {self.pos_no:.4f} (entry: {self.entry_price_no:.4f})", flush=True)

    def run(self):
        logging.info("Bot started")
        if not check_geoblock():
            logging.error("Остановка: IP заблокирован геоблоком Polymarket.")
            return

        # Тест доступности CLOB через прокси
        try:
            test = _session.get("https://clob.polymarket.com/data/order-book", timeout=10)
            logging.info(f"✅ CLOB доступен, статус: {test.status_code}")
        except Exception as e:
            logging.error(f"❌ CLOB через прокси недоступен: {e}. Проверьте прокси.")

        while True:
            try:
                if not self.check_balance():
                    logging.error("Stopping: insufficient balance")
                    time.sleep(30)
                    continue

                self.update_market()
                if not self.YES or not self.NO:
                    time.sleep(5)
                    continue

                self.update_position()
                self.check_stop_loss()
                self.print_status()

                client.cancel_all()
                self.market_make()
                self.rebalance()

                time.sleep(config.SLEEP_BETWEEN_CYCLES)

            except Exception as e:
                logging.error(f"ERROR: {e}")
                time.sleep(5)


if __name__ == "__main__":
    setup_approvals(w3, account)
    bot = BalancedMM()
    bot.run()
