import os
import json
import time
import logging
import requests
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv

# --- IMPORTS DA BIBLIOTECA OFICIAL DA POLYMARKET ---
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL

# ================= CONFIGURAÇÕES =================

# Carteira do Bot/Trader que você quer copiar
TARGET_WALLET = "0x63ce342161250d705dc0b16df89036c8e5f9ba9a"

# Modo de teste: True = Apenas simula / False = Gasta dinheiro real
DRY_RUN = False 

# Filtro de Mercados: Deixe vazio [] para copiar tudo.
# Exemplo: ["Bitcoin", "Ethereum", "XRP"]
WHITELIST_WORDS = ["Bitcoin", "Ethereum", "XRP"]

# --- GESTÃO DE BANCA ---
# Modo de Aposta: "FIXED" (valor fixo) ou "RATIO" (porcentagem da aposta do alvo)
BET_MODE = "FIXED" 

# Se FIXED, quanto apostar por trade (em USDC)?
FIXED_AMOUNT = Decimal("0.50") 

# Se RATIO, qual a proporção? (0.10 = 10% do valor do alvo)
COPY_RATIO = Decimal("0.10") 

# Limites de Segurança
MAX_USDC_PER_TRADE = Decimal("5.00")  # Nunca apostar mais que isso
MIN_USDC_PER_TRADE = Decimal("0.10")  # Mínimo aceito pela Polymarket (aprox)

# Configurações de Sistema
STATE_FILE = "polymarket_copy_state.json"
POLL_SECONDS = 1.0
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon Mainnet

# Carrega as variáveis do arquivo .env
load_dotenv()

# ================= FUNÇÕES AUXILIARES =================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_tx": [], "last_timestamp": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("seen_tx", [])
            data.setdefault("last_timestamp", 0)
            return data
    except:
        return {"seen_tx": [], "last_timestamp": 0}

def save_state(seen_tx_list, last_timestamp):
    # Mantém apenas os últimos 5000 IDs para não deixar o arquivo gigante
    if len(seen_tx_list) > 5000:
        seen_tx_list = seen_tx_list[-5000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen_tx": seen_tx_list, "last_timestamp": int(last_timestamp)}, f)

def iso_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")

def quantize_usdc(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

# --- API DE DADOS (MONITORAMENTO) ---
def fetch_trades(start_ts=None, end_ts=None, limit=50):
    url = "https://data-api.polymarket.com/activity"
    params = {
        "user": TARGET_WALLET,
        "limit": limit,
        "type": "TRADE",
        "sortBy": "TIMESTAMP",
        "sortDirection": "ASC",
    }
    if start_ts: params["start"] = int(start_ts)
    if end_ts: params["end"] = int(end_ts)

    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code == 429: return "RATE_LIMIT", []
        if r.status_code != 200: return f"HTTP_{r.status_code}", []
        data = r.json()
        return "OK", (data if isinstance(data, list) else [])
    except Exception as e:
        return f"ERR_{type(e).__name__}", []

# --- INTEGRAÇÃO COM GAMMA (Para descobrir o Token ID) ---
def get_market_token_id(condition_id: str, outcome_index: int) -> str:
    """Busca o Market ID e Token ID correto baseado na conditionId da aposta."""
    url = "https://gamma-api.polymarket.com/markets"
    try:
        r = requests.get(url, params={"condition_ids": condition_id}, timeout=5)
        r.raise_for_status()
        data = r.json()
        if not data: return None
        
        market = data[0]
        clob_ids = market.get("clobTokenIds")
        
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
            
        if clob_ids and len(clob_ids) > outcome_index:
            return str(clob_ids[outcome_index])
        return None
    except Exception as e:
        print(f"❌ Erro ao buscar Token ID: {e}")
        return None

# --- CLIENTE DE EXECUÇÃO (CLOB) ---
def init_clob_client():
    key = os.getenv("POLY_PRIVATE_KEY")
    funder = os.getenv("POLY_FUNDER")
    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1")) # Padrão 1

    if not key or not funder:
        raise ValueError("ERRO: Configure POLY_PRIVATE_KEY e POLY_FUNDER no arquivo .env")

    print("🔑 Iniciando cliente autenticado...")
    client = ClobClient(
        host=CLOB_HOST,
        key=key,
        chain_id=CHAIN_ID,
        signature_type=sig_type,
        funder=funder
    )
    # Cria as credenciais de API se necessário
    try:
        creds = client.derive_api_key()
        client.set_api_creds(creds)
    except Exception as e:
        print(f"⚠️ Aviso na criação de credenciais (pode já estar ok): {e}")
    
    return client

def get_usdc_balance(client: ClobClient) -> Decimal:
    """Verifica saldo de COLATERAL (USDC) na Polymarket"""
    try:
        resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        raw_bal = Decimal(resp['balance']) # Vem em wei (6 casas decimais para USDC na Polygon)
        return raw_bal / Decimal("1000000")
    except Exception as e:
        print(f"❌ Erro ao checar saldo: {e}")
        return Decimal("0")

def place_order(client: ClobClient, token_id: str, side: str, price: float, size_usdc: Decimal):
    """Envia uma Limit Order (GTC) no preço que o alvo pagou"""
    
    # Converter USDC para quantidade de shares
    # Size (shares) = USDC / Preço
    if price <= 0: 
        print("❌ Preço inválido.")
        return None

    size_shares = float(size_usdc) / price
    size_shares = round(size_shares, 2) # Arredonda para 2 casas

    # Define BUY ou SELL baseado na string
    order_side = BUY if side.upper() == "BUY" else SELL

    print(f"🔄 Preparando ordem: {side} {size_shares} shares @ ${price} (Total: ${size_usdc})")

    try:
        order_args = OrderArgs(
            price=price,
            size=size_shares,
            side=order_side,
            token_id=token_id
        )
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.GTC)
        return resp
    except Exception as e:
        print(f"❌ Falha ao enviar ordem: {e}")
        return None

# ================= LOOP PRINCIPAL =================

def main():
    print("🚀 BOT DE COPY TRADING INICIADO")
    print(f"🎯 Alvo: {TARGET_WALLET}")
    print(f"🧪 DRY_RUN (Simulação): {DRY_RUN}")
    
    client = None
    if not DRY_RUN:
        try:
            client = init_clob_client()
            bal = get_usdc_balance(client)
            print(f"💰 Seu Saldo USDC: ${bal:.2f}")
        except Exception as e:
            print(f"🛑 Erro fatal na autenticação: {e}")
            return

    state = load_state()
    seen_tx = set(state.get("seen_tx", []))
    last_timestamp = state.get("last_timestamp", int(time.time()))

    print("⏳ Sincronizando estado inicial...")
    # Faz uma busca inicial só para marcar o que já existe como visto
    _, initial_items = fetch_trades(limit=50)
    for item in initial_items:
        tx = item.get("transactionHash")
        if tx: seen_tx.add(tx)
    
    # Atualiza timestamp para AGORA, para não pegar trades velhos
    last_timestamp = int(time.time())
    save_state(list(seen_tx), last_timestamp)
    print("✅ Sincronizado. Aguardando novas apostas...\n")

    while True:
        try:
            current_time = int(time.time())
            status, items = fetch_trades(start_ts=last_timestamp + 1, end_ts=current_time, limit=20)

            if status == "RATE_LIMIT":
                time.sleep(5)
                continue
            
            if status != "OK":
                print(f"⚠️ API Status: {status}")
                time.sleep(POLL_SECONDS)
                continue

            if not items:
                # print(".", end="", flush=True) # Feedback visual opcional
                time.sleep(POLL_SECONDS)
                continue

            for item in items:
                tx = item.get("transactionHash")
                ts = int(item.get("timestamp", 0))
                
                # Ignora se já vimos ou se é timestamp antigo
                if not tx or tx in seen_tx: continue
                
                last_timestamp = max(last_timestamp, ts)
                seen_tx.add(tx) # Marca como visto imediatamente

                # Dados da aposta do Alvo
                side = item.get("side") # BUY ou SELL
                target_usdc = Decimal(str(item.get("usdcSize", "0")))
                price = float(item.get("price", "0"))
                outcome = item.get("outcome")
                title = item.get("title", "Unknown")
                
                # --- FILTRO 1: Whitelist de Palavras ---
                if WHITELIST_WORDS:
                    found = False
                    for word in WHITELIST_WORDS:
                        if word.lower() in title.lower():
                            found = True
                            break
                    if not found:
                        print(f"🚫 Ignorado (Filtro): {title[:30]}...")
                        continue

                # --- FILTRO 2: Apenas Compras (Opcional, mas recomendado para copy) ---
                if side != "BUY":
                    print(f"🚫 Ignorado (Venda): {side} em {title[:20]}")
                    continue

                print(f"\n🔔 NOVA APOSTA DETECTADA!")
                print(f"📅 {iso_time(ts)} | {side} {outcome} | Alvo: ${target_usdc:.2f} | Preço: {price}")
                print(f"📜 Mercado: {title}")

                if DRY_RUN:
                    print("🧪 Simulação: Trade teria sido executado.")
                    continue

                # --- LÓGICA DE EXECUÇÃO REAL ---
                if client:
                    # 1. Verifica saldo atual
                    my_balance = get_usdc_balance(client)
                    
                    # 2. Calcula quanto apostar
                    my_wager = Decimal("0")
                    if BET_MODE == "FIXED":
                        my_wager = FIXED_AMOUNT
                    else:
                        my_wager = target_usdc * COPY_RATIO
                    
                    # Travas de segurança
                    my_wager = min(my_wager, MAX_USDC_PER_TRADE)
                    my_wager = quantize_usdc(my_wager)

                    if my_wager > my_balance:
                        print(f"⚠️ Saldo insuficiente (${my_balance}) para aposta de ${my_wager}")
                        continue
                    
                    if my_wager < MIN_USDC_PER_TRADE:
                        print(f"⚠️ Valor muito baixo (${my_wager}). Mínimo é ${MIN_USDC_PER_TRADE}")
                        continue

                    # 3. Busca Token ID
                    condition_id = item.get("conditionId")
                    outcome_idx = item.get("outcomeIndex")
                    
                    token_id = get_market_token_id(condition_id, int(outcome_idx))
                    
                    if token_id:
                        # 4. Envia a Ordem
                        resp = place_order(client, token_id, side, price, my_wager)
                        if resp and resp.get("orderID"):
                            print(f"✅ ORDEM SUCESSO! ID: {resp.get('orderID')}")
                        else:
                            print(f"❌ Falha no envio: {resp}")
                    else:
                        print("❌ Não foi possível encontrar o Token ID para este mercado.")

            # Salva estado após processar o lote
            save_state(list(seen_tx), last_timestamp)

        except Exception as e:
            print(f"Erro no loop principal: {e}")
            time.sleep(2)
        
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()