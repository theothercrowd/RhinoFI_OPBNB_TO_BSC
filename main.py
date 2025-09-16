import time
import random
import requests
from web3 import Web3
from eth_account import Account

# ----------------------- Configuration ----------------------- #
RPCS = [
    "https://1rpc.io/opbnb",
    "https://opbnb-rpc.publicnode.com",
]

MIN_SLEEP = 5          # seconds
MAX_SLEEP = 10         # seconds
BRIDGE_PERCENTAGE = 99  # % of wallet balance to bridge

OPBNB_BRIDGE_CONTRACT = Web3.to_checksum_address(
    "0x2b4553122d960ca98075028d68735cc6b15deeb5"
)

# Rhino.fi API
RHINO_API_BASE = "https://api.rhino.fi"
API_KEY = "SECRET-XXXXXXXXXX" # take your secret API key from https://developers.rhino.fi/
TOKEN_SYMBOL = "BNB"

# ABI: depositNativeWithId(uint256 commitmentId)
DEPOSIT_ABI = [{
    "inputs": [{
        "internalType": "uint256",
        "name": "commitmentId",
        "type": "uint256"
    }],
    "name": "depositNativeWithId",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function"
}]

WALLETS_FILE = "wallets.txt"
# ------------------------------------------------------------- #

# ------------------------ Helpers ---------------------------- #
def load_wallets(path=WALLETS_FILE):
    with open(path) as f:
        return [w.strip() for w in f if w.strip()]

def get_web3():
    for rpc in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc))
            if w3.is_connected():
                print(f"Connected to {rpc}")
                return w3
        except Exception as e:
            print(f"{rpc} failed: {e}")
    raise RuntimeError("All RPCs failed")

def get_jwt():
    url = f"{RHINO_API_BASE}/authentication/auth/apiKey"
    r = requests.post(url, json={"apiKey": API_KEY},
                      headers={"Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"Auth failed: {r.text}")
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Auth error: {data['error']}")
    return data["jwt"]

def get_bridge_configs():
    r = requests.get(f"{RHINO_API_BASE}/bridge/configs")
    if r.status_code != 200:
        raise RuntimeError(f"Config fetch failed: {r.text}")
    return r.json()

def find_chain_names(cfg):
    chain_in = chain_out = None
    for name in cfg.keys():
        lname = name.lower()
        if "opbnb" in lname:
            chain_in = name
        elif "binance" in lname:
            chain_out = name
    return chain_in, chain_out

def floor_to_8_decimals(wei_amt: int) -> int:
    quantum = 10 ** 10            # 0.00000001 BNB in wei
    return (wei_amt // quantum) * quantum
# ------------------------------------------------------------- #

# -------------------- Rhino.fi wrappers ---------------------- #
def get_user_quote(jwt, chain_in, chain_out, depositor, recipient, amount_str):
    url = f"{RHINO_API_BASE}/bridge/quote/user"
    payload = {
        "token": TOKEN_SYMBOL,
        "chainIn": chain_in,
        "chainOut": chain_out,
        "amount": amount_str,
        "depositor": depositor,
        "recipient": recipient,
        "mode": "pay",
        "amountNative": "0",
    }
    r = requests.post(url, json=payload,
                      headers={"Authorization": jwt, "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"Quote error {r.status_code}: {r.text}")
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Quote error: {data['error']}")
    return data["quoteId"]

def convert_quote_id_to_int(quote_id: str) -> int:
    """Safely convert Rhino.fi quote-ID (decimal or hex) to int."""
    quote_id = quote_id.strip()
    if quote_id.startswith("0x"):
        return int(quote_id, 16)
    try:
        return int(quote_id, 16)
    except ValueError:
        return int(quote_id, 10)

def commit_quote(jwt, quote_id):
    url = f"{RHINO_API_BASE}/bridge/quote/commit/{quote_id}"
    r = requests.post(url,
                      headers={"Authorization": jwt, "Content-Type": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"Commit error {r.status_code}: {r.text}")
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Commit error: {data['error']}")
    return data
# ------------------------------------------------------------- #

# ---------------------- Bridge logic ------------------------- #
def bridge_mode():
    jwt = get_jwt()
    cfg = get_bridge_configs()
    chain_in, chain_out = find_chain_names(cfg)
    if not (chain_in and chain_out):
        raise RuntimeError("Chain names not found in configs")

    print(f"Chains  →  {chain_in}  ➜  {chain_out}")

    w3 = get_web3()
    contract = w3.eth.contract(address=OPBNB_BRIDGE_CONTRACT, abi=DEPOSIT_ABI)
    wallets = load_wallets()

    for pk in wallets:
        try:
            acct = Account.from_key(pk)
            addr = acct.address
            bal_wei = w3.eth.get_balance(addr)

            amt_wei = int(bal_wei * BRIDGE_PERCENTAGE / 100)
            amt_wei = floor_to_8_decimals(amt_wei)     # enforce 8-decimal precision

            if amt_wei == 0:
                print(f"{addr}: balance too low, skipped")
                continue

            amt_bnb = w3.from_wei(amt_wei, "ether")
            amt_str = f"{amt_bnb:.8f}"

            print(f"\nWallet   {addr}")
            print(f"Balance: {w3.from_wei(bal_wei, 'ether')} BNB")
            print(f"Bridging {amt_str} BNB")

            quote_id = get_user_quote(jwt, chain_in, chain_out, addr, addr, amt_str)
            commit_quote(jwt, quote_id)

            commit_id_int = convert_quote_id_to_int(quote_id)

            tx = contract.functions.depositNativeWithId(commit_id_int).build_transaction({
                "from": addr,
                "value": amt_wei,
                "nonce": w3.eth.get_transaction_count(addr),
                "gas": 300_000,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=pk)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            print(f"Tx sent  → {w3.to_hex(tx_hash)}")
            print(f"Quote ID → {quote_id}")

            time.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))
        except Exception as e:
            print(f"Error ({addr}): {e}")
# ------------------------------------------------------------- #

# ---------------------- Main entry --------------------------- #
if __name__ == "__main__":
    bridge_mode()
