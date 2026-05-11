import os
import io
import csv
import json
import re
import time
import threading
import requests
import pdfrw
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response

_state_lock = threading.RLock()

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['DATA_FILE'] = 'data/state.json'

DEFAULT_TAX_YEAR = 2025

def load_state():
    """Load state from state.json if it exists."""
    if os.path.exists(app.config['DATA_FILE']):
        with open(app.config['DATA_FILE'], 'r') as f:
            return json.load(f)
    return None

def save_state(data):
    """Save state atomically: write to a temp file then rename."""
    path = app.config['DATA_FILE']
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

@app.route('/backup', methods=['POST'])
def backup():
    """Create a timestamped backup of state.json."""
    import shutil
    src = app.config['DATA_FILE']
    if not os.path.exists(src):
        return jsonify({'ok': False, 'message': 'No state file to backup'})
    timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    backup_dir = os.path.join('data', 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, f'state_{timestamp}.json')
    shutil.copy2(src, dst)
    print(f'[Backup] Created {dst}')
    return jsonify({'ok': True, 'file': dst})

def ensure_state():
    """Ensure state exists with all required keys."""
    state = load_state()
    if state is None:
        state = {}
    if 'tax_year' not in state:
        state['tax_year'] = DEFAULT_TAX_YEAR
    if 'wallets' not in state:
        state['wallets'] = []
    if 'transactions' not in state:
        state['transactions'] = {}
    if 'classifications' not in state:
        state['classifications'] = {}
    if 'known_addresses' not in state:
        state['known_addresses'] = []  # [{'label': 'Coinbase', 'addresses': ['0x...', '0x...']}]
    if 'cost_basis_method' not in state:
        state['cost_basis_method'] = 'LIFO'
    if 'price_cache' not in state:
        state['price_cache'] = {}
    return state

def get_tax_year():
    state = load_state()
    if state and 'tax_year' in state:
        return state['tax_year']
    return DEFAULT_TAX_YEAR

def get_configured_addresses(state):
    """Get all configured wallet addresses (lowercased)."""
    return set(w['address'].lower() for w in state.get('wallets', []) if w.get('address'))

def get_all_known_addresses(state):
    """Get a dict mapping lowercased addresses to labels (own wallets + known addresses)."""
    addr_map = {}
    for w in state.get('wallets', []):
        addr_map[w['address'].lower()] = w['label']
    for ka in state.get('known_addresses', []):
        for addr in ka.get('addresses', []):
            addr_map[addr.lower()] = ka['label']
    return addr_map

def parse_csv(filepath):
    """Parse a BitcoinTax opening positions CSV."""
    positions = []
    with open(filepath, 'r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            cleaned = {k.strip(): v.strip() if v else '' for k, v in row.items()}
            position = {
                'date': cleaned.get('Date', ''),
                'symbol': cleaned.get('Symbol', ''),
                'account': cleaned.get('Account', ''),
                'volume': cleaned.get('Volume', ''),
                'price': cleaned.get('Price', ''),
                'currency': cleaned.get('Currency', ''),
                'fee': cleaned.get('Fee', ''),
                'fee_currency': cleaned.get('FeeCurrency', ''),
                'total': cleaned.get('Total', ''),
                'memo': cleaned.get('Memo', ''),
            }
            positions.append(position)
    return positions

_HEX_ADDR_RE = re.compile(r'^(0x)?[0-9a-fA-F]{40}$')


def _normalize_hex(s):
    """Return 0x-prefixed lowercase hex if s looks like an Ethereum address, else None."""
    if not s:
        return None
    s = s.strip()
    if not _HEX_ADDR_RE.match(s):
        return None
    return ('0x' + s.lower()) if not s.lower().startswith('0x') else s.lower()


def _strip_dollar(s):
    """Parse strings like '$1.00', '$88,364.245' to float; '' returns 0.0."""
    if not s:
        return 0.0
    s = str(s).replace('$', '').replace(',', '').strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def detect_csv_format(filepath):
    """Sniff the first few lines to identify the CSV format.
    Returns 'coinbase', 'generic', or 'chain_glance'.
    """
    with open(filepath, 'r', encoding='utf-8-sig', errors='replace') as f:
        lines = [next(f, '').strip() for _ in range(3)]
    if len(lines) >= 2 and lines[1].strip() == 'Transactions':
        return 'coinbase'
    # Generic format: header row begins with the expected prefix (allow quoted columns)
    first = lines[0].lower() if lines else ''
    normalized = first.replace('"', '').replace("'", '')
    if normalized.startswith('tx_hash,date,type,direction,token,amount'):
        return 'generic'
    return 'chain_glance'


COINBASE_REWARD_SENDERS = {'Coinbase Card Rewards', 'Coinbase'}


def parse_coinbase_csv(filepath):
    """Parse a Coinbase 'Transactions' CSV into the system's transaction schema.
    Auto-classification hints are attached as `_auto_cls` (item_index -> classification).
    """
    transactions = []
    with open(filepath, 'r', encoding='utf-8-sig', errors='replace') as f:
        for _ in range(3):
            next(f, None)
        reader = csv.DictReader(f)
        for row in reader:
            cleaned = {(k or '').strip(): (v or '').strip() for k, v in row.items()}
            ttype = cleaned.get('Transaction Type', '')
            asset = cleaned.get('Asset', '')
            ts = cleaned.get('Timestamp', '')
            try:
                qty = float(cleaned.get('Quantity Transacted', '0') or 0)
            except (ValueError, TypeError):
                qty = 0
            subtotal = _strip_dollar(cleaned.get('Subtotal', ''))
            total = _strip_dollar(cleaned.get('Total (inclusive of fees and/or spread)', ''))
            fee = _strip_dollar(cleaned.get('Fees and/or Spread', ''))
            sender_raw = cleaned.get('Sender Address', '')
            recipient_raw = cleaned.get('Recipient Address', '')
            sender_hex = _normalize_hex(sender_raw)
            recipient_hex = _normalize_hex(recipient_raw)

            tx = None
            auto_cls = {}

            if ttype == 'Card Spend':
                continue  # skip per design — USDC disposals at $0 g/l not on 8949

            if ttype == 'Deposit':
                continue  # USD bank deposit; not a crypto event

            if ttype == 'Receive':
                # Income reward stream: sender is "Coinbase Card Rewards" or "Coinbase" or Reward Income elsewhere
                is_reward = sender_raw in COINBASE_REWARD_SENDERS
                amount = abs(qty)
                if amount == 0 or not asset:
                    continue
                tx = _make_cb_tx('RECEIVE', ts, sender_hex or '', '', asset, amount, total or subtotal)
                if is_reward:
                    auto_cls[0] = 'Income'

            elif ttype == 'Reward Income':
                amount = abs(qty)
                if amount == 0 or not asset:
                    continue
                tx = _make_cb_tx('RECEIVE', ts, '', '', asset, amount, total or subtotal)
                auto_cls[0] = 'Income'

            elif ttype == 'Send':
                amount = abs(qty)
                if amount == 0 or not asset:
                    continue
                tx = _make_cb_tx('SEND', ts, '', recipient_hex or '', asset, amount, total or subtotal)

            elif ttype == 'Buy':
                # USD -> crypto. proceeds_to_basis = total (subtotal + fee paid)
                amount = abs(qty)
                if amount == 0 or not asset:
                    continue
                cost = total if total else (subtotal + fee)
                tx = _make_cb_trade(ts, 'USD', cost, asset, amount, cost)

            elif ttype == 'Advanced Trade Sell':
                # crypto -> USD. proceeds_after_fees = total (subtotal - fee)
                amount = abs(qty)
                if amount == 0 or not asset:
                    continue
                proceeds = total if total else max(subtotal - fee, 0)
                tx = _make_cb_trade(ts, asset, amount, 'USD', proceeds, proceeds)

            elif ttype == 'Asset Migration':
                # 1:1 token rename — non-taxable. Mark sent side with 'Migration' classification.
                # Coinbase emits this as one row per side (negative qty for old, positive for new).
                # Treat the negative-qty row as the sent side referencing old asset; recv side is the new asset.
                # Without both sides in one row, we lose pairing — but Coinbase also pairs them via timestamp.
                # Simplest: emit the negative-qty row as a TRADE with sent=old_asset and skip the positive-qty row.
                # The actual paired asset name (POL, etc.) we can't know from one row alone, so we use Notes if present.
                if qty < 0:
                    sent_amount = abs(qty)
                    notes = cleaned.get('Notes', '')
                    new_asset = _extract_migration_target(notes) or asset
                    tx = _make_cb_trade(ts, asset, sent_amount, new_asset, sent_amount, 0)
                    auto_cls[0] = 'Migration'
                else:
                    continue  # skip the positive-qty side; pairing handled by sent side

            elif ttype == 'Credit':
                amount = abs(qty)
                if amount == 0 or not asset:
                    continue
                tx = _make_cb_tx('RECEIVE', ts, '', '', asset, amount, total or subtotal)
                # Credits are returns of basis — leave unclassified, user reviews

            else:
                continue  # unknown type — skip

            if tx is not None:
                if auto_cls:
                    tx['_auto_cls'] = auto_cls
                transactions.append(tx)

    transactions.sort(key=lambda t: t.get('date', ''))
    return transactions


def _make_cb_tx(tx_type, ts, sender, recipient, asset, amount, usd_value):
    """Build a non-trade Coinbase transaction in the system schema."""
    item = {
        'amount': amount,
        'token': asset,
        'token_name': asset,
        'usd_value': str(usd_value) if usd_value else '',
        'pretty_usd': f"${usd_value:,.2f}" if usd_value else '',
        'is_nft': False,
        'token_id': '',
        'contract_address': '',
    }
    return {
        'date': ts,
        'account': '',
        'blockchain': '',
        'type': tx_type,
        'volume': str(amount),
        'symbol': asset,
        'value': str(usd_value) if usd_value else '',
        'currency': 'USD',
        'fee': '',
        'fee_currency': '',
        'tx_hash': '',
        'sender': sender,
        'recipient': recipient,
        'url': '',
        'unsuccessful': '',
        'spam': '',
        'ledgers': '',
        'summary': '',
        'notes': '',
        'parsed_details': {
            'sent': [item] if tx_type == 'SEND' else [],
            'received': [item] if tx_type == 'RECEIVE' else [],
            'fees': [],
        },
    }


def _make_cb_trade(ts, sent_asset, sent_amount, recv_asset, recv_amount, usd_value):
    """Build a Coinbase TRADE in the system schema."""
    sent_item = {
        'amount': sent_amount, 'token': sent_asset, 'token_name': sent_asset,
        'usd_value': str(usd_value) if usd_value else '',
        'pretty_usd': f"${usd_value:,.2f}" if usd_value else '',
        'is_nft': False, 'token_id': '', 'contract_address': '',
    }
    recv_item = {
        'amount': recv_amount, 'token': recv_asset, 'token_name': recv_asset,
        'usd_value': str(usd_value) if usd_value else '',
        'pretty_usd': f"${usd_value:,.2f}" if usd_value else '',
        'is_nft': False, 'token_id': '', 'contract_address': '',
    }
    return {
        'date': ts, 'account': '', 'blockchain': '',
        'type': 'TRADE', 'volume': str(sent_amount), 'symbol': sent_asset,
        'value': str(usd_value) if usd_value else '', 'currency': 'USD',
        'fee': '', 'fee_currency': '',
        'tx_hash': '', 'sender': '', 'recipient': '', 'url': '',
        'unsuccessful': '', 'spam': '', 'ledgers': '', 'summary': '', 'notes': '',
        'parsed_details': {
            'sent': [sent_item], 'received': [recv_item], 'fees': [],
        },
    }


def _extract_migration_target(notes):
    """Best-effort: pull the new ticker out of an Asset Migration Notes field.
    e.g., 'Migrated MATIC to POL' -> 'POL'. Returns None if not found.
    """
    if not notes:
        return None
    m = re.search(r'\bto\s+([A-Z0-9]{2,10})\b', notes)
    return m.group(1) if m else None


_EXPLORER_BY_CHAIN = {
    'base': 'https://basescan.org/tx/',
    'eth': 'https://etherscan.io/tx/',
    'ethereum': 'https://etherscan.io/tx/',
    'polygon': 'https://polygonscan.com/tx/',
    'matic': 'https://polygonscan.com/tx/',
    'optimism': 'https://optimistic.etherscan.io/tx/',
    'arbitrum': 'https://arbiscan.io/tx/',
}


def _explorer_url(blockchain, tx_hash):
    base = _EXPLORER_BY_CHAIN.get((blockchain or '').lower())
    return f"{base}{tx_hash}" if base and tx_hash else ''


def parse_generic_csv(filepath):
    """Parse the system's generic per-leg CSV (one row per leg, grouped by tx_hash).

    Header (exact prefix detected by detect_csv_format):
      tx_hash,date,type,direction,token,amount,usd_value,sender,recipient,
      blockchain,is_nft,token_id,contract_address
    """
    groups = {}  # tx_hash -> {meta, sent[], received[], fees[]}
    with open(filepath, 'r', newline='', encoding='utf-8-sig', errors='replace') as f:
        reader = csv.DictReader(f)
        for row in reader:
            cleaned = {(k or '').strip(): (v or '').strip() for k, v in row.items()}
            tx_hash = cleaned.get('tx_hash', '')
            if not tx_hash:
                continue
            direction = cleaned.get('direction', '').lower()
            if direction not in ('sent', 'received', 'fee'):
                continue
            try:
                amount = abs(float(cleaned.get('amount', '') or 0))
            except (ValueError, TypeError):
                continue
            if amount <= 0:
                continue
            token = cleaned.get('token', '')
            if not token:
                continue
            usd_raw = cleaned.get('usd_value', '')
            try:
                usd_val = float(usd_raw) if usd_raw else 0.0
            except (ValueError, TypeError):
                usd_val = 0.0
            is_nft = cleaned.get('is_nft', '').lower() in ('true', '1', 'yes')
            item = {
                'amount': amount,
                'token': token,
                'token_name': token,
                'usd_value': str(usd_val) if usd_val else '',
                'pretty_usd': f"${usd_val:,.2f}" if usd_val else '',
                'is_nft': is_nft,
                'token_id': cleaned.get('token_id', ''),
                'contract_address': cleaned.get('contract_address', '').lower(),
            }

            if tx_hash not in groups:
                sender_norm = _normalize_hex(cleaned.get('sender', '')) or ''
                recipient_norm = _normalize_hex(cleaned.get('recipient', '')) or ''
                groups[tx_hash] = {
                    'tx_hash': tx_hash,
                    'date': cleaned.get('date', ''),
                    'type': cleaned.get('type', '').upper(),
                    'sender': sender_norm,
                    'recipient': recipient_norm,
                    'blockchain': cleaned.get('blockchain', ''),
                    'sent': [],
                    'received': [],
                    'fees': [],
                }
            bucket = {'sent': 'sent', 'received': 'received', 'fee': 'fees'}[direction]
            groups[tx_hash][bucket].append(item)

    transactions = []
    for h, g in groups.items():
        # Compute total USD value for the row (sum of received-side items, fallback to sent)
        try:
            total_usd = sum(float(it['usd_value']) for it in g['received'] if it.get('usd_value'))
            if total_usd == 0:
                total_usd = sum(float(it['usd_value']) for it in g['sent'] if it.get('usd_value'))
        except (ValueError, TypeError):
            total_usd = 0.0
        tx = {
            'date': g['date'],
            'account': '',
            'blockchain': g['blockchain'],
            'type': g['type'],
            'volume': '',
            'symbol': '',
            'value': str(total_usd) if total_usd else '',
            'currency': 'USD',
            'fee': '',
            'fee_currency': '',
            'tx_hash': h,
            'sender': g['sender'],
            'recipient': g['recipient'],
            'url': _explorer_url(g['blockchain'], h),
            'unsuccessful': '',
            'spam': '',
            'ledgers': '',
            'summary': '',
            'notes': '',
            'parsed_details': {
                'sent': g['sent'],
                'received': g['received'],
                'fees': g['fees'],
            },
        }
        transactions.append(tx)

    transactions.sort(key=lambda t: t.get('date', ''))
    return transactions


def parse_wallet_csv(filepath):
    """Parse a Chain Glance wallet CSV."""
    transactions = []
    with open(filepath, 'r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            cleaned = {k.strip(): v.strip() if v else '' for k, v in row.items()}
            blockchain = cleaned.get('Blockchain', '')
            # Filter: only ETH, Base, and Polygon chains
            if blockchain.lower() not in ('eth', 'base', 'ethereum', 'matic', 'polygon'):
                continue
            # Filter out unsuccessful/reverted transactions
            if cleaned.get('Unsuccessful', '').lower() in ('true', '1', 'yes'):
                continue
            # Filter out non-taxable transaction types
            tx_type = cleaned.get('Type', '').upper()
            if tx_type in ('APPROVE', 'EXECUTE'):
                continue
            # BURN with proceeds (e.g., Polymarket position exit) is functionally a TRADE.
            # parse_ledgers categorizes legs by amount sign, so renaming is safe even
            # for plain burns (received side just stays empty → zero proceeds).
            type_for_storage = 'TRADE' if tx_type == 'BURN' else cleaned.get('Type', '')
            tx = {
                'date': cleaned.get('Date', ''),
                'account': cleaned.get('Account', ''),
                'blockchain': blockchain,
                'type': type_for_storage,
                'volume': cleaned.get('Volume', ''),
                'symbol': cleaned.get('Symbol', ''),
                'value': cleaned.get('Value', ''),
                'currency': cleaned.get('Currency', ''),
                'fee': cleaned.get('Fee', ''),
                'fee_currency': cleaned.get('FeeCurrency', ''),
                'tx_hash': cleaned.get('TxHash', ''),
                'sender': cleaned.get('Sender', ''),
                'recipient': cleaned.get('Recipient', ''),
                'url': cleaned.get('Url', ''),
                'unsuccessful': cleaned.get('Unsuccessful', ''),
                'spam': cleaned.get('Spam', ''),
                'ledgers': cleaned.get('Ledgers', ''),
                'summary': cleaned.get('Summary', ''),
                'notes': cleaned.get('Notes', ''),
            }
            # Parse ledgers JSON
            tx['parsed_details'] = parse_ledgers(tx)
            transactions.append(tx)
    return transactions

def parse_ledgers(tx):
    """Parse the Ledgers JSON column to extract human-readable details."""
    details = {}
    ledgers_raw = tx.get('ledgers', '')
    tx_type = tx.get('type', '').upper()

    if ledgers_raw:
        try:
            ledgers = json.loads(ledgers_raw)
            sent_items = []
            received_items = []
            fee_items = []

            if isinstance(ledgers, list):
                for entry in ledgers:
                    amount = entry.get('amount', '0')
                    currency = entry.get('currency', '')
                    token_name = entry.get('txInfo', {}).get('name', currency) if isinstance(entry.get('txInfo'), dict) else currency
                    native_amount = entry.get('nativeamount', entry.get('nativeAmount', ''))
                    pretty = entry.get('prettyNativeAmount', '')
                    is_fee = entry.get('isFee', False)

                    is_nft = entry.get('isNft', False)
                    token_id = entry.get('tokenId', '')
                    tx_info = entry.get('txInfo', {}) if isinstance(entry.get('txInfo'), dict) else {}

                    # For NFTs, use the name from txInfo and include tokenId
                    display_token = currency
                    if is_nft and token_name and token_name != currency:
                        display_token = token_name
                    if is_nft and token_id and f"#{token_id}" not in display_token:
                        # Truncate very long token IDs
                        tid = token_id if len(str(token_id)) <= 8 else str(token_id)[:6] + '...'
                        display_token = f"{display_token} #{tid}"

                    contract_address = entry.get('contractAddress', '') or (tx_info.get('contractAddress', '') if tx_info else '')

                    item = {
                        'amount': amount,
                        'token': display_token if is_nft else currency,
                        'token_name': token_name,
                        'usd_value': native_amount,
                        'pretty_usd': pretty,
                        'is_nft': is_nft,
                        'token_id': token_id,
                        'contract_address': contract_address,
                    }

                    try:
                        amt = float(amount)
                    except (ValueError, TypeError):
                        amt = 0

                    if is_fee:
                        # Pull fee USD from Summary if not in ledger entry
                        if not native_amount and not pretty:
                            try:
                                summary = json.loads(tx.get('summary', '{}'))
                                fee_info = summary.get('fee', {})
                                if fee_info:
                                    fee_usd = fee_info.get('nativeAmount', 0)
                                    fee_pretty = fee_info.get('prettyNativeAmount', '')
                                    item['usd_value'] = str(abs(float(fee_usd))) if fee_usd else ''
                                    item['pretty_usd'] = fee_pretty.replace('-', '') if fee_pretty else ''
                            except (json.JSONDecodeError, TypeError, ValueError):
                                pass
                        fee_items.append(item)
                    elif amt < 0:
                        item['amount'] = abs(amt)
                        sent_items.append(item)
                    elif amt > 0:
                        received_items.append(item)

            details['sent'] = sent_items
            details['received'] = received_items
            details['fees'] = fee_items
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    # Fallback to summary/volume if ledgers didn't parse
    if not details.get('sent') and not details.get('received'):
        if tx_type == 'SEND':
            details['sent'] = [{'amount': tx.get('volume', ''), 'token': tx.get('symbol', ''), 'usd_value': tx.get('value', '')}]
        elif tx_type == 'RECEIVE':
            details['received'] = [{'amount': tx.get('volume', ''), 'token': tx.get('symbol', ''), 'usd_value': tx.get('value', '')}]
        elif tx_type == 'TRADE':
            details['sent'] = [{'amount': tx.get('volume', ''), 'token': tx.get('symbol', ''), 'usd_value': tx.get('value', '')}]

    if 'sent' not in details:
        details['sent'] = []
    if 'received' not in details:
        details['received'] = []
    if 'fees' not in details:
        details['fees'] = []

    return details

def format_details(tx, addr_map=None):
    """Format parsed details into a readable string."""
    details = tx.get('parsed_details', {})
    tx_type = tx.get('type', '').upper()
    parts = []
    if addr_map is None:
        addr_map = {}

    if tx_type == 'TRADE':
        sent = details.get('sent', [])
        received = details.get('received', [])
        if sent:
            sold_parts = [f"{format_amount(s['amount'])} {s['token']}" for s in sent]
            parts.append(f"Sold {', '.join(sold_parts)}")
        if received:
            got_parts = [f"{format_amount(r['amount'])} {r['token']}" for r in received]
            parts.append(f"Got {', '.join(got_parts)}")
    elif tx_type == 'SEND':
        sent = details.get('sent', [])
        if sent:
            s = sent[0]
            recipient = tx.get('recipient', '')
            if recipient and recipient.lower() in addr_map:
                addr = f" \u2192 {addr_map[recipient.lower()]}"
            elif recipient:
                addr = f" \u2192 {recipient[:6]}...{recipient[-4:]}"
            else:
                addr = ''
            parts.append(f"Sent {format_amount(s['amount'])} {s['token']}{addr}")
    elif tx_type == 'RECEIVE':
        received = details.get('received', [])
        if received:
            recv_parts = [f"{format_amount(r['amount'])} {r['token']}" for r in received]
            sender = tx.get('sender', '')
            if sender and sender.lower() in addr_map:
                addr = f" from {addr_map[sender.lower()]}"
            elif sender:
                addr = f" from {sender[:6]}...{sender[-4:]}"
            else:
                addr = ''
            parts.append(f"Received {', '.join(recv_parts)}{addr}")
    else:
        # MINT, APPROVE, EXECUTE
        summary = tx.get('summary', '')
        if summary:
            parts.append(summary)
        else:
            sent = details.get('sent', [])
            received = details.get('received', [])
            if sent:
                parts.append(f"Sent {format_amount(sent[0]['amount'])} {sent[0]['token']}")
            if received:
                parts.append(f"Received {format_amount(received[0]['amount'])} {received[0]['token']}")

    return ' / '.join(parts) if parts else tx.get('summary', '-')

def check_needs_classification(tx, known_addresses, known_addr_map=None):
    """Check if a transaction needs user classification."""
    all_known = set(known_addresses)
    if known_addr_map:
        all_known.update(known_addr_map.keys())
    tx_type = tx.get('type', '').upper()
    if tx_type == 'SEND':
        recipient = tx.get('recipient', '').lower()
        if recipient and recipient not in all_known:
            return True
    elif tx_type == 'RECEIVE':
        sender = tx.get('sender', '').lower()
        if sender and sender not in all_known:
            return True
    elif tx_type == 'MINT':
        return True  # MINTs always need classification
    return False

def compute_stats(positions):
    """Compute basic stats from positions."""
    total_lots = len(positions)
    symbols = set(p['symbol'] for p in positions if p['symbol'])
    unique_tokens = len(symbols)
    total_cost_basis = 0.0
    for p in positions:
        try:
            total_cost_basis += float(p['total']) if p['total'] else 0.0
        except (ValueError, TypeError):
            pass
    return {
        'total_lots': total_lots,
        'unique_tokens': unique_tokens,
        'total_cost_basis': round(total_cost_basis, 2),
    }

# Stablecoins — always $1.00
STABLECOINS = {'USDC', 'USDT', 'DAI', 'LUSD', 'BUSD', 'GUSD', 'USDP', 'TUSD', 'FRAX'}

# ============ COINGECKO PRICE LOOKUP ============

SYMBOL_TO_COINGECKO_ID = {
    'LQTY': 'liquity',
    'LUSD': 'liquity-usd',
    'ETH': 'ethereum',
    'BTC': 'bitcoin',
    'USDC': 'usd-coin',
    'USDT': 'tether',
    'DAI': 'dai',
    'LINK': 'chainlink',
    'UNI': 'uniswap',
    'AAVE': 'aave',
    'COMP': 'compound-governance-token',
    'SNX': 'synthetix-network-token',
    'SUSHI': 'sushi',
    'MKR': 'maker',
    'WETH': 'ethereum',
    'WBTC': 'bitcoin',
    'STETH': 'staked-ether',
    'CBETH': 'coinbase-wrapped-staked-eth',
    'RETH': 'rocket-pool-eth',
    'DEGEN': 'degen-base',
}


def get_coingecko_id(symbol):
    """Map a token symbol to a CoinGecko ID."""
    upper = symbol.upper()
    if upper in SYMBOL_TO_COINGECKO_ID:
        return SYMBOL_TO_COINGECKO_ID[upper]
    # Fallback: try lowercase symbol (often works for many tokens)
    return symbol.lower()


def parse_tx_date_for_coingecko(date_str):
    """Convert ISO date string to dd-mm-yyyy for CoinGecko API and a cache key."""
    if not date_str:
        return None, None
    # Handle "2025-06-25T17:36:00Z" or "2025-06-25 17:36:00"
    date_part = date_str.replace('T', ' ').replace('Z', '').split(' ')[0]
    parts = date_part.split('-')
    if len(parts) != 3:
        return None, None
    yyyy, mm, dd = parts
    cg_date = f"{dd}-{mm}-{yyyy}"
    cache_key_date = f"{yyyy}-{mm}-{dd}"
    return cg_date, cache_key_date


def date_str_to_unix(date_str):
    """Convert ISO date string to unix timestamp for DefiLlama."""
    try:
        dt = datetime.strptime(date_str.replace('T', ' ').replace('Z', '').split('.')[0].strip(), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
            dt = datetime.strptime(date_str.split('T')[0], '%Y-%m-%d')
        except ValueError:
            return None
    return int(dt.timestamp())


CHAIN_MAP = {
    'eth': 'ethereum',
    'ethereum': 'ethereum',
    'base': 'base',
    'matic': 'polygon',
    'polygon': 'polygon',
}


def fetch_defillama_price(contract_address, blockchain, timestamp):
    """Fetch historical price from DefiLlama. Returns price in USD or None."""
    chain = CHAIN_MAP.get(blockchain.lower(), blockchain.lower())
    coin_id = f"{chain}:{contract_address}"
    url = f"https://coins.llama.fi/prices/historical/{timestamp}/{coin_id}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        coins = data.get('coins', {})
        coin_data = coins.get(coin_id, {})
        price = coin_data.get('price')
        return price
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"  [DefiLlama] Error fetching {coin_id} at {timestamp}: {e}")
        return None


def fetch_coingecko_price(coingecko_id, cg_date):
    """Fetch historical price from CoinGecko. Returns price in USD or None."""
    url = f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/history?date={cg_date}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        price = data.get('market_data', {}).get('current_price', {}).get('usd')
        return price
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"  [CoinGecko] Error fetching {coingecko_id} on {cg_date}: {e}")
        return None


def infer_prices_from_trades(transactions):
    """Infer missing USD values from the other side of trades.
    In a swap, what you gave = what you got in USD terms."""
    for tx in transactions:
        if tx.get('type', '').upper() not in ('TRADE', 'MINT'):
            continue
        details = tx.get('parsed_details', {})
        sent = details.get('sent', [])
        received = details.get('received', [])

        # Calculate total known USD on each side
        def side_usd(items):
            total = 0
            for item in items:
                usd = item.get('usd_value', '') or item.get('pretty_usd', '')
                if isinstance(usd, str):
                    usd = usd.replace('$', '').replace(',', '').strip()
                try:
                    total += abs(float(usd)) if usd else 0
                except (ValueError, TypeError):
                    pass
            return total

        sent_usd = side_usd(sent)
        received_usd = side_usd(received)

        # Fill in missing values from the other side
        def fill_zero_items(items, total_from_other_side):
            """Fill $0 items using total from the other side minus known values on this side."""
            known = 0
            zero_items = []
            for item in items:
                usd = item.get('usd_value', '') or item.get('pretty_usd', '')
                if isinstance(usd, str):
                    usd = usd.replace('$', '').replace(',', '').strip()
                try:
                    val = abs(float(usd)) if usd else 0
                except:
                    val = 0
                if val > 0:
                    known += val
                else:
                    zero_items.append(item)
            if zero_items and total_from_other_side > 0:
                remainder = max(0, total_from_other_side - known)
                share = remainder / len(zero_items)
                for item in zero_items:
                    item['usd_value'] = str(share)
                    item['pretty_usd'] = f"${share:,.2f}"

        if sent_usd > 0:
            fill_zero_items(received, sent_usd)
        # Recalculate received_usd after filling
        received_usd = side_usd(received)
        if received_usd > 0:
            fill_zero_items(sent, received_usd)

    # Also handle RECEIVEs/SENDs where the Volume field has a value but line items don't
    for tx in transactions:
        details = tx.get('parsed_details', {})
        tx_value = tx.get('value', '')
        try:
            tx_usd = float(tx_value) if tx_value else 0
        except:
            tx_usd = 0
        if tx_usd <= 0:
            continue

        for direction in ('sent', 'received'):
            items = details.get(direction, [])
            if len(items) == 1:
                item = items[0]
                usd = item.get('usd_value', '') or item.get('pretty_usd', '')
                if isinstance(usd, str):
                    usd = usd.replace('$', '').replace(',', '').strip()
                try:
                    val = abs(float(usd)) if usd else 0
                except:
                    val = 0
                if val == 0:
                    item['usd_value'] = str(tx_usd)
                    item['pretty_usd'] = f"${tx_usd:,.2f}"

    # Force stablecoin prices to $1.00
    for tx in transactions:
        details = tx.get('parsed_details', {})
        for direction in ('sent', 'received'):
            for item in details.get(direction, []):
                token = item.get('token', '').upper()
                if token in STABLECOINS:
                    try:
                        amount = abs(float(item.get('amount', 0)))
                    except (ValueError, TypeError):
                        continue
                    if amount > 0:
                        item['usd_value'] = str(amount)
                        item['pretty_usd'] = f"${amount:,.2f}"


def fill_missing_usd_values(transactions, state):
    """Scan transactions for missing USD values and look them up via CoinGecko."""
    # First: infer prices from trade pairs (no API needed)
    infer_prices_from_trades(transactions)

    price_cache = state.get('price_cache', {})
    # Track tokens that have failed before (by coingecko_id, not date-specific)
    failed_tokens = set()
    for k, v in price_cache.items():
        if v is None:
            # Extract token id (everything before the last _YYYY-MM-DD)
            parts = k.rsplit('_', 1)
            if len(parts) == 2:
                failed_tokens.add(parts[0])

    # First pass: collect all needed lookups to deduplicate
    needed_lookups = {}  # cache_key -> (coingecko_id, cg_date, token)
    items_to_fill = []   # (item_ref, cache_key, amount)

    for tx in transactions:
        details = tx.get('parsed_details', {})
        date_str = tx.get('date', '')
        cg_date, cache_date = parse_tx_date_for_coingecko(date_str)
        if not cg_date or not cache_date:
            continue

        for category in ('sent', 'received', 'fees'):
            for item in details.get(category, []):
                # Skip fee items — gas fees already have values embedded or are negligible
                if category == 'fees':
                    continue

                usd_val = item.get('usd_value', '')
                if isinstance(usd_val, str):
                    usd_val = usd_val.replace('$', '').replace(',', '').strip()
                try:
                    usd_float = abs(float(usd_val)) if usd_val else 0
                except (ValueError, TypeError):
                    usd_float = 0

                if usd_float != 0:
                    continue

                token = item.get('token', '')
                if not token or token == 'NFT':
                    continue

                amount_str = item.get('amount', '0')
                try:
                    amount = abs(float(amount_str))
                except (ValueError, TypeError):
                    continue
                if amount == 0:
                    continue

                coingecko_id = get_coingecko_id(token)
                cache_key = f"{coingecko_id}_{cache_date}"

                # Skip if already cached (including failed)
                if cache_key in price_cache:
                    if price_cache[cache_key] is not None:
                        items_to_fill.append((item, cache_key, amount))
                    continue

                # Skip if this token has failed before on any date
                if coingecko_id in failed_tokens:
                    price_cache[cache_key] = None
                    continue

                contract = item.get('contract_address', '')
                blockchain = tx.get('blockchain', '')
                needed_lookups[cache_key] = (coingecko_id, cg_date, token, contract, blockchain, date_str)
                items_to_fill.append((item, cache_key, amount))

    # Second pass: batch API calls (CoinGecko first, then DefiLlama fallback)
    lookups_made = 0
    for cache_key, (coingecko_id, cg_date, token, contract, blockchain, date_str) in needed_lookups.items():
        if lookups_made > 0:
            time.sleep(0.5)

        # Try CoinGecko first
        print(f"  [CoinGecko] Looking up {token} ({coingecko_id}) on {cg_date}...")
        price = fetch_coingecko_price(coingecko_id, cg_date)
        lookups_made += 1

        # Fallback to DefiLlama if CoinGecko fails and we have a contract address
        if price is None and contract and blockchain:
            timestamp = date_str_to_unix(date_str)
            if timestamp:
                print(f"  [DefiLlama] Trying {token} ({blockchain}:{contract[:10]}...) ...")
                price = fetch_defillama_price(contract, blockchain, timestamp)
                if price is not None:
                    print(f"  [DefiLlama] Found: ${price} per {token}")

        price_cache[cache_key] = price
        if price is not None:
            print(f"  Found: ${price} per {token}")
        else:
            print(f"  No price data for {token} from any source")
            failed_tokens.add(coingecko_id)

    # Third pass: fill in values from cache
    for item, cache_key, amount in items_to_fill:
        price = price_cache.get(cache_key)
        if price is None:
            continue
        usd_value = price * amount
        item['usd_value'] = str(usd_value)
        item['pretty_usd'] = f"${usd_value:,.2f}"
        print(f"  [CoinGecko] Set {amount} {item.get('token', '?')} = ${usd_value:,.2f}")

    # Save the cache back to state
    state['price_cache'] = price_cache
    if lookups_made > 0:
        print(f"  [CoinGecko] Done. Made {lookups_made} API call(s).")

    # Run inference again now that API lookups may have filled in prices
    infer_prices_from_trades(transactions)

    return transactions


@app.context_processor
def inject_globals():
    """Inject tax_year into all templates."""
    return {'tax_year': get_tax_year()}

@app.route('/')
def index():
    state = load_state()
    if state and state.get('positions'):
        return redirect(url_for('positions'))
    return render_template('index.html', active_nav='positions')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return redirect(url_for('index'))
    file = request.files['file']
    if file.filename == '':
        return redirect(url_for('index'))
    if file and file.filename.endswith('.csv'):
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], 'opening_positions.csv')
        file.save(filepath)
        positions = parse_csv(filepath)
        state = ensure_state()
        state['positions'] = positions
        state['filename'] = file.filename
        save_state(state)
        return redirect(url_for('positions'))
    return redirect(url_for('index'))

@app.route('/positions')
def positions():
    state = load_state()
    if not state or not state.get('positions'):
        return redirect(url_for('index'))
    stats = compute_stats(state['positions'])
    return render_template('positions.html', positions=state['positions'], stats=stats,
                           filename=state.get('filename', ''), active_nav='positions')

@app.route('/reset', methods=['POST'])
def reset():
    """Remove ALL state and allow re-upload."""
    if os.path.exists(app.config['DATA_FILE']):
        os.remove(app.config['DATA_FILE'])
    return redirect(url_for('index'))

@app.route('/reupload-positions', methods=['POST'])
def reupload_positions():
    """Replace only opening positions, keeping wallets/transactions/classifications."""
    if 'file' not in request.files:
        return redirect(url_for('positions'))
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.csv'):
        return redirect(url_for('positions'))
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], 'opening_positions.csv')
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    file.save(filepath)
    new_positions = parse_csv(filepath)
    state = ensure_state()
    state['positions'] = new_positions
    state['filename'] = file.filename
    state['reconciliations'] = {}
    save_state(state)
    return redirect(url_for('positions'))

@app.route('/api/positions')
def api_positions():
    """JSON endpoint for positions (for client-side filtering)."""
    state = load_state()
    if not state:
        return jsonify([])
    symbol = request.args.get('symbol', '').strip()
    positions = state.get('positions', [])
    if symbol:
        positions = [p for p in positions if p['symbol'].lower() == symbol.lower()]
    return jsonify(positions)

# ============ WALLETS (Phase 2) ============

def is_known_single_token(tx, known_addresses, addr_map):
    """Check if a single-token SEND/RECEIVE goes to/from a known address."""
    all_known = set(known_addresses)
    if addr_map:
        all_known.update(addr_map.keys())
    tx_type = tx.get('type', '').upper()
    if tx_type == 'SEND':
        recipient = tx.get('recipient', '').lower()
        return recipient and recipient in all_known
    elif tx_type == 'RECEIVE':
        sender = tx.get('sender', '').lower()
        return sender and sender in all_known
    return False

def count_unclassified_items(tx, tx_index, wallet_id, classifications, known_addresses, addr_map):
    """Count unclassified non-fee line items for a transaction."""
    tx_type = tx.get('type', '').upper()
    # TRADEs are always auto-classified
    if tx_type == 'TRADE':
        return 0
    # Check if this tx needs classification at all
    if not check_needs_classification(tx, known_addresses, addr_map):
        return 0
    line_items = build_line_items(tx)
    non_fee = [li for li in line_items if li['direction'] != 'fee']
    # Single-token to known address is auto
    if len(non_fee) <= 1 and is_known_single_token(tx, known_addresses, addr_map):
        return 0

    # Check for Payment/LP Deposit MINTs — received NFTs are auto-classified
    is_payment_mint = False
    is_lp_deposit_mint = False
    if tx_type == 'MINT':
        sent_items = [li for li in non_fee if li['direction'] == 'sent']
        for si, _ in enumerate(sent_items):
            ik = f"{wallet_id}_{tx_index}_{si}"
            cl = classifications.get(ik, '')
            if cl == 'Payment':
                is_payment_mint = True
            elif cl == 'LP Deposit':
                is_lp_deposit_mint = True

    count = 0
    for item_idx, li in enumerate(non_fee):
        item_key = f"{wallet_id}_{tx_index}_{item_idx}"
        if item_key in classifications:
            continue
        # Auto-classified items don't need review
        if li.get('is_nft') and li['direction'] == 'received':
            if is_lp_deposit_mint:
                continue  # auto LP Position
            if is_payment_mint:
                sent_has_nft = any(s.get('is_nft') for s in tx.get('parsed_details', {}).get('sent', []))
                if not sent_has_nft:
                    continue  # auto Purchase
        count += 1
    return count

@app.route('/wallets')
def wallets():
    state = ensure_state()
    wallet_list = state.get('wallets', [])
    transactions = state.get('transactions', {})
    classifications = state.get('classifications', {})
    known_addresses = get_configured_addresses(state)
    addr_map = get_all_known_addresses(state)

    # Compute status per wallet
    wallet_status = {}
    for w in wallet_list:
        wid = w['id']
        txs = transactions.get(wid, [])
        needs_review = 0
        for i, tx in enumerate(txs):
            needs_review += count_unclassified_items(tx, i, wid, classifications, known_addresses, addr_map)
        wallet_status[wid] = {
            'total': len(txs),
            'needs_review': needs_review,
        }

    return render_template('wallets.html', wallets=wallet_list, wallet_status=wallet_status,
                           known_addresses=sorted(state.get('known_addresses', []), key=lambda ka: ka.get('label', '').lower()),
                           active_nav='wallets')

@app.route('/wallets/add', methods=['POST'])
def add_wallet():
    label = request.form.get('label', '').strip()
    address = request.form.get('address', '').strip()
    if not label or not address:
        return redirect(url_for('wallets'))
    state = ensure_state()
    # Generate simple ID
    wid = str(len(state['wallets']) + 1)
    # Ensure unique ID
    existing_ids = {w['id'] for w in state['wallets']}
    counter = len(state['wallets']) + 1
    while wid in existing_ids:
        counter += 1
        wid = str(counter)
    state['wallets'].append({'id': wid, 'label': label, 'address': address})
    save_state(state)
    return redirect(url_for('wallets'))

@app.route('/wallets/rename/<wallet_id>', methods=['POST'])
def rename_wallet(wallet_id):
    label = request.form.get('label', '').strip()
    if not label:
        return redirect(url_for('wallets'))
    state = ensure_state()
    for w in state['wallets']:
        if w['id'] == wallet_id:
            w['label'] = label
            break
    save_state(state)
    return redirect(url_for('wallets'))

@app.route('/wallets/remove/<wallet_id>', methods=['POST'])
def remove_wallet(wallet_id):
    state = ensure_state()
    state['wallets'] = [w for w in state['wallets'] if w['id'] != wallet_id]
    # Also remove transactions and classifications for this wallet
    state['transactions'].pop(wallet_id, None)
    # Remove classifications for this wallet
    to_remove = [k for k in state.get('classifications', {}) if k.startswith(f"{wallet_id}_")]
    for k in to_remove:
        del state['classifications'][k]
    save_state(state)
    return redirect(url_for('wallets'))

@app.route('/known-addresses/add', methods=['POST'])
def add_known_address():
    label = request.form.get('label', '').strip()
    address = request.form.get('address', '').strip()
    if not label or not address:
        return redirect(url_for('wallets'))
    state = ensure_state()
    # Find existing entry with same label
    existing = next((ka for ka in state['known_addresses'] if ka['label'].lower() == label.lower()), None)
    if existing:
        if address.lower() not in [a.lower() for a in existing['addresses']]:
            existing['addresses'].append(address)
    else:
        state['known_addresses'].append({'label': label, 'addresses': [address]})
    save_state(state)
    return redirect(url_for('wallets'))

@app.route('/known-addresses/rename', methods=['POST'])
def rename_known_address():
    old_label = request.form.get('old_label', '').strip()
    new_label = request.form.get('new_label', '').strip()
    if not old_label or not new_label:
        return redirect(url_for('wallets'))
    state = ensure_state()
    for ka in state['known_addresses']:
        if ka['label'] == old_label:
            ka['label'] = new_label
            break
    save_state(state)
    return redirect(url_for('wallets'))

@app.route('/known-addresses/remove', methods=['POST'])
def remove_known_address():
    label = request.form.get('label', '').strip()
    address = request.form.get('address', '').strip()
    state = ensure_state()
    for ka in state['known_addresses']:
        if ka['label'] == label:
            ka['addresses'] = [a for a in ka['addresses'] if a.lower() != address.lower()]
    state['known_addresses'] = [ka for ka in state['known_addresses'] if ka.get('addresses')]
    save_state(state)
    return redirect(url_for('wallets'))

@app.route('/wallets/upload/<wallet_id>', methods=['POST'])
def upload_wallet_csv(wallet_id):
    state = ensure_state()
    # Verify wallet exists
    wallet = next((w for w in state['wallets'] if w['id'] == wallet_id), None)
    if not wallet:
        return redirect(url_for('wallets'))
    if 'file' not in request.files:
        return redirect(url_for('wallet_detail', wallet_id=wallet_id))
    file = request.files['file']
    if file.filename == '' or not file.filename.endswith('.csv'):
        return redirect(url_for('wallet_detail', wallet_id=wallet_id))

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], f'wallet_{wallet_id}.csv')
    file.save(filepath)
    fmt = detect_csv_format(filepath)
    print(f"[Upload] Detected CSV format: {fmt}")
    if fmt == 'coinbase':
        transactions = parse_coinbase_csv(filepath)
    elif fmt == 'generic':
        transactions = parse_generic_csv(filepath)
    else:
        transactions = parse_wallet_csv(filepath)
    # Extract auto-classification hints (Coinbase parser only)
    auto_classifications = {}
    for i, tx in enumerate(transactions):
        hint = tx.pop('_auto_cls', None)
        if hint:
            for item_idx, cls in hint.items():
                auto_classifications[f"{wallet_id}_{i}_{item_idx}"] = cls
    # Auto-fill missing USD values from CoinGecko
    print(f"[CoinGecko] Scanning {len(transactions)} transactions for missing USD values...")
    transactions = fill_missing_usd_values(transactions, state)
    # Preserve manual transactions and their classifications across CSV re-uploads
    old_txs = state.get('transactions', {}).get(wallet_id, [])
    manual_txs = [tx for tx in old_txs if tx.get('manual')]
    manual_old_indices = [i for i, tx in enumerate(old_txs) if tx.get('manual')]
    # Collect classification keys for manual transactions (old index -> classifications)
    manual_classifications = {}
    for old_idx in manual_old_indices:
        prefix = f"{wallet_id}_{old_idx}_"
        for k, v in state.get('classifications', {}).items():
            if k.startswith(prefix):
                suffix = k[len(prefix):]
                manual_classifications[(old_idx, suffix)] = v

    state['transactions'][wallet_id] = transactions
    # Clear old classifications for this wallet since new data
    to_remove = [k for k in state.get('classifications', {}) if k.startswith(f"{wallet_id}_")]
    for k in to_remove:
        del state['classifications'][k]

    # Apply auto-classifications discovered during parsing
    for k, v in auto_classifications.items():
        state['classifications'][k] = v

    # Re-append manual transactions at the end and restore their classifications
    if manual_txs:
        base_idx = len(transactions)
        for offset, mtx in enumerate(manual_txs):
            state['transactions'][wallet_id].append(mtx)
            new_idx = base_idx + offset
            old_idx = manual_old_indices[offset]
            for (oi, suffix), cls_val in manual_classifications.items():
                if oi == old_idx:
                    state['classifications'][f"{wallet_id}_{new_idx}_{suffix}"] = cls_val

    save_state(state)
    return redirect(url_for('wallet_detail', wallet_id=wallet_id))

@app.route('/wallets/<wallet_id>/add-transaction', methods=['POST'])
def add_manual_transaction(wallet_id):
    """Add a manually created transaction to a wallet."""
    state = ensure_state()
    wallet = next((w for w in state['wallets'] if w['id'] == wallet_id), None)
    if not wallet:
        return redirect(url_for('wallets'))

    tx_date = request.form.get('date', '').strip()
    tx_type = request.form.get('type', 'RECEIVE').strip().upper()
    token = request.form.get('token', '').strip()
    amount = request.form.get('amount', '0').strip()
    usd_value = request.form.get('usd_value', '0').strip()
    classification = request.form.get('classification', '').strip()

    # For TRADE: second set of fields
    token2 = request.form.get('token2', '').strip()
    amount2 = request.form.get('amount2', '0').strip()
    usd_value2 = request.form.get('usd_value2', '0').strip()

    if not tx_date or not token:
        return redirect(url_for('wallet_detail', wallet_id=wallet_id))

    # Convert date to ISO format
    try:
        dt = datetime.strptime(tx_date, '%Y-%m-%d %H:%M')
        iso_date = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        # Try with just date
        try:
            dt = datetime.strptime(tx_date, '%Y-%m-%d')
            iso_date = dt.strftime('%Y-%m-%dT00:00:00Z')
        except ValueError:
            iso_date = tx_date

    # Build parsed_details
    sent_items = []
    received_items = []

    item = {
        'token': token,
        'amount': amount,
        'usd_value': usd_value,
        'pretty_usd': f'${float(usd_value):,.2f}' if usd_value else '$0.00',
        'is_nft': False,
    }

    if tx_type == 'RECEIVE':
        received_items.append(item)
    elif tx_type == 'SEND':
        sent_items.append(item)
    elif tx_type == 'TRADE':
        sent_items.append(item)
        if token2:
            received_items.append({
                'token': token2,
                'amount': amount2,
                'usd_value': usd_value2,
                'pretty_usd': f'${float(usd_value2):,.2f}' if usd_value2 else '$0.00',
                'is_nft': False,
            })

    tx = {
        'date': iso_date,
        'type': tx_type,
        'blockchain': 'manual',
        'tx_hash': '',
        'sender': '',
        'recipient': '',
        'url': '',
        'value': usd_value,
        'manual': True,
        'parsed_details': {
            'sent': sent_items,
            'received': received_items,
            'fees': [],
        },
    }

    if wallet_id not in state['transactions']:
        state['transactions'][wallet_id] = []
    state['transactions'][wallet_id].append(tx)

    # Auto-set classification if provided
    if classification:
        tx_index = len(state['transactions'][wallet_id]) - 1
        # Classify each non-fee item
        item_idx = 0
        for s in sent_items:
            cls_key = f"{wallet_id}_{tx_index}_{item_idx}"
            state['classifications'][cls_key] = classification
            item_idx += 1
        for r in received_items:
            cls_key = f"{wallet_id}_{tx_index}_{item_idx}"
            state['classifications'][cls_key] = classification
            item_idx += 1

    save_state(state)
    return redirect(url_for('wallet_detail', wallet_id=wallet_id))

def format_amount(amount):
    """Format an amount to reasonable precision."""
    try:
        val = float(amount)
    except (ValueError, TypeError):
        return str(amount) if amount else '0'
    abs_val = abs(val)
    if abs_val == 0:
        return '0'
    elif abs_val >= 1:
        return f"{val:,.2f}"
    elif abs_val >= 0.01:
        return f"{val:,.4f}"
    else:
        return f"{val:,.6f}"

def format_usd(value):
    """Format a value as USD currency string."""
    try:
        val = float(value)
    except (ValueError, TypeError):
        return '$0.00'
    return f"${val:,.2f}"

def format_date(date_str):
    """Format ISO date to more readable format: YYYY-MM-DD HH:MM."""
    if not date_str:
        return ''
    # Handle ISO format like "2025-06-25T17:36:00Z" or "2025-06-25 17:36:00"
    date_str = date_str.replace('T', ' ').replace('Z', '')
    # Truncate to minutes
    if len(date_str) > 16:
        date_str = date_str[:16]
    return date_str

def build_line_items(tx):
    """Build line items from parsed_details for expandable sub-rows."""
    details = tx.get('parsed_details', {})
    line_items = []

    for item in details.get('sent', []):
        usd_val = item.get('pretty_usd', '') or item.get('usd_value', '')
        if isinstance(usd_val, str):
            usd_val = usd_val.replace('$', '').replace(',', '').strip()
        try:
            usd_float = abs(float(usd_val)) if usd_val else 0
        except (ValueError, TypeError):
            usd_float = 0
        is_nft = item.get('is_nft', False)
        amt = str(int(float(item.get('amount', '0')))) if is_nft else format_amount(item.get('amount', ''))
        line_items.append({
            'direction': 'sent',
            'amount': amt,
            'token': item.get('token', ''),
            'usd_value': format_usd(usd_float),
            'missing_value': usd_float == 0,
            'is_nft': is_nft,
        })

    for item in details.get('received', []):
        usd_val = item.get('pretty_usd', '') or item.get('usd_value', '')
        if isinstance(usd_val, str):
            usd_val = usd_val.replace('$', '').replace(',', '').strip()
        try:
            usd_float = float(usd_val) if usd_val else 0
        except (ValueError, TypeError):
            usd_float = 0
        is_nft = item.get('is_nft', False)
        amt = str(int(float(item.get('amount', '0')))) if is_nft else format_amount(item.get('amount', ''))
        line_items.append({
            'direction': 'received',
            'amount': amt,
            'token': item.get('token', ''),
            'usd_value': format_usd(usd_float),
            'missing_value': usd_float == 0,
            'is_nft': is_nft,
        })

    for item in details.get('fees', []):
        usd_val = item.get('pretty_usd', '') or item.get('usd_value', '')
        if isinstance(usd_val, str):
            usd_val = usd_val.replace('$', '').replace(',', '').strip()
        try:
            usd_float = abs(float(usd_val)) if usd_val else 0
        except (ValueError, TypeError):
            usd_float = 0
        line_items.append({
            'direction': 'fee',
            'amount': format_amount(item.get('amount', '')),
            'token': item.get('token', ''),
            'usd_value': format_usd(usd_float),
            'missing_value': usd_float == 0,
        })

    return line_items

def build_parent_summary(tx, line_items):
    """Build a short summary for multi-token parent rows."""
    details = tx.get('parsed_details', {})
    tx_type = tx.get('type', '').upper()
    sent = details.get('sent', [])
    received = details.get('received', [])

    if tx_type == 'TRADE':
        parts = []
        if sent:
            parts.append(f"Sold {sent[0]['token']}")
        if received:
            parts.append(f"Got {received[0]['token']}")
        return ' / '.join(parts) if parts else 'Trade'
    elif tx_type == 'RECEIVE':
        non_fee = [li for li in line_items if li['direction'] != 'fee']
        count = len(non_fee)
        return f"{count} tokens received"
    elif tx_type == 'SEND':
        non_fee = [li for li in line_items if li['direction'] != 'fee']
        count = len(non_fee)
        return f"{count} tokens sent"
    else:
        non_fee = [li for li in line_items if li['direction'] != 'fee']
        return f"{len(non_fee)} tokens"

@app.route('/wallets/<wallet_id>')
def wallet_detail(wallet_id):
    state = ensure_state()
    wallet = next((w for w in state['wallets'] if w['id'] == wallet_id), None)
    if not wallet:
        return redirect(url_for('wallets'))
    transactions = state.get('transactions', {}).get(wallet_id, [])
    classifications = state.get('classifications', {})
    known_addresses = get_configured_addresses(state)
    addr_map = get_all_known_addresses(state)

    # Annotate transactions
    display_txs = []
    needs_review_count = 0
    for i, tx in enumerate(transactions):
        tx_type = tx.get('type', '').upper()
        needs_classification = check_needs_classification(tx, known_addresses, addr_map)
        is_trade = tx_type == 'TRADE'

        line_items = build_line_items(tx)
        non_fee_items = [li for li in line_items if li['direction'] != 'fee']
        is_multi = len(non_fee_items) > 1

        # Determine if single-token tx to/from known address (auto-classified)
        is_known_single = (not is_multi) and is_known_single_token(tx, known_addresses, addr_map)

        # Annotate each non-fee line item with classification info
        # Check if this is a MINT with a Payment or LP Deposit classification on sent items
        is_payment_mint = False
        is_lp_deposit_mint = False
        if tx.get('type', '').upper() == 'MINT':
            sent_idx = 0
            for li in line_items:
                if li['direction'] == 'sent':
                    ik = f"{wallet_id}_{i}_{sent_idx}"
                    cl = classifications.get(ik, '')
                    if cl == 'Payment':
                        is_payment_mint = True
                    elif cl == 'LP Deposit':
                        is_lp_deposit_mint = True
                if li['direction'] != 'fee':
                    sent_idx += 1

        item_idx = 0
        classified_count = 0
        total_classifiable = 0
        for li in line_items:
            if li['direction'] == 'fee':
                li['item_key'] = ''
                li['classification'] = ''
                li['show_dropdown'] = False
                li['auto_label'] = ''
                continue

            item_key = f"{wallet_id}_{i}_{item_idx}"
            li['item_key'] = item_key
            li['classification'] = classifications.get(item_key, '')

            if is_trade:
                # Trades are always auto-classified (taxable disposal)
                li['show_dropdown'] = False
                li['auto_label'] = 'Trade'
                classified_count += 1
            elif is_lp_deposit_mint and li['direction'] == 'received' and li.get('is_nft'):
                # LP deposit: NFT received is the LP position
                li['show_dropdown'] = False
                li['auto_label'] = 'LP Position'
                classified_count += 1
            elif is_payment_mint and li['direction'] == 'received' and li.get('is_nft'):
                # Payment mint: auto-classify only if sent items are fungible tokens (not NFTs)
                sent_has_nft = any(s.get('is_nft') for s in tx.get('parsed_details', {}).get('sent', []))
                if not sent_has_nft:
                    li['show_dropdown'] = False
                    li['auto_label'] = 'Purchase'
                    classified_count += 1
                else:
                    # Sent item is an NFT (redemption) — let user classify
                    li['show_dropdown'] = True
                    li['auto_label'] = ''
                    if li['classification']:
                        classified_count += 1
            elif not needs_classification or is_known_single:
                # Known address or no classification needed
                li['show_dropdown'] = False
                li['auto_label'] = 'Auto'
                classified_count += 1
            elif li['classification']:
                # User has classified this item
                li['show_dropdown'] = True
                li['auto_label'] = ''
                classified_count += 1
            else:
                # Needs classification, not yet classified
                li['show_dropdown'] = True
                li['auto_label'] = ''
                needs_review_count += 1

            total_classifiable += 1
            item_idx += 1

        # Build classification status summary for multi-token parent rows
        if is_multi:
            if is_trade:
                classification_summary = 'Trade'
            elif total_classifiable > 0 and classified_count == total_classifiable:
                classification_summary = 'All classified'
            elif classified_count > 0:
                classification_summary = f"{classified_count} of {total_classifiable} classified"
            else:
                classification_summary = ''
        else:
            classification_summary = ''

        # For LP deposits: recalculate NFT value as sum of all sent items
        if is_lp_deposit_mint:
            sent_total = 0
            for li in line_items:
                if li.get('direction') == 'sent':
                    usd_str = li.get('usd_value', '$0.00').replace('$', '').replace(',', '')
                    try:
                        sent_total += abs(float(usd_str))
                    except (ValueError, TypeError):
                        pass
            # Update NFT line item value
            for li in line_items:
                if li.get('direction') == 'received' and li.get('is_nft'):
                    li['usd_value'] = format_usd(sent_total)
                    li['missing_value'] = False

        # Format the value (USD) column
        raw_value = tx.get('value', '')
        try:
            raw_float = float(raw_value) if raw_value else 0
        except (ValueError, TypeError):
            raw_float = 0
        if is_lp_deposit_mint:
            # LP deposit: parent shows sum of sent items
            sent_total = 0
            for li in line_items:
                if li.get('direction') == 'sent':
                    usd_str = li.get('usd_value', '$0.00').replace('$', '').replace(',', '')
                    try:
                        sent_total += abs(float(usd_str))
                    except (ValueError, TypeError):
                        pass
            formatted_value = format_usd(sent_total)
        elif raw_float == 0 and line_items:
            # Use max of sent total vs received total (not sum of all, to avoid double-counting)
            sent_total = 0
            recv_total = 0
            for li in line_items:
                usd_str = li.get('usd_value', '$0.00').replace('$', '').replace(',', '')
                try:
                    val = abs(float(usd_str))
                except (ValueError, TypeError):
                    val = 0
                if li.get('direction') == 'sent':
                    sent_total += val
                elif li.get('direction') == 'received':
                    recv_total += val
            formatted_value = format_usd(max(sent_total, recv_total))
        else:
            formatted_value = format_usd(raw_value) if raw_value else '$0.00'

        # Summary for parent row
        if is_multi:
            summary = build_parent_summary(tx, line_items)
        else:
            summary = format_details(tx, addr_map)

        # For single-token rows, pull classification info from item 0
        single_item = non_fee_items[0] if non_fee_items else None

        single_show_dropdown = single_item.get('show_dropdown', False) if single_item else False
        single_classification = single_item.get('classification', '') if single_item else ''
        if is_multi:
            is_needs_review = needs_classification and classification_summary not in ('All classified', 'Trade')
        else:
            is_needs_review = single_show_dropdown and not single_classification

        display_txs.append({
            'index': i,
            'date': format_date(tx.get('date', '')),
            'type': tx.get('type', ''),
            'details': summary,
            'value': formatted_value,
            'tx_hash': tx.get('tx_hash', ''),
            'url': tx.get('url') or _explorer_url(tx.get('blockchain', ''), tx.get('tx_hash', '')),
            'blockchain': tx.get('blockchain', ''),
            'needs_classification': needs_classification,
            'sender': tx.get('sender', ''),
            'recipient': tx.get('recipient', ''),
            'line_items': line_items,
            'is_multi': is_multi,
            'is_trade': is_trade,
            'classification_summary': classification_summary,
            'is_needs_review': is_needs_review,
            # Single-token row classification
            'single_item_key': single_item.get('item_key', '') if single_item else '',
            'single_classification': single_classification,
            'single_show_dropdown': single_show_dropdown,
            'single_auto_label': single_item.get('auto_label', '') if single_item else '',
        })

    show_all = request.args.get('show_all') == '1'
    search_q = request.args.get('q', '').strip()
    total_count = len(display_txs)
    if search_q:
        ql = search_q.lower()
        def matches(tx):
            if ql in (tx.get('details') or '').lower(): return True
            if ql in (tx.get('type') or '').lower(): return True
            for li in tx.get('line_items', []):
                if ql in (li.get('token') or '').lower(): return True
            return False
        display_txs = [tx for tx in display_txs if matches(tx)]
    elif not show_all:
        display_txs = [tx for tx in display_txs if tx.get('is_needs_review')]
    return render_template('wallet_detail.html', wallet=wallet, transactions=display_txs,
                           needs_review_count=needs_review_count, total_count=total_count,
                           shown_count=len(display_txs), show_all=show_all,
                           search_q=search_q, active_nav='wallets')

@app.route('/wallets/classify', methods=['POST'])
def classify_transaction():
    # Support both form and JSON submissions
    if request.is_json:
        data = request.get_json()
        tx_key = data.get('tx_key', '')
        classification = data.get('classification', '')
    else:
        tx_key = request.form.get('tx_key', '')
        classification = request.form.get('classification', '')
    if not tx_key:
        if request.is_json:
            return jsonify({'status': 'error', 'message': 'No tx_key'}), 400
        return redirect(url_for('wallets'))
    with _state_lock:
        state = ensure_state()
        if classification:
            state['classifications'][tx_key] = classification
        else:
            state['classifications'].pop(tx_key, None)
        save_state(state)
    if request.is_json:
        return jsonify({'status': 'ok', 'tx_key': tx_key, 'classification': classification})
    # Redirect back to the wallet detail page
    wallet_id = tx_key.split('_')[0]
    return redirect(url_for('wallet_detail', wallet_id=wallet_id))

# ============ RECONCILE (Phase 3) ============

def get_trade_proceeds(tx):
    """Get the USD proceeds from a trade (value of what was received)."""
    details = tx.get('parsed_details', {})
    received = details.get('received', [])
    total = 0.0
    for r in received:
        usd_val = r.get('usd_value', '') or r.get('pretty_usd', '')
        if isinstance(usd_val, str):
            usd_val = usd_val.replace('$', '').replace(',', '').strip()
        try:
            total += abs(float(usd_val))
        except (ValueError, TypeError):
            pass
    return total


def build_2025_lots(state, wallet_id):
    """Scan wallet transactions and build acquisition lots from 2025 activity.

    Returns a list of lots from:
    - RECEIVE transactions classified as 'Income' or 'Airdrop'
    - The received side of TRADE transactions
    Each lot: {date, symbol, volume, price, total, account, source, tx_index, item_index}
    """
    transactions = state.get('transactions', {}).get(wallet_id, [])
    classifications = state.get('classifications', {})
    wallet = next((w for w in state['wallets'] if w['id'] == wallet_id), None)
    if not wallet:
        return []
    wallet_address = wallet['address']

    lots = []
    for tx_index, tx in enumerate(transactions):
        tx_type = tx.get('type', '').upper()
        details = tx.get('parsed_details', {})
        tx_date = tx.get('date', '')

        if tx_type == 'RECEIVE':
            received = details.get('received', [])
            for item_index, item in enumerate(received):
                item_key = f"{wallet_id}_{tx_index}_{item_index}"
                classification = classifications.get(item_key, '')
                # Skip transfers — the virtual lot pool handles them
                sender = tx.get('sender', '').lower()
                known_wallets = {w['address'].lower() for w in state.get('wallets', [])}
                is_transfer_from_own_wallet = sender in known_wallets
                if classification == 'Transfer' or (not classification and is_transfer_from_own_wallet):
                    continue
                if classification not in ('Income', 'Airdrop'):
                    continue
                token = item.get('token', '')
                try:
                    amount = abs(float(item.get('amount', 0)))
                except (ValueError, TypeError):
                    amount = 0
                if amount <= 0:
                    continue
                # Get USD value
                usd_val = item.get('usd_value', '') or item.get('pretty_usd', '')
                if isinstance(usd_val, str):
                    usd_val = usd_val.replace('$', '').replace(',', '').strip()
                try:
                    total_usd = abs(float(usd_val)) if usd_val else 0
                except (ValueError, TypeError):
                    total_usd = 0
                price_per = total_usd / amount if amount > 0 else 0

                if classification == 'Airdrop':
                    source = '2025_airdrop'
                else:
                    source = '2025_income'
                lots.append({
                    'date': tx_date,
                    'symbol': token,
                    'volume': amount,
                    'price': price_per,
                    'total': total_usd,
                    'account': wallet_address,
                    'source': source,
                    'tx_index': tx_index,
                    'item_index': item_index,
                })

        elif tx_type == 'TRADE':
            received = details.get('received', [])
            for item_index, item in enumerate(received):
                token = item.get('token', '')
                try:
                    amount = abs(float(item.get('amount', 0)))
                except (ValueError, TypeError):
                    amount = 0
                if amount <= 0:
                    continue
                usd_val = item.get('usd_value', '') or item.get('pretty_usd', '')
                if isinstance(usd_val, str):
                    usd_val = usd_val.replace('$', '').replace(',', '').strip()
                try:
                    total_usd = abs(float(usd_val)) if usd_val else 0
                except (ValueError, TypeError):
                    total_usd = 0
                price_per = total_usd / amount if amount > 0 else 0

                lots.append({
                    'date': tx_date,
                    'symbol': token,
                    'volume': amount,
                    'price': price_per,
                    'total': total_usd,
                    'account': wallet_address,
                    'source': '2025_trade',
                    'tx_index': tx_index,
                    'item_index': item_index,
                })

        elif tx_type == 'MINT':
            # Check if any sent item is classified as Payment — received items become lots
            has_payment = False
            for item_idx, _ in enumerate(details.get('sent', [])):
                item_key = f"{wallet_id}_{tx_index}_{item_idx}"
                if classifications.get(item_key) == 'Payment':
                    has_payment = True
                    break
            if has_payment:
                received = details.get('received', [])
                for item_index, item in enumerate(received):
                    token = item.get('token', '')
                    try:
                        amount = abs(float(item.get('amount', 0)))
                    except (ValueError, TypeError):
                        amount = 0
                    if amount <= 0:
                        continue
                    usd_val = item.get('usd_value', '') or item.get('pretty_usd', '')
                    if isinstance(usd_val, str):
                        usd_val = usd_val.replace('$', '').replace(',', '').strip()
                    try:
                        total_usd = abs(float(usd_val)) if usd_val else 0
                    except (ValueError, TypeError):
                        total_usd = 0
                    price_per = total_usd / amount if amount > 0 else 0

                    lots.append({
                        'date': tx_date,
                        'symbol': token,
                        'volume': amount,
                        'price': price_per,
                        'total': total_usd,
                        'account': wallet_address,
                        'source': '2025_mint',
                        'tx_index': tx_index,
                        'item_index': item_index,
                    })

    return lots


def _parse_usd_value(item):
    """Extract a float USD value from a ledger item."""
    usd_val = item.get('usd_value', '') or item.get('pretty_usd', '')
    if isinstance(usd_val, str):
        usd_val = usd_val.replace('$', '').replace(',', '').strip()
    try:
        return abs(float(usd_val)) if usd_val else 0
    except (ValueError, TypeError):
        return 0


def _parse_amount(item):
    """Extract a float amount from a ledger item."""
    try:
        return abs(float(item.get('amount', 0)))
    except (ValueError, TypeError):
        return 0


def _consume_from_pool(pool_lots, symbol, amount, method):
    """Consume lots from a pool for a given symbol using LIFO/FIFO.

    Mutates pool_lots in-place (reduces volumes, removes exhausted lots).
    Returns list of consumed lot dicts: [{lot_id, date, volume_used, price, cost_basis, source}]
    """
    symbol_upper = symbol.upper()
    # Gather matching lots with their indices
    matching = [(i, lot) for i, lot in enumerate(pool_lots) if lot['symbol'].upper() == symbol_upper]
    # Sort by date: LIFO = newest first, FIFO = oldest first
    matching.sort(key=lambda x: x[1]['date'], reverse=(method == 'LIFO'))

    consumed = []
    remaining = amount
    dust_tolerance = max(1e-9, amount * 0.0000001)
    indices_to_remove = []

    for idx, lot in matching:
        if remaining <= dust_tolerance:
            break
        vol_to_use = min(lot['volume'], remaining)
        cost_basis = vol_to_use * lot['price']
        consumed.append({
            'lot_id': lot['lot_id'],
            'date': lot['date'],
            'volume_used': vol_to_use,
            'price': lot['price'],
            'cost_basis': cost_basis,
            'source': lot['source'],
        })
        remaining -= vol_to_use
        lot['volume'] -= vol_to_use
        if lot['volume'] <= 0.000001:
            indices_to_remove.append(idx)

    # Remove exhausted lots (in reverse order to preserve indices)
    for idx in sorted(indices_to_remove, reverse=True):
        pool_lots.pop(idx)

    return consumed


def build_wallet_lot_pools(state, method, up_to_date=None, skip_trade_consumption=False):
    """Build virtual lot pools for all wallets by processing events chronologically.

    Returns: {wallet_address_lower: [lot_dicts]}
    Each lot: {lot_id, date, symbol, volume, price, total, source}

    skip_trade_consumption: if True, don't consume the sold side of TRADE/MINT transactions.
        The received side is still added. Use this when _run_reconcile_all will do the consuming.

    The pool is computed fresh each time (idempotent). Opening positions are never modified.
    """
    import copy

    positions = state.get('positions', [])
    transactions = state.get('transactions', {})
    classifications = state.get('classifications', {})
    wallets = state.get('wallets', [])
    wallet_map = {w['id']: w for w in wallets}
    own_addresses = {w['address'].lower() for w in wallets}
    # Map address -> wallet_address (lowercase)
    addr_to_wallet = {w['address'].lower(): w['address'].lower() for w in wallets}
    # Also map known addresses to their labels (for Coinbase etc.)
    known_addr_labels = {}
    for ka in state.get('known_addresses', []):
        for addr in ka.get('addresses', []):
            known_addr_labels[addr.lower()] = ka['label']

    # Initialize pools with opening position lots
    pools = {}  # wallet_address_lower -> [lots]
    for w in wallets:
        pools[w['address'].lower()] = []

    # Add opening positions to pools
    for i, p in enumerate(positions):
        account = p.get('account', '').lower()
        if account not in pools:
            # Opening position assigned to an address not in wallets — skip or add to that key
            pools.setdefault(account, [])
        try:
            volume = float(p.get('volume', 0))
            price = float(p.get('price', 0))
            total = float(p.get('total', 0))
        except (ValueError, TypeError):
            continue
        if volume <= 0:
            continue
        effective_price = price if price > 0 else (total / volume if volume > 0 else 0)
        pools[account].append({
            'lot_id': f"op_{i}",
            'date': p.get('date', ''),
            'symbol': p.get('symbol', '').upper(),
            'volume': volume,
            'price': effective_price,
            'total': total,
            'source': 'opening',
        })

    # Staked-lots pools: separate sub-pool per wallet for tokens locked via Staking.
    # Lots move from main pool -> staked pool on Staking SEND, and back on Staking RECEIVE,
    # preserving original cost basis and acquisition date across the lock period.
    staked_pools = {}  # wallet_addr -> [staked lots]

    # Collect ALL events across ALL wallets
    lot_counter = [0]  # mutable counter for unique lot IDs

    def next_lot_id(prefix):
        lot_counter[0] += 1
        return f"{prefix}_{lot_counter[0]}"

    events = []
    for wid, txs in transactions.items():
        wallet = wallet_map.get(wid)
        if not wallet:
            continue
        wallet_addr = wallet['address'].lower()
        for ti, tx in enumerate(txs):
            tx_date = tx.get('date', '')
            if up_to_date and tx_date > up_to_date:
                continue
            events.append({
                'date': tx_date,
                'wallet_id': wid,
                'wallet_addr': wallet_addr,
                'tx_index': ti,
                'tx': tx,
            })

    # Sort chronologically
    events.sort(key=lambda e: e['date'])

    # Process each event
    for event in events:
        tx = event['tx']
        wid = event['wallet_id']
        wallet_addr = event['wallet_addr']
        ti = event['tx_index']
        tx_type = tx.get('type', '').upper()
        details = tx.get('parsed_details', {})
        tx_date = tx.get('date', '')

        if tx_type == 'RECEIVE':
            sender = tx.get('sender', '').lower()
            received = details.get('received', [])
            is_from_own_wallet = sender in own_addresses
            is_from_known_address = sender in known_addr_labels
            known_label = known_addr_labels.get(sender, '').lower()

            for ii, item in enumerate(received):
                item_key = f"{wid}_{ti}_{ii}"
                cls = classifications.get(item_key, '')
                token = item.get('token', '')
                amount = _parse_amount(item)
                if amount <= 0 or not token:
                    continue

                is_transfer = cls == 'Transfer' or (not cls and (is_from_own_wallet or is_from_known_address))

                if is_transfer and (sender in addr_to_wallet or is_from_known_address):
                    # Transfer IN: consume lots from source, add to dest
                    if sender in addr_to_wallet:
                        source_addr = addr_to_wallet[sender]
                    else:
                        # From known address (e.g., Coinbase) — use label as pool key
                        source_addr = known_label
                    source_pool = pools.get(source_addr, [])
                    # Fallback: if the label-keyed pool has nothing for this token but
                    # the sender's hex address itself has a pool (opening positions whose
                    # account field used the hex address), use that.
                    if not any(l.get('symbol','').upper() == token.upper() for l in source_pool):
                        if sender in pools and any(l.get('symbol','').upper() == token.upper() for l in pools[sender]):
                            source_pool = pools[sender]
                    consumed_lots = _consume_from_pool(source_pool, token, amount, method)
                    # Add consumed lots to destination pool (carrying original cost basis)
                    dest_pool = pools.setdefault(wallet_addr, [])
                    for cl in consumed_lots:
                        dest_pool.append({
                            'lot_id': cl['lot_id'],  # keep original lot_id for tracing
                            'date': cl['date'],
                            'symbol': token.upper(),
                            'volume': cl['volume_used'],
                            'price': cl['price'],
                            'total': cl['volume_used'] * cl['price'],
                            'source': cl['source'],
                        })
                    # If there's unmatched volume (no lots in source), don't create fallback lots
                    # The trade will stay unmatched until the source wallet is uploaded
                elif cls in ('Income', 'Airdrop'):
                    # Income/Airdrop: add new lot at FMV
                    total_usd = _parse_usd_value(item)
                    price_per = total_usd / amount if amount > 0 else 0
                    source = '2025_airdrop' if cls == 'Airdrop' else '2025_income'
                    pools.setdefault(wallet_addr, []).append({
                        'lot_id': next_lot_id(source),
                        'date': tx_date,
                        'symbol': token.upper(),
                        'volume': amount,
                        'price': price_per,
                        'total': total_usd,
                        'source': source,
                    })
                elif cls == 'Staking':
                    # Staking RECEIVE (unstaking): pull original lots from staked sub-pool
                    # back to the main pool, preserving cost basis and acquisition date.
                    # Any excess (staking rewards) gets a new lot at FMV.
                    staked = staked_pools.get(wallet_addr, [])
                    consumed = _consume_from_pool(staked, token, amount, method)
                    recovered = 0.0
                    dest_pool = pools.setdefault(wallet_addr, [])
                    for cl in consumed:
                        dest_pool.append({
                            'lot_id': cl['lot_id'],
                            'date': cl['date'],
                            'symbol': token.upper(),
                            'volume': cl['volume_used'],
                            'price': cl['price'],
                            'total': cl['volume_used'] * cl['price'],
                            'source': cl['source'],
                        })
                        recovered += cl['volume_used']
                    excess = amount - recovered
                    if excess > 1e-9:
                        # Rewards above what was originally staked → new lot at FMV.
                        total_usd = _parse_usd_value(item) * (excess / amount) if amount > 0 else 0
                        price_per = total_usd / excess if excess > 0 else 0
                        dest_pool.append({
                            'lot_id': next_lot_id('2025_unstaking'),
                            'date': tx_date,
                            'symbol': token.upper(),
                            'volume': excess,
                            'price': price_per,
                            'total': total_usd,
                            'source': '2025_unstaking',
                        })

        elif tx_type == 'SEND':
            recipient = tx.get('recipient', '').lower()
            sent = details.get('sent', [])
            is_to_own_wallet = recipient in own_addresses

            for ii, item in enumerate(sent):
                item_key = f"{wid}_{ti}_{ii}"
                cls = classifications.get(item_key, '')
                token = item.get('token', '')
                amount = _parse_amount(item)
                if amount <= 0 or not token:
                    continue

                is_transfer = cls == 'Transfer' or (not cls and is_to_own_wallet)
                if is_transfer:
                    # Transfer OUT: handled by the RECEIVE side on the destination wallet
                    # But we still need to consume from source pool
                    # Actually NO: the RECEIVE handler on the destination wallet
                    # already does _consume_from_pool on the source. So skip here.
                    pass
                elif cls == 'Staking':
                    # Staking SEND: move lots from main pool to staked sub-pool, preserving
                    # original basis and date so unstaking can restore them later.
                    source_pool = pools.get(wallet_addr, [])
                    consumed = _consume_from_pool(source_pool, token, amount, method)
                    staked_dest = staked_pools.setdefault(wallet_addr, [])
                    for cl in consumed:
                        staked_dest.append({
                            'lot_id': cl['lot_id'],
                            'date': cl['date'],
                            'symbol': token.upper(),
                            'volume': cl['volume_used'],
                            'price': cl['price'],
                            'total': cl['volume_used'] * cl['price'],
                            'source': cl['source'],
                        })

        elif tx_type == 'TRADE':
            sent = details.get('sent', [])
            received = details.get('received', [])

            # Asset Migration: classified as 'Migration' on the sent side. Carry cost basis
            # from old token's lots to new token (no taxable event).
            is_migration = any(
                classifications.get(f"{wid}_{ti}_{idx}") == 'Migration'
                for idx in range(len(sent))
            )
            if is_migration:
                if sent and received:
                    sent_token = sent[0].get('token', '')
                    sent_amount = _parse_amount(sent[0])
                    recv_token = received[0].get('token', '')
                    if sent_token and recv_token and sent_amount > 0:
                        wallet_pool = pools.setdefault(wallet_addr, [])
                        consumed = _consume_from_pool(wallet_pool, sent_token, sent_amount, method)
                        for cl in consumed:
                            wallet_pool.append({
                                'lot_id': cl['lot_id'],
                                'date': cl['date'],
                                'symbol': recv_token.upper(),
                                'volume': cl['volume_used'],
                                'price': cl['price'],
                                'total': cl['volume_used'] * cl['price'],
                                'source': cl['source'],
                            })
                continue

            # Consume sold side from pool (unless skipped for reconciliation)
            if not skip_trade_consumption:
                for item in sent:
                    token = item.get('token', '')
                    amount = _parse_amount(item)
                    if amount <= 0 or not token:
                        continue
                    if token.upper() == 'USD':
                        continue  # fiat is special-cased; not lot-tracked
                    source_pool = pools.get(wallet_addr, [])
                    _consume_from_pool(source_pool, token, amount, method)

            # Add received side as new lots
            for ii, item in enumerate(received):
                token = item.get('token', '')
                amount = _parse_amount(item)
                if amount <= 0 or not token:
                    continue
                if token.upper() == 'USD':
                    continue  # fiat is special-cased; not lot-tracked
                total_usd = _parse_usd_value(item)
                price_per = total_usd / amount if amount > 0 else 0
                # Force stablecoins to $1.00
                if token.upper() in STABLECOINS:
                    price_per = 1.0
                    total_usd = amount
                pools.setdefault(wallet_addr, []).append({
                    'lot_id': next_lot_id('2025_trade'),
                    'date': tx_date,
                    'symbol': token.upper(),
                    'volume': amount,
                    'price': price_per,
                    'total': total_usd,
                    'source': '2025_trade',
                })

        elif tx_type == 'MINT':
            sent = details.get('sent', [])
            received = details.get('received', [])

            # Check if any sent item is classified as Payment or LP Deposit
            has_payment = False
            has_lp = False
            for idx, _ in enumerate(sent):
                item_key = f"{wid}_{ti}_{idx}"
                cl = classifications.get(item_key, '')
                if cl == 'Payment':
                    has_payment = True
                elif cl == 'LP Deposit':
                    has_lp = True

            if has_payment or has_lp:
                # Consume sent tokens from pool (unless skipped for reconciliation)
                if not skip_trade_consumption:
                    for item in sent:
                        token = item.get('token', '')
                        amount = _parse_amount(item)
                        if amount <= 0 or not token:
                            continue
                        source_pool = pools.get(wallet_addr, [])
                        _consume_from_pool(source_pool, token, amount, method)

                # Add received items as new lots
                for ii, item in enumerate(received):
                    token = item.get('token', '')
                    amount = _parse_amount(item)
                    if amount <= 0 or not token:
                        continue
                    total_usd = _parse_usd_value(item)
                    price_per = total_usd / amount if amount > 0 else 0
                    pools.setdefault(wallet_addr, []).append({
                        'lot_id': next_lot_id('2025_mint'),
                        'date': tx_date,
                        'symbol': token.upper(),
                        'volume': amount,
                        'price': price_per,
                        'total': total_usd,
                        'source': '2025_mint',
                    })

    return pools


def lifo_match_from_pool(pool_lots, sold_token, sold_amount, method='LIFO', consume=False):
    """Match lots from a pre-built virtual pool for a sold token using LIFO or FIFO.

    pool_lots: list of lot dicts from the wallet's virtual pool
    consume: if True, mutate pool_lots in-place (reduce volumes, remove exhausted lots)
    Returns (matched_lots, remaining_amount, warning).
    matched_lots: [{'lot_index': str, 'volume_used': float, 'cost_basis': float,
                     'date_acquired': str, 'price': float, 'source': str}]
    """
    symbol_upper = sold_token.upper()
    # Gather matching lots with their pool indices
    matching = [(i, lot) for i, lot in enumerate(pool_lots) if lot['symbol'].upper() == symbol_upper]
    # Sort by date: LIFO = newest first, FIFO = oldest first
    matching.sort(key=lambda x: x[1]['date'], reverse=(method == 'LIFO'))

    matched = []
    remaining = sold_amount
    dust_tolerance = max(1e-9, sold_amount * 0.0000001)
    indices_to_remove = []

    for idx, lot in matching:
        if remaining <= dust_tolerance:
            break
        vol_to_use = min(lot['volume'], remaining)
        cost_basis = vol_to_use * lot['price']
        matched.append({
            'lot_index': lot['lot_id'],
            'volume_used': vol_to_use,
            'cost_basis': cost_basis,
            'date_acquired': lot['date'],
            'price': lot['price'],
            'source': lot.get('source', 'opening'),
        })
        remaining -= vol_to_use
        if consume:
            lot['volume'] -= vol_to_use
            if lot['volume'] <= dust_tolerance:
                indices_to_remove.append(idx)

    if consume:
        for idx in sorted(indices_to_remove, reverse=True):
            pool_lots.pop(idx)

    warning = None
    if remaining > dust_tolerance:
        warning = f"Insufficient lots: {remaining:.6f} {sold_token} unmatched. Update account names or add more lots."

    return matched, remaining, warning


def lifo_match(positions, sold_token, sold_amount, wallet_address, lots_2025=None, method='LIFO', consumed=None):
    """Match opening position lots and 2025 lots for a sold token using LIFO or FIFO.

    consumed: dict mapping lot_index -> volume already consumed by prior trades.
              Used by _run_reconcile_all to track lot usage across trades.

    Returns (matched_lots, remaining_amount, warning).
    matched_lots: [{'lot_index': int/str, 'volume_used': float, 'cost_basis': float,
                     'date_acquired': str, 'price': float, 'source': str}]
    """
    if consumed is None:
        consumed = {}

    # Find opening position lots for the sold token where account matches
    matching_lots = []
    for i, p in enumerate(positions):
        if p['symbol'].upper() != sold_token.upper():
            continue
        account = p.get('account', '')
        if account.lower() == wallet_address.lower():
            try:
                volume = float(p.get('volume', 0))
                price = float(p.get('price', 0))
                total = float(p.get('total', 0))
            except (ValueError, TypeError):
                continue
            # Subtract already consumed volume
            available = volume - consumed.get(i, 0)
            if available <= 0.000001:
                continue
            volume = available
            # If price is 0 but total exists, compute effective price
            effective_price = price if price > 0 else (total / volume if volume > 0 else 0)
            matching_lots.append({
                'lot_index': i,
                'date': p['date'],
                'volume': volume,
                'price': effective_price,
                'total': total,
                'source': 'opening',
            })

    # Add 2025 lots for the sold token (account always matches since it's the wallet address)
    if lots_2025:
        for lot in lots_2025:
            if lot['symbol'].upper() != sold_token.upper():
                continue
            lot_id = f"2025_{lot['tx_index']}_{lot['item_index']}"
            available = lot['volume'] - consumed.get(lot_id, 0)
            if available <= 0.000001:
                continue
            matching_lots.append({
                'lot_index': lot_id,
                'date': lot['date'],
                'volume': available,
                'price': lot['price'],
                'total': lot['total'],
                'source': lot['source'],
            })

    # Sort by date: LIFO = newest first, FIFO = oldest first
    matching_lots.sort(key=lambda x: x['date'], reverse=(method == 'LIFO'))

    matched = []
    remaining = sold_amount
    warning = None

    for lot in matching_lots:
        if remaining <= 0:
            break
        vol_to_use = min(lot['volume'], remaining)
        cost_basis = vol_to_use * lot['price']
        matched.append({
            'lot_index': lot['lot_index'],
            'volume_used': vol_to_use,
            'cost_basis': cost_basis,
            'date_acquired': lot['date'],
            'price': lot['price'],
            'source': lot.get('source', 'opening'),
        })
        remaining -= vol_to_use

    if remaining > 0.000001:  # small tolerance for floating point
        warning = f"Insufficient lots: {remaining:.6f} {sold_token} unmatched. Update account names or add more lots."

    return matched, remaining, warning


def determine_term(date_acquired_str, date_sold_str):
    """Determine if a holding is long-term (>1 year) or short-term."""
    from datetime import datetime
    try:
        # Parse various date formats
        for fmt in ('%Y-%m-%d %H:%M:%S %z', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%fZ',
                    '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S'):
            try:
                acquired = datetime.strptime(date_acquired_str.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return 'short'

        for fmt in ('%Y-%m-%d %H:%M:%S %z', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%fZ',
                    '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S'):
            try:
                sold = datetime.strptime(date_sold_str.strip(), fmt)
                break
            except ValueError:
                continue
        else:
            return 'short'

        # Make both naive for comparison
        acquired = acquired.replace(tzinfo=None)
        sold = sold.replace(tzinfo=None)

        diff = sold - acquired
        if diff.days > 365:
            return 'long'
        return 'short'
    except Exception:
        return 'short'


@app.route('/reconcile')
def reconcile():
    state = ensure_state()
    transactions = state.get('transactions', {})
    reconciliations = state.get('reconciliations', {})
    classifications = state.get('classifications', {})
    wallet_list = state.get('wallets', [])
    wallet_map = {w['id']: w for w in wallet_list}

    trades = []
    for wid, txs in transactions.items():
        wallet = wallet_map.get(wid)
        if not wallet:
            continue
        for i, tx in enumerate(txs):
            tx_type = tx.get('type', '').upper()
            # Include TRADEs and MINTs with Payment classification
            if tx_type == 'TRADE':
                _sent = tx.get('parsed_details', {}).get('sent', [])
                _sold = _sent[0].get('token', '').upper() if _sent else ''
                # Skip Buys (USD-on-sent) and Migrations (non-taxable rebrand)
                if _sold == 'USD':
                    continue
                if any(classifications.get(f"{wid}_{i}_{idx}") == 'Migration' for idx in range(len(_sent))):
                    continue
            elif tx_type == 'MINT':
                # Check if any sent item is classified as Payment or LP Deposit
                has_payment = False
                has_lp = False
                for item_idx, _ in enumerate(tx.get('parsed_details', {}).get('sent', [])):
                    item_key = f"{wid}_{i}_{item_idx}"
                    cl = classifications.get(item_key, '')
                    if cl == 'Payment':
                        has_payment = True
                    elif cl == 'LP Deposit':
                        has_lp = True
                if not has_payment and not has_lp:
                    continue
            else:
                continue
            details = tx.get('parsed_details', {})
            sent = details.get('sent', [])
            received = details.get('received', [])

            # For Payment/LP MINTs
            if tx_type == 'MINT':
                if has_lp:
                    # LP Deposit: multiple tokens sent, NFT received
                    # Each sent token is a separate disposal; show first token for list
                    sold_token = sent[0].get('token', '') if sent else ''
                    sold_amount = format_amount(sent[0].get('amount', '')) if sent else ''
                    # Show all sent tokens in description
                    if len(sent) > 1:
                        sold_token = ' + '.join(s.get('token', '') for s in sent)
                        sold_amount = ''  # too complex for one number

                    nft_item = next((r for r in received if r.get('is_nft')), None)
                    recv_amount = format_amount(nft_item.get('amount', '')) if nft_item else ''
                    recv_token = nft_item.get('token', '') if nft_item else ''

                    # Proceeds = sum of ALL sent items
                    try:
                        proceeds = sum(abs(float(s.get('usd_value', '0').replace('$','').replace(',',''))) for s in sent)
                    except (ValueError, TypeError):
                        proceeds = 0
                else:
                    # Payment: net same-token refunds
                    sold_token = sent[0].get('token', '') if sent else ''
                    try:
                        total_sent = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token)
                        total_refund = sum(abs(float(r.get('amount', 0))) for r in received if r.get('token') == sold_token)
                    except (ValueError, TypeError):
                        total_sent = 0
                        total_refund = 0
                    net_amount = total_sent - total_refund
                    sold_amount = format_amount(net_amount)

                    nft_item = next((r for r in received if r.get('is_nft') or r.get('token') != sold_token), None)
                    recv_amount = format_amount(nft_item.get('amount', '')) if nft_item else ''
                    recv_token = nft_item.get('token', '') if nft_item else ''

                    try:
                        sent_usd = sum(abs(float(s.get('usd_value', '0').replace('$','').replace(',',''))) for s in sent if s.get('token') == sold_token)
                        refund_usd = sum(abs(float(r.get('usd_value', '0').replace('$','').replace(',',''))) for r in received if r.get('token') == sold_token)
                    except (ValueError, TypeError):
                        sent_usd = 0
                        refund_usd = 0
                    proceeds = sent_usd - refund_usd
            else:
                # Sold info — sum all sent items of the same token
                sold_amount = ''
                sold_token = ''
                if sent:
                    sold_token = sent[0].get('token', '')
                    try:
                        total_sold = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token)
                    except (ValueError, TypeError):
                        total_sold = abs(float(sent[0].get('amount', 0)))
                    sold_amount = format_amount(total_sold)

                # Received info
                recv_amount = ''
                recv_token = ''
                if received:
                    recv_amount = format_amount(received[0].get('amount', ''))
                    recv_token = received[0].get('token', '')

                proceeds = get_trade_proceeds(tx)
            recon_key = f"{wid}_{i}"
            status = 'Matched' if recon_key in reconciliations else 'Unmatched'

            trades.append({
                'wallet_id': wid,
                'tx_index': i,
                'date': format_date(tx.get('date', '')),
                'wallet_label': wallet['label'],
                'sold_amount': sold_amount,
                'sold_token': sold_token,
                'recv_amount': recv_amount,
                'recv_token': recv_token,
                'proceeds': format_usd(proceeds),
                'status': status,
            })

    # Sort by date descending
    trades.sort(key=lambda x: x['date'])

    matched_count = sum(1 for t in trades if t['status'] == 'Matched')

    return render_template('reconcile.html', trades=trades, matched_count=matched_count,
                           total_count=len(trades), cost_basis_method=state.get('cost_basis_method', 'LIFO'),
                           active_nav='reconcile')


def _build_pool_for_trade(state, method, target_wallet_id, target_tx_index):
    """Build the virtual lot pool for a specific wallet at the point of a specific trade.

    Builds pools with skip_trade_consumption=True, then consumes all trades that
    chronologically precede the target trade. Returns the wallet's pool ready for matching.
    """
    transactions = state.get('transactions', {})
    classifications = state.get('classifications', {})

    # Get the target trade's date for ordering
    target_txs = transactions.get(target_wallet_id, [])
    if target_tx_index < len(target_txs):
        target_date = target_txs[target_tx_index].get('date', '')
    else:
        target_date = ''

    # Build pools without trade consumption, up to and including the target trade's date
    recon_pools = build_wallet_lot_pools(state, method, up_to_date=target_date, skip_trade_consumption=True)

    # Collect all trades (same logic as _run_reconcile_all) and consume those before target
    all_trades = []
    for wid, txs in transactions.items():
        wallet = next((w for w in state['wallets'] if w['id'] == wid), None)
        if not wallet:
            continue
        for i, tx in enumerate(txs):
            tx_type = tx.get('type', '').upper()
            details = tx.get('parsed_details', {})
            sent = details.get('sent', [])
            received = details.get('received', [])

            if tx_type == 'TRADE':
                sold_token = sent[0].get('token', '') if sent else ''
                try:
                    sold_amount = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token) if sent else 0
                except (ValueError, TypeError):
                    continue
            elif tx_type == 'MINT':
                has_payment = any(classifications.get(f"{wid}_{i}_{idx}") in ('Payment', 'LP Deposit')
                                  for idx, _ in enumerate(sent))
                if not has_payment:
                    continue
                sold_token = sent[0].get('token', '') if sent else ''
                try:
                    total_sent = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token)
                    total_refund = sum(abs(float(r.get('amount', 0))) for r in received if r.get('token') == sold_token)
                except (ValueError, TypeError):
                    continue
                sold_amount = total_sent - total_refund
            else:
                continue

            if sold_amount <= 0:
                continue

            all_trades.append({
                'wid': wid,
                'tx_index': i,
                'wallet': wallet,
                'date': tx.get('date', ''),
                'sold_token': sold_token,
                'sold_amount': sold_amount,
            })

    all_trades.sort(key=lambda x: x['date'])

    # Consume all trades that come before the target trade
    for trade in all_trades:
        # Skip the target trade itself
        if trade['wid'] == target_wallet_id and trade['tx_index'] == target_tx_index:
            continue
        # Only consume trades that are chronologically before (or same date but different trade)
        if trade['date'] > target_date:
            continue
        wallet_addr = trade['wallet']['address'].lower()
        wallet_pool = recon_pools.get(wallet_addr, [])
        _consume_from_pool(wallet_pool, trade['sold_token'], trade['sold_amount'], method)

    target_wallet = next((w for w in state['wallets'] if w['id'] == target_wallet_id), None)
    if not target_wallet:
        return []
    return recon_pools.get(target_wallet['address'].lower(), [])


@app.route('/reconcile/<wallet_id>/<int:tx_index>')
def reconcile_detail(wallet_id, tx_index):
    state = ensure_state()
    wallet = next((w for w in state['wallets'] if w['id'] == wallet_id), None)
    if not wallet:
        return redirect(url_for('reconcile'))

    transactions = state.get('transactions', {}).get(wallet_id, [])
    if tx_index >= len(transactions):
        return redirect(url_for('reconcile'))

    tx = transactions[tx_index]
    if tx.get('type', '').upper() not in ('TRADE', 'MINT'):
        return redirect(url_for('reconcile'))

    details = tx.get('parsed_details', {})
    sent = details.get('sent', [])
    received = details.get('received', [])
    positions = state.get('positions', [])
    reconciliations = state.get('reconciliations', {})
    recon_key = f"{wallet_id}_{tx_index}"
    existing_recon = reconciliations.get(recon_key)

    # Trade info — handle Payment MINTs with netting
    tx_type = tx.get('type', '').upper()
    if tx_type == 'MINT':
        sold_token = sent[0].get('token', '') if sent else ''
        try:
            total_sent = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token)
            total_refund = sum(abs(float(r.get('amount', 0))) for r in received if r.get('token') == sold_token)
        except (ValueError, TypeError):
            total_sent = 0
            total_refund = 0
        sold_amount = total_sent - total_refund

        nft_item = next((r for r in received if r.get('is_nft') or r.get('token') != sold_token), None)
        recv_token = nft_item.get('token', '') if nft_item else ''
        recv_amount = abs(float(nft_item.get('amount', 0))) if nft_item else 0

        try:
            sent_usd = sum(abs(float(s.get('usd_value', '0').replace('$','').replace(',',''))) for s in sent if s.get('token') == sold_token)
            refund_usd = sum(abs(float(r.get('usd_value', '0').replace('$','').replace(',',''))) for r in received if r.get('token') == sold_token)
        except (ValueError, TypeError):
            sent_usd = 0
            refund_usd = 0
        proceeds = sent_usd - refund_usd
    else:
        sold_token = sent[0].get('token', '') if sent else ''
        try:
            sold_amount = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token) if sent else 0
        except (ValueError, TypeError):
            sold_amount = 0

        recv_token = received[0].get('token', '') if received else ''
        try:
            recv_amount = sum(abs(float(r.get('amount', 0))) for r in received if r.get('token') == recv_token) if received else 0
        except (ValueError, TypeError):
            recv_amount = 0

        proceeds = get_trade_proceeds(tx)

    # Build virtual lot pool and consume prior trades to get accurate available lots
    method = state.get('cost_basis_method', 'LIFO')
    wallet_pool = _build_pool_for_trade(state, method, wallet_id, tx_index)

    # All opening position lots for the sold token (for display)
    token_lots = []
    for i, p in enumerate(positions):
        if p['symbol'].upper() == sold_token.upper():
            try:
                vol = float(p.get('volume', 0))
                price = float(p.get('price', 0))
                total = float(p.get('total', 0))
            except (ValueError, TypeError):
                vol = price = total = 0
            token_lots.append({
                'lot_index': i,
                'date': p['date'],
                'account': p.get('account', ''),
                'volume': vol,
                'price': price,
                'total': total,
            })

    # Build 2025 acquired lots for display (non-transfer ones)
    all_2025_lots = build_2025_lots(state, wallet_id)
    token_2025_lots = [l for l in all_2025_lots if l['symbol'].upper() == sold_token.upper()]

    # Match from virtual pool
    matched_lots, remaining, warning = lifo_match_from_pool(wallet_pool, sold_token, sold_amount, method=method)
    matched_indices = {m['lot_index'] for m in matched_lots}

    # Calculate cost basis and gain/loss from LIFO match
    total_cost_basis = sum(m['cost_basis'] for m in matched_lots)
    gain_loss = proceeds - total_cost_basis

    # Determine term (use earliest matched lot for most conservative)
    trade_date = tx.get('date', '')
    if matched_lots:
        terms = [determine_term(m['date_acquired'], trade_date) for m in matched_lots]
        # If any lot is short-term, the whole thing is mixed; show per-lot
        has_long = 'long' in terms
        has_short = 'short' in terms
        if has_long and has_short:
            overall_term = 'mixed'
        elif has_long:
            overall_term = 'long'
        else:
            overall_term = 'short'
    else:
        overall_term = 'unknown'

    # If already reconciled, use saved data
    if existing_recon:
        saved_lots_used = {l['lot_index'] for l in existing_recon.get('lots_used', [])}
    else:
        saved_lots_used = None

    # Build matched indices separately for opening vs 2025 lots
    matched_opening_indices = {m['lot_index'] for m in matched_lots if m.get('source', 'opening') == 'opening'}
    matched_2025_ids = {m['lot_index'] for m in matched_lots if m.get('source', 'opening') != 'opening'}

    return render_template('reconcile_detail.html',
                           wallet=wallet,
                           tx=tx,
                           tx_index=tx_index,
                           trade_date=format_date(trade_date),
                           sold_token=sold_token,
                           sold_amount=sold_amount,
                           sold_amount_fmt=format_amount(sold_amount),
                           recv_token=recv_token,
                           recv_amount_fmt=format_amount(recv_amount),
                           proceeds=proceeds,
                           proceeds_fmt=format_usd(proceeds),
                           token_lots=token_lots,
                           token_2025_lots=token_2025_lots,
                           matched_lots=matched_lots,
                           matched_indices=matched_indices,
                           matched_opening_indices=matched_opening_indices,
                           matched_2025_ids=matched_2025_ids,
                           total_cost_basis=total_cost_basis,
                           cost_basis_fmt=format_usd(total_cost_basis),
                           gain_loss=gain_loss,
                           gain_loss_fmt=f"{'-' if gain_loss < 0 else '+'}{format_usd(abs(gain_loss))}",
                           is_gain=gain_loss >= 0,
                           overall_term=overall_term,
                           warning=warning,
                           remaining=remaining,
                           existing_recon=existing_recon,
                           saved_lots_used=saved_lots_used,
                           active_nav='reconcile')


@app.route('/reconcile/all', methods=['POST'])
def reconcile_all():
    """Auto-confirm all trades that have matching lots."""
    state = ensure_state()
    _run_reconcile_all(state)
    return redirect(url_for('reconcile'))


@app.route('/reconcile/confirm', methods=['POST'])
def reconcile_confirm():
    wallet_id = request.form.get('wallet_id', '')
    tx_index = request.form.get('tx_index', '')
    try:
        tx_index = int(tx_index)
    except (ValueError, TypeError):
        return redirect(url_for('reconcile'))

    state = ensure_state()
    transactions = state.get('transactions', {}).get(wallet_id, [])
    if tx_index >= len(transactions):
        return redirect(url_for('reconcile'))

    tx = transactions[tx_index]
    details = tx.get('parsed_details', {})
    sent = details.get('sent', [])
    received = details.get('received', [])
    tx_type = tx.get('type', '').upper()

    if tx_type == 'MINT':
        sold_token = sent[0].get('token', '') if sent else ''
        try:
            total_sent = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token)
            total_refund = sum(abs(float(r.get('amount', 0))) for r in received if r.get('token') == sold_token)
        except (ValueError, TypeError):
            total_sent = 0
            total_refund = 0
        sold_amount = total_sent - total_refund
        try:
            sent_usd = sum(abs(float(s.get('usd_value', '0').replace('$','').replace(',',''))) for s in sent if s.get('token') == sold_token)
            refund_usd = sum(abs(float(r.get('usd_value', '0').replace('$','').replace(',',''))) for r in received if r.get('token') == sold_token)
        except (ValueError, TypeError):
            sent_usd = 0
            refund_usd = 0
        proceeds = sent_usd - refund_usd
    else:
        sold_token = sent[0].get('token', '') if sent else ''
        sold_amount_raw = sent[0].get('amount', 0) if sent else 0
        try:
            sold_amount = abs(float(sold_amount_raw))
        except (ValueError, TypeError):
            sold_amount = 0
        proceeds = get_trade_proceeds(tx)

    wallet = next((w for w in state['wallets'] if w['id'] == wallet_id), None)
    if not wallet:
        return redirect(url_for('reconcile'))

    # Build virtual lot pool with prior trades consumed, then match
    method = state.get('cost_basis_method', 'LIFO')
    wallet_pool = _build_pool_for_trade(state, method, wallet_id, tx_index)
    matched_lots, remaining, warning = lifo_match_from_pool(wallet_pool, sold_token, sold_amount, method=method)

    total_cost_basis = sum(m['cost_basis'] for m in matched_lots)
    gain_loss = proceeds - total_cost_basis

    trade_date = tx.get('date', '')
    if matched_lots:
        terms = [determine_term(m['date_acquired'], trade_date) for m in matched_lots]
        has_long = 'long' in terms
        has_short = 'short' in terms
        if has_long and has_short:
            overall_term = 'mixed'
        elif has_long:
            overall_term = 'long'
        else:
            overall_term = 'short'
    else:
        overall_term = 'unknown'

    # Save reconciliation
    if 'reconciliations' not in state:
        state['reconciliations'] = {}

    recon_key = f"{wallet_id}_{tx_index}"
    state['reconciliations'][recon_key] = {
        'lots_used': [
            {
                'lot_index': m['lot_index'],
                'volume_used': m['volume_used'],
                'cost_basis': round(m['cost_basis'], 2),
                'date_acquired': m['date_acquired'],
                'source': m.get('source', 'opening'),
            }
            for m in matched_lots
        ],
        'proceeds': round(proceeds, 2),
        'gain_loss': round(gain_loss, 2),
        'term': overall_term,
        'status': 'matched',
    }

    save_state(state)
    return redirect(url_for('reconcile_detail', wallet_id=wallet_id, tx_index=tx_index))


@app.route('/reconcile/unconfirm', methods=['POST'])
def reconcile_unconfirm():
    wallet_id = request.form.get('wallet_id', '')
    tx_index = request.form.get('tx_index', '')
    try:
        tx_index = int(tx_index)
    except (ValueError, TypeError):
        return redirect(url_for('reconcile'))

    state = ensure_state()
    recon_key = f"{wallet_id}_{tx_index}"
    if 'reconciliations' in state:
        state['reconciliations'].pop(recon_key, None)
    save_state(state)
    return redirect(url_for('reconcile_detail', wallet_id=wallet_id, tx_index=tx_index))


@app.route('/positions/update-account', methods=['POST'])
def update_position_account():
    lot_index = request.form.get('lot_index', '')
    new_account = request.form.get('new_account', '').strip()
    redirect_url = request.form.get('redirect_url', '')

    try:
        lot_index = int(lot_index)
    except (ValueError, TypeError):
        return redirect(redirect_url or url_for('positions'))

    state = ensure_state()
    positions = state.get('positions', [])
    if 0 <= lot_index < len(positions):
        positions[lot_index]['account'] = new_account
        # Clear any reconciliations that might use this lot, since the account changed
        if 'reconciliations' in state:
            to_remove = []
            for key, recon in state['reconciliations'].items():
                for lot in recon.get('lots_used', []):
                    if lot['lot_index'] == lot_index:
                        to_remove.append(key)
                        break
            for key in to_remove:
                del state['reconciliations'][key]
        save_state(state)

    return redirect(redirect_url or url_for('positions'))


@app.route('/positions/delete', methods=['POST'])
def delete_position():
    lot_index = request.form.get('lot_index', '')
    redirect_url = request.form.get('redirect_url', '')

    try:
        lot_index = int(lot_index)
    except (ValueError, TypeError):
        return redirect(redirect_url or url_for('positions'))

    state = ensure_state()
    positions = state.get('positions', [])
    if 0 <= lot_index < len(positions):
        # Only clear reconciliations that used this specific lot
        if 'reconciliations' in state:
            to_remove = []
            for key, recon in state['reconciliations'].items():
                for lot in recon.get('lots_used', []):
                    if lot.get('lot_index') == lot_index:
                        to_remove.append(key)
                        break
            for key in to_remove:
                del state['reconciliations'][key]
            # Update lot indices in remaining reconciliations (shift down by 1 for lots above deleted index)
            for key, recon in state['reconciliations'].items():
                for lot in recon.get('lots_used', []):
                    if isinstance(lot.get('lot_index'), int) and lot['lot_index'] > lot_index:
                        lot['lot_index'] -= 1
        positions.pop(lot_index)
        state['positions'] = positions
        save_state(state)

    return redirect(redirect_url or url_for('positions'))


@app.route('/positions/update-field', methods=['POST'])
def update_position_field():
    """AJAX endpoint: update a single field on a position lot."""
    data = request.get_json(force=True)
    lot_index = data.get('lot_index')
    field = data.get('field', '')
    value = data.get('value', '')

    if field not in ('account', 'volume', 'price'):
        return jsonify({'error': 'Invalid field'}), 400

    try:
        lot_index = int(lot_index)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid lot_index'}), 400

    state = ensure_state()
    positions = state.get('positions', [])
    if not (0 <= lot_index < len(positions)):
        return jsonify({'error': 'Lot index out of range'}), 400

    positions[lot_index][field] = value

    # Recalculate total if volume or price changed
    if field in ('volume', 'price'):
        try:
            vol = float(positions[lot_index].get('volume', 0) or 0)
            prc = float(positions[lot_index].get('price', 0) or 0)
            positions[lot_index]['total'] = str(round(vol * prc, 10))
        except (ValueError, TypeError):
            pass

    # Clear affected reconciliations
    if 'reconciliations' in state:
        to_remove = []
        for key, recon in state['reconciliations'].items():
            for lot in recon.get('lots_used', []):
                if lot.get('lot_index') == lot_index:
                    to_remove.append(key)
                    break
        for key in to_remove:
            del state['reconciliations'][key]

    save_state(state)
    return jsonify({'ok': True, 'lot': positions[lot_index]})


@app.route('/positions/add', methods=['POST'])
def add_position():
    """AJAX endpoint: add a new opening position lot."""
    data = request.get_json(force=True)
    date = data.get('date', '').strip()
    symbol = data.get('symbol', '').strip()
    account = data.get('account', '').strip()
    volume = data.get('volume', '').strip()
    price = data.get('price', '').strip()

    if not symbol:
        return jsonify({'error': 'Symbol is required'}), 400

    try:
        vol = float(volume) if volume else 0
        prc = float(price) if price else 0
        total = str(round(vol * prc, 10))
    except (ValueError, TypeError):
        total = ''

    lot = {
        'date': date,
        'symbol': symbol.upper(),
        'account': account,
        'volume': volume,
        'price': price,
        'currency': 'USD',
        'fee': '',
        'fee_currency': '',
        'total': total,
        'memo': '',
    }

    state = ensure_state()
    if 'positions' not in state:
        state['positions'] = []
    state['positions'].append(lot)
    save_state(state)

    return jsonify({'ok': True, 'lot': lot, 'lot_index': len(state['positions']) - 1})


@app.route('/positions/delete-ajax', methods=['POST'])
def delete_position_ajax():
    """AJAX endpoint: delete a position lot and return JSON."""
    data = request.get_json(force=True)
    lot_index = data.get('lot_index')

    try:
        lot_index = int(lot_index)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid lot_index'}), 400

    state = ensure_state()
    positions = state.get('positions', [])
    if not (0 <= lot_index < len(positions)):
        return jsonify({'error': 'Lot index out of range'}), 400

    # Clear affected reconciliations
    if 'reconciliations' in state:
        to_remove = []
        for key, recon in state['reconciliations'].items():
            for lot in recon.get('lots_used', []):
                if lot.get('lot_index') == lot_index:
                    to_remove.append(key)
                    break
        for key in to_remove:
            del state['reconciliations'][key]
        for key, recon in state['reconciliations'].items():
            for lot in recon.get('lots_used', []):
                if isinstance(lot.get('lot_index'), int) and lot['lot_index'] > lot_index:
                    lot['lot_index'] -= 1

    positions.pop(lot_index)
    state['positions'] = positions
    save_state(state)

    return jsonify({'ok': True})


# ============ REPORTS (Phase 4) ============

_DATE_FORMATS = ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S',
                 '%Y-%m-%d %H:%M:%S %z', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d',
                 '%b-%d-%Y %H:%M', '%b-%d-%Y',
                 '%Y-%m-%d %H:%M:%S UTC')

def parse_date_to_dt(date_str):
    """Parse any known date string format into a datetime; returns datetime.min on failure."""
    if not date_str:
        return datetime.min
    date_str = date_str.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return datetime.min

def format_date_mmddyyyy(date_str):
    """Convert various date formats to MM/DD/YYYY."""
    if not date_str:
        return ''
    date_str = date_str.strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime('%m/%d/%Y')
        except ValueError:
            continue
    return date_str


def _shorten_token_label(s):
    """Collapse very long #NNNN...NNNN tokenIds in a token label to first 8 + ... + last 4."""
    if not s:
        return s
    return re.sub(r'#([0-9a-fA-F]{20,})', lambda m: f"#{m.group(1)[:8]}…{m.group(1)[-4:]}", s)


def build_form8949_rows(state):
    """Build Form 8949 rows from reconciliation data.

    Returns a list of dicts with: description, date_acquired, date_sold, proceeds, cost_basis, gain_loss, term
    If a trade used multiple lots with different acquisition dates, create separate rows per lot.
    """
    reconciliations = state.get('reconciliations', {})
    transactions = state.get('transactions', {})
    rows = []

    for recon_key, recon in reconciliations.items():
        parts = recon_key.split('_')
        wallet_id = parts[0]
        tx_index = int(parts[1])

        txs = transactions.get(wallet_id, [])
        if tx_index >= len(txs):
            continue
        tx = txs[tx_index]
        details = tx.get('parsed_details', {})
        sent = details.get('sent', [])
        date_sold = tx.get('date', '')

        sold_token = sent[0].get('token', '') if sent else ''
        lots_used = recon.get('lots_used', [])
        total_proceeds = recon.get('proceeds', 0)

        if len(lots_used) == 1:
            # Single lot - one row
            lot = lots_used[0]
            rows.append({
                'description': f"{lot['volume_used']:,.8f}".rstrip('0').rstrip('.') + f" {_shorten_token_label(sold_token)}",
                'date_acquired': format_date_mmddyyyy(lot['date_acquired']),
                'date_sold': format_date_mmddyyyy(date_sold),
                'proceeds': round(total_proceeds, 2),
                'cost_basis': round(lot['cost_basis'], 2),
                'gain_loss': round(total_proceeds - lot['cost_basis'], 2),
                'term': determine_term(lot['date_acquired'], date_sold),
                'sort_date': date_sold,
            })
        else:
            # Multiple lots - split proceeds proportionally by cost basis, one row per lot
            total_volume = sum(l['volume_used'] for l in lots_used)
            for lot in lots_used:
                # Allocate proceeds proportionally by volume
                if total_volume > 0:
                    lot_proceeds = total_proceeds * (lot['volume_used'] / total_volume)
                else:
                    lot_proceeds = 0
                lot_gain_loss = lot_proceeds - lot['cost_basis']
                rows.append({
                    'description': f"{lot['volume_used']:,.8f}".rstrip('0').rstrip('.') + f" {_shorten_token_label(sold_token)}",
                    'date_acquired': format_date_mmddyyyy(lot['date_acquired']),
                    'date_sold': format_date_mmddyyyy(date_sold),
                    'proceeds': round(lot_proceeds, 2),
                    'cost_basis': round(lot['cost_basis'], 2),
                    'gain_loss': round(lot_gain_loss, 2),
                    'term': determine_term(lot['date_acquired'], date_sold),
                    'sort_date': date_sold,
                })

    # Sort by date sold
    rows.sort(key=lambda x: parse_date_to_dt(x.get('sort_date', '')))
    return rows


def build_income_rows(state):
    """Build crypto income rows from classified transactions.

    Returns a list of dicts with: date, token, amount, fmv_usd, type
    """
    transactions = state.get('transactions', {})
    classifications = state.get('classifications', {})
    rows = []

    for cls_key, cls_value in classifications.items():
        if cls_value not in ('Income', 'Airdrop'):
            continue
        parts = cls_key.split('_')
        if len(parts) < 3:
            continue
        wallet_id = parts[0]
        tx_index = int(parts[1])
        item_index = int(parts[2])

        txs = transactions.get(wallet_id, [])
        if tx_index >= len(txs):
            continue
        tx = txs[tx_index]
        details = tx.get('parsed_details', {})
        received = details.get('received', [])
        if item_index >= len(received):
            continue

        item = received[item_index]
        token = item.get('token', '')
        amount_raw = item.get('amount', '0')
        try:
            amount = abs(float(amount_raw))
        except (ValueError, TypeError):
            amount = 0

        usd_val = item.get('usd_value', '') or item.get('pretty_usd', '')
        if isinstance(usd_val, str):
            usd_val = usd_val.replace('$', '').replace(',', '').strip()
        try:
            fmv = abs(float(usd_val)) if usd_val else 0
        except (ValueError, TypeError):
            fmv = 0

        rows.append({
            'date': format_date_mmddyyyy(tx.get('date', '')),
            'token': token,
            'amount': amount,
            'fmv_usd': round(fmv, 2),
            'type': cls_value,
            'sort_date': tx.get('date', ''),
        })

    rows.sort(key=lambda x: parse_date_to_dt(x.get('sort_date', '')))
    return rows


@app.route('/settings/cost-basis-method', methods=['POST'])
def set_cost_basis_method():
    method = request.form.get('method', 'LIFO')
    if method not in ('LIFO', 'FIFO'):
        method = 'LIFO'
    state = ensure_state()
    state['cost_basis_method'] = method
    # Recalculate all reconciliations with new method
    state['reconciliations'] = {}
    save_state(state)
    _run_reconcile_all(state)
    return redirect(url_for('reconcile'))


def _process_transfers(state):
    """Disabled: transfers are now handled by the virtual lot pool system (build_wallet_lot_pools)."""
    pass


def _run_reconcile_all(state):
    """Shared logic for auto-reconciling all trades using virtual lot pools.

    Builds virtual lot pools for all wallets, then processes trades chronologically.
    The pool already accounts for transfers carrying cost basis between wallets.
    """
    transactions = state.get('transactions', {})
    classifications = state.get('classifications', {})
    reconciliations = state.get('reconciliations', {})
    method = state.get('cost_basis_method', 'LIFO')

    # Collect all reconcilable trades across wallets
    all_trades = []
    for wid, txs in transactions.items():
        wallet = next((w for w in state['wallets'] if w['id'] == wid), None)
        if not wallet:
            continue
        for i, tx in enumerate(txs):
            recon_key = f"{wid}_{i}"
            if recon_key in reconciliations:
                continue

            tx_type = tx.get('type', '').upper()
            details = tx.get('parsed_details', {})
            sent = details.get('sent', [])
            received = details.get('received', [])

            if tx_type == 'TRADE':
                sold_token = sent[0].get('token', '') if sent else ''
                # Skip Buys (USD on sent side) and Migrations (non-taxable rebrand)
                if sold_token.upper() == 'USD':
                    continue
                if any(classifications.get(f"{wid}_{i}_{idx}") == 'Migration' for idx in range(len(sent))):
                    continue
                try:
                    sold_amount = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token) if sent else 0
                except (ValueError, TypeError):
                    continue
                proceeds = get_trade_proceeds(tx)
            elif tx_type == 'MINT':
                has_payment = any(classifications.get(f"{wid}_{i}_{idx}") in ('Payment', 'LP Deposit')
                                  for idx, _ in enumerate(sent))
                if not has_payment:
                    continue
                sold_token = sent[0].get('token', '') if sent else ''
                try:
                    total_sent = sum(abs(float(s.get('amount', 0))) for s in sent if s.get('token') == sold_token)
                    total_refund = sum(abs(float(r.get('amount', 0))) for r in received if r.get('token') == sold_token)
                except (ValueError, TypeError):
                    continue
                sold_amount = total_sent - total_refund
                try:
                    sent_usd = sum(abs(float(s.get('usd_value', '0').replace('$','').replace(',',''))) for s in sent if s.get('token') == sold_token)
                    refund_usd = sum(abs(float(r.get('usd_value', '0').replace('$','').replace(',',''))) for r in received if r.get('token') == sold_token)
                except (ValueError, TypeError):
                    continue
                proceeds = sent_usd - refund_usd
            else:
                continue

            if sold_amount <= 0:
                continue

            all_trades.append({
                'wid': wid,
                'tx_index': i,
                'recon_key': recon_key,
                'wallet': wallet,
                'date': tx.get('date', ''),
                'sold_token': sold_token,
                'sold_amount': sold_amount,
                'proceeds': proceeds,
            })

    # Sort trades chronologically so lot consumption is in order
    all_trades.sort(key=lambda x: x['date'])

    # Build pools fresh for reconciliation — skip trade sold-side consumption
    # so _run_reconcile_all can consume them in chronological order
    recon_pools = build_wallet_lot_pools(state, method, skip_trade_consumption=True)

    for trade in all_trades:
        wallet_addr = trade['wallet']['address'].lower()
        wallet_pool = recon_pools.get(wallet_addr, [])

        matched_lots, remaining, warning = lifo_match_from_pool(
            wallet_pool, trade['sold_token'], trade['sold_amount'], method=method, consume=True)

        dust_tol = max(1e-9, trade['sold_amount'] * 0.0000001)
        if matched_lots and remaining < dust_tol:
            total_cost_basis = sum(m['cost_basis'] for m in matched_lots)
            gain_loss = trade['proceeds'] - total_cost_basis
            terms = [determine_term(m['date_acquired'], trade['date']) for m in matched_lots]
            if all(t == 'long' for t in terms):
                overall_term = 'long'
            elif all(t == 'short' for t in terms):
                overall_term = 'short'
            else:
                overall_term = 'mixed'

            reconciliations[trade['recon_key']] = {
                'lots_used': matched_lots,
                'proceeds': trade['proceeds'],
                'gain_loss': gain_loss,
                'term': overall_term,
                'status': 'matched',
            }

    state['reconciliations'] = reconciliations
    save_state(state)

@app.route('/reports')
def reports():
    state = ensure_state()
    form8949_rows = build_form8949_rows(state)
    income_rows = build_income_rows(state)

    # Summary stats
    total_proceeds = sum(r['proceeds'] for r in form8949_rows)
    total_cost_basis = sum(r['cost_basis'] for r in form8949_rows)
    total_gain_loss = sum(r['gain_loss'] for r in form8949_rows)
    short_term_gl = sum(r['gain_loss'] for r in form8949_rows if r['term'] == 'short')
    long_term_gl = sum(r['gain_loss'] for r in form8949_rows if r['term'] == 'long')
    total_income = sum(r['fmv_usd'] for r in income_rows)

    return render_template('reports.html',
                           form8949_rows=form8949_rows,
                           income_rows=income_rows,
                           total_proceeds=total_proceeds,
                           total_cost_basis=total_cost_basis,
                           total_gain_loss=total_gain_loss,
                           short_term_gl=short_term_gl,
                           long_term_gl=long_term_gl,
                           total_income=total_income,
                           cost_basis_method=state.get('cost_basis_method', 'LIFO'),
                           active_nav='reports')


@app.route('/reports/form8949-csv')
def form8949_csv():
    state = ensure_state()
    rows = build_form8949_rows(state)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([f'Crypto Form 8949 - {state.get("tax_year", 2025)}'])
    writer.writerow(['Form 8949 Statement'])
    # Separate short-term and long-term
    short_rows = [r for r in rows if r['term'] == 'short']
    long_rows = [r for r in rows if r['term'] != 'short']
    header = ['Description (a)', 'Date Acquired(b)', 'Date Sold (c)', 'Proceeds (d)',
              'Cost Basis(e)', 'Adjustment Code (f)', 'Adjustment amount(g)', 'Gain or loss(h)']
    if short_rows:
        writer.writerow([])
        writer.writerow(['Part I (Short-Term)'])
        writer.writerow(header)
        for r in short_rows:
            writer.writerow([r['description'], r['date_acquired'], r['date_sold'],
                             f"{r['proceeds']:.2f}", f"{r['cost_basis']:.2f}", '', '', f"{r['gain_loss']:.2f}"])
        st_proceeds = sum(r['proceeds'] for r in short_rows)
        st_basis = sum(r['cost_basis'] for r in short_rows)
        st_gl = sum(r['gain_loss'] for r in short_rows)
        writer.writerow(['Totals', '', '', f"{st_proceeds:.2f}", f"{st_basis:.2f}", '', '0', f"{st_gl:.2f}"])
    if long_rows:
        writer.writerow([])
        writer.writerow(['Part II (Long-Term)'])
        writer.writerow(header)
        for r in long_rows:
            writer.writerow([r['description'], r['date_acquired'], r['date_sold'],
                             f"{r['proceeds']:.2f}", f"{r['cost_basis']:.2f}", '', '', f"{r['gain_loss']:.2f}"])
        lt_proceeds = sum(r['proceeds'] for r in long_rows)
        lt_basis = sum(r['cost_basis'] for r in long_rows)
        lt_gl = sum(r['gain_loss'] for r in long_rows)
        writer.writerow(['Totals', '', '', f"{lt_proceeds:.2f}", f"{lt_basis:.2f}", '', '0', f"{lt_gl:.2f}"])

    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=form8949.csv'}
    )


def encode_pdf_field_name(name):
    """Encode a field name to UTF-16 for matching against PDF form field names.
    Returns the pdfrw-style string representation like '<FEFF00660031...>'."""
    utf16_bytes = name.encode('utf-16-be')
    hex_str = 'FEFF' + utf16_bytes.hex().upper()
    return f'<{hex_str}>'


def fill_pdf_field(page, field_name_str, value, is_checkbox=False):
    """Find and fill a field on a PDF page by its decoded name string.

    field_name_str: e.g. 'f1_03[0]' or 'c1_1[2]'
    """
    target = encode_pdf_field_name(field_name_str)
    annots = page['/Annots'] if '/Annots' in page else None
    if not annots:
        return False
    for annot in annots:
        raw_name = str(annot['/T']) if '/T' in annot else ''
        if raw_name == target:
            if is_checkbox:
                # Each checkbox has its own appearance state name
                # Discover it from the /AP/N dictionary
                ap = annot['/AP'] if '/AP' in annot else None
                check_val = '1'  # default
                if ap:
                    ap_n = ap['/N'] if '/N' in ap else None
                    if ap_n:
                        # Get the non-Off state name
                        for key in ap_n.keys():
                            if key != '/Off':
                                check_val = key.lstrip('/')
                                break
                annot.update(pdfrw.PdfDict(
                    V=pdfrw.PdfName(check_val),
                    AS=pdfrw.PdfName(check_val),
                ))
            else:
                annot.update(pdfrw.PdfDict(
                    V=pdfrw.PdfString.encode(str(value)),
                    AP='',
                ))
            return True
    return False


def build_form8949_pages(template_path, rows, page_prefix, checkbox_index, name_value, ssn_value):
    """Build filled Form 8949 pages for a set of rows (short-term or long-term).

    page_prefix: '1' for Part I (short-term), '2' for Part II (long-term)
    checkbox_index: which checkbox to check (2 for C/F - not reported on 1099-B)
    Returns a list of filled PDF pages.
    """
    if not rows:
        return []

    ROWS_PER_PAGE = 11
    pages = []

    # Split rows into chunks of ROWS_PER_PAGE
    chunks = [rows[i:i + ROWS_PER_PAGE] for i in range(0, len(rows), ROWS_PER_PAGE)]

    for chunk_idx, chunk in enumerate(chunks):
        # Read a fresh copy of the template for each page
        template = pdfrw.PdfReader(template_path)
        # Page 1 (index 0) is Part I (short-term), Page 2 (index 1) is Part II (long-term)
        page_index = 0 if page_prefix == '1' else 1
        page = template.pages[page_index]

        # Set NeedAppearances so PDF readers regenerate field appearances
        if template.Root.AcroForm:
            template.Root.AcroForm.update(pdfrw.PdfDict(NeedAppearances=pdfrw.PdfObject('true')))

        # Fill name and SSN fields
        fill_pdf_field(page, f'f{page_prefix}_01[0]', name_value)
        fill_pdf_field(page, f'f{page_prefix}_02[0]', ssn_value)

        # Check the appropriate box (C for short-term, F for long-term)
        fill_pdf_field(page, f'c{page_prefix}_1[{checkbox_index}]', '', is_checkbox=True)

        # Fill data rows
        # Each row uses 8 consecutive fields starting from f{p}_03
        # Row 0: f{p}_03 to f{p}_10, Row 1: f{p}_11 to f{p}_18, etc.
        for row_idx, row in enumerate(chunk):
            base_field_num = 3 + (row_idx * 8)  # f{p}_03, f{p}_11, f{p}_19, ...
            col_values = [
                row['description'],                           # (a) Description
                row['date_acquired'],                          # (b) Date acquired
                row['date_sold'],                              # (c) Date sold
                f"{row['proceeds']:.2f}",                      # (d) Proceeds
                f"{row['cost_basis']:.2f}",                    # (e) Cost basis
                '',                                            # (f) Adjustment code
                '',                                            # (g) Adjustment amount
                f"{row['gain_loss']:.2f}",                     # (h) Gain or loss
            ]
            for col_idx, val in enumerate(col_values):
                field_num = base_field_num + col_idx
                field_name = f'f{page_prefix}_{field_num:02d}[0]'
                fill_pdf_field(page, field_name, val)

        # Fill totals row (last row on the form)
        # Totals fields: f{p}_91 through f{p}_95 correspond to columns (d), (e), (f), (g), (h)
        total_proceeds = sum(r['proceeds'] for r in chunk)
        total_cost_basis = sum(r['cost_basis'] for r in chunk)
        total_gain_loss = sum(r['gain_loss'] for r in chunk)

        fill_pdf_field(page, f'f{page_prefix}_91[0]', f"{total_proceeds:.2f}")
        fill_pdf_field(page, f'f{page_prefix}_92[0]', f"{total_cost_basis:.2f}")
        fill_pdf_field(page, f'f{page_prefix}_93[0]', '')       # adjustment code
        fill_pdf_field(page, f'f{page_prefix}_94[0]', '0.00')   # adjustment amount
        fill_pdf_field(page, f'f{page_prefix}_95[0]', f"{total_gain_loss:.2f}")

        pages.append(page)

    return pages


@app.route('/reports/form8949-pdf')
def form8949_pdf():
    state = ensure_state()
    rows = build_form8949_rows(state)

    short_term = [r for r in rows if r['term'] == 'short']
    long_term = [r for r in rows if r['term'] == 'long']

    tax_year = state.get('tax_year', DEFAULT_TAX_YEAR)
    template_path = os.path.join(os.path.dirname(__file__), 'data', 'f8949.pdf')

    # Get name and SSN from query params (not stored)
    name_value = request.args.get('name', '').strip()
    ssn_value = request.args.get('ssn', '').strip()

    # Build pages for short-term (Part I, page 1 template, check box C = index 2)
    # and long-term (Part II, page 2 template, check box F = index 2)
    short_pages = build_form8949_pages(template_path, short_term, '1', 2, name_value, ssn_value)
    long_pages = build_form8949_pages(template_path, long_term, '2', 2, name_value, ssn_value)

    # Build the output PDF with all pages
    writer = pdfrw.PdfWriter()

    for pg in short_pages:
        writer.addpage(pg)
    for pg in long_pages:
        writer.addpage(pg)

    # If no rows at all, return a blank form
    if not short_pages and not long_pages:
        template = pdfrw.PdfReader(template_path)
        for pg in template.pages:
            writer.addpage(pg)

    # Set NeedAppearances on the merged AcroForm
    # We need to create an AcroForm for the writer
    # Read one template to get AcroForm settings
    if short_pages or long_pages:
        # Collect all annotations from all pages for the AcroForm Fields
        all_fields = []
        for pg in writer.pagearray:
            annots = pg['/Annots'] if '/Annots' in pg else None
            if annots:
                all_fields.extend(annots)
        writer.trailer.Root.AcroForm = pdfrw.PdfDict(
            Fields=all_fields,
            NeedAppearances=pdfrw.PdfObject('true'),
        )

    # Write to bytes buffer
    output = io.BytesIO()
    writer.write(output)
    output.seek(0)

    return Response(
        output.getvalue(),
        mimetype='application/pdf',
        headers={
            'Content-Disposition': f'attachment; filename=form8949_{tax_year}.pdf',
        },
    )


@app.route('/reports/income-csv')
def income_csv():
    state = ensure_state()
    rows = build_income_rows(state)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Token', 'Amount', 'Fair Market Value (USD)', 'Type'])
    for r in rows:
        writer.writerow([r['date'], r['token'], r['amount'], f"{r['fmv_usd']:.2f}", r['type']])
    total_fmv = sum(r['fmv_usd'] for r in rows)
    writer.writerow(['TOTAL', '', '', f"{total_fmv:.2f}", ''])

    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=crypto_income.csv'}
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
