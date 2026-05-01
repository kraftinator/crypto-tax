import os
import csv
import json
import time
import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify

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
    """Save state to state.json."""
    os.makedirs(os.path.dirname(app.config['DATA_FILE']), exist_ok=True)
    with open(app.config['DATA_FILE'], 'w') as f:
        json.dump(data, f, indent=2)

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

def parse_wallet_csv(filepath):
    """Parse a Chain Glance wallet CSV."""
    transactions = []
    with open(filepath, 'r', newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            cleaned = {k.strip(): v.strip() if v else '' for k, v in row.items()}
            blockchain = cleaned.get('Blockchain', '')
            # Filter: only ETH and Base chains
            if blockchain.lower() not in ('eth', 'base', 'ethereum'):
                continue
            # Filter out non-taxable transaction types
            tx_type = cleaned.get('Type', '').upper()
            if tx_type in ('APPROVE', 'EXECUTE'):
                continue
            tx = {
                'date': cleaned.get('Date', ''),
                'account': cleaned.get('Account', ''),
                'blockchain': blockchain,
                'type': cleaned.get('Type', ''),
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

                    item = {
                        'amount': amount,
                        'token': currency,
                        'token_name': token_name,
                        'usd_value': native_amount,
                        'pretty_usd': pretty,
                    }

                    try:
                        amt = float(amount)
                    except (ValueError, TypeError):
                        amt = 0

                    if is_fee:
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


def fill_missing_usd_values(transactions, state):
    """Scan transactions for missing USD values and look them up via CoinGecko."""
    price_cache = state.get('price_cache', {})
    lookups_made = 0

    for tx in transactions:
        details = tx.get('parsed_details', {})
        date_str = tx.get('date', '')
        cg_date, cache_date = parse_tx_date_for_coingecko(date_str)
        if not cg_date or not cache_date:
            continue

        for category in ('sent', 'received', 'fees'):
            for item in details.get(category, []):
                # Check if USD value is missing
                usd_val = item.get('usd_value', '')
                if isinstance(usd_val, str):
                    usd_val = usd_val.replace('$', '').replace(',', '').strip()
                try:
                    usd_float = abs(float(usd_val)) if usd_val else 0
                except (ValueError, TypeError):
                    usd_float = 0

                if usd_float != 0:
                    continue  # Already has a value

                token = item.get('token', '')
                if not token:
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

                if cache_key in price_cache:
                    price = price_cache[cache_key]
                    if price is None:
                        continue  # Previously failed, skip
                    print(f"  [CoinGecko] Cache hit: {token} on {cache_date} = ${price}")
                else:
                    # Rate limit: delay between API calls
                    if lookups_made > 0:
                        time.sleep(1)
                    print(f"  [CoinGecko] Looking up {token} ({coingecko_id}) on {cg_date}...")
                    price = fetch_coingecko_price(coingecko_id, cg_date)
                    lookups_made += 1
                    # Cache the result (even None for failures)
                    price_cache[cache_key] = price
                    if price is not None:
                        print(f"  [CoinGecko] Found: ${price} per {token}")
                    else:
                        print(f"  [CoinGecko] No price data for {token} on {cg_date}")
                        continue

                # Calculate USD value and update the item
                usd_value = price * amount
                item['usd_value'] = str(usd_value)
                item['pretty_usd'] = f"${usd_value:,.2f}"
                print(f"  [CoinGecko] Set {amount} {token} = ${usd_value:,.2f}")

    # Save the cache back to state
    state['price_cache'] = price_cache
    if lookups_made > 0:
        print(f"  [CoinGecko] Done. Made {lookups_made} API call(s).")
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
    """Remove state and allow re-upload."""
    if os.path.exists(app.config['DATA_FILE']):
        os.remove(app.config['DATA_FILE'])
    return redirect(url_for('index'))

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
    count = 0
    for item_idx, li in enumerate(non_fee):
        item_key = f"{wallet_id}_{tx_index}_{item_idx}"
        if item_key not in classifications:
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
                           known_addresses=state.get('known_addresses', []),
                           active_nav='wallets')

@app.route('/wallets/add', methods=['POST'])
def add_wallet():
    label = request.form.get('label', '').strip()
    address = request.form.get('address', '').strip()
    if not label or not address:
        return redirect(url_for('wallets'))
    state = ensure_state()
    if len(state['wallets']) >= 6:
        return redirect(url_for('wallets'))
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
    transactions = parse_wallet_csv(filepath)
    # Auto-fill missing USD values from CoinGecko
    print(f"[CoinGecko] Scanning {len(transactions)} transactions for missing USD values...")
    transactions = fill_missing_usd_values(transactions, state)
    state['transactions'][wallet_id] = transactions
    # Clear old classifications for this wallet since new data
    to_remove = [k for k in state.get('classifications', {}) if k.startswith(f"{wallet_id}_")]
    for k in to_remove:
        del state['classifications'][k]
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
        line_items.append({
            'direction': 'sent',
            'amount': format_amount(item.get('amount', '')),
            'token': item.get('token', ''),
            'usd_value': format_usd(usd_float),
            'missing_value': usd_float == 0,
        })

    for item in details.get('received', []):
        usd_val = item.get('pretty_usd', '') or item.get('usd_value', '')
        if isinstance(usd_val, str):
            usd_val = usd_val.replace('$', '').replace(',', '').strip()
        try:
            usd_float = float(usd_val) if usd_val else 0
        except (ValueError, TypeError):
            usd_float = 0
        line_items.append({
            'direction': 'received',
            'amount': format_amount(item.get('amount', '')),
            'token': item.get('token', ''),
            'usd_value': format_usd(usd_float),
            'missing_value': usd_float == 0,
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

        # Format the value (USD) column — if original is $0, sum line items
        raw_value = tx.get('value', '')
        try:
            raw_float = float(raw_value) if raw_value else 0
        except (ValueError, TypeError):
            raw_float = 0
        if raw_float == 0 and line_items:
            total_usd = 0
            for li in line_items:
                usd_str = li.get('usd_value', '$0.00').replace('$', '').replace(',', '')
                try:
                    total_usd += abs(float(usd_str))
                except (ValueError, TypeError):
                    pass
            formatted_value = format_usd(total_usd)
        else:
            formatted_value = format_usd(raw_value) if raw_value else '$0.00'

        # Summary for parent row
        if is_multi:
            summary = build_parent_summary(tx, line_items)
        else:
            summary = format_details(tx, addr_map)

        # For single-token rows, pull classification info from item 0
        single_item = non_fee_items[0] if non_fee_items else None

        display_txs.append({
            'index': i,
            'date': format_date(tx.get('date', '')),
            'type': tx.get('type', ''),
            'details': summary,
            'value': formatted_value,
            'tx_hash': tx.get('tx_hash', ''),
            'url': tx.get('url', ''),
            'blockchain': tx.get('blockchain', ''),
            'needs_classification': needs_classification,
            'sender': tx.get('sender', ''),
            'recipient': tx.get('recipient', ''),
            'line_items': line_items,
            'is_multi': is_multi,
            'is_trade': is_trade,
            'classification_summary': classification_summary,
            # Single-token row classification
            'single_item_key': single_item.get('item_key', '') if single_item else '',
            'single_classification': single_item.get('classification', '') if single_item else '',
            'single_show_dropdown': single_item.get('show_dropdown', False) if single_item else False,
            'single_auto_label': single_item.get('auto_label', '') if single_item else '',
        })

    return render_template('wallet_detail.html', wallet=wallet, transactions=display_txs,
                           needs_review_count=needs_review_count, total_count=len(display_txs),
                           active_nav='wallets')

@app.route('/wallets/classify', methods=['POST'])
def classify_transaction():
    tx_key = request.form.get('tx_key', '')  # format: walletId_txIndex_itemIndex
    classification = request.form.get('classification', '')
    if not tx_key:
        return redirect(url_for('wallets'))
    state = ensure_state()
    if classification:
        state['classifications'][tx_key] = classification
    else:
        state['classifications'].pop(tx_key, None)
    save_state(state)
    # Redirect back to the wallet detail page
    wallet_id = tx_key.split('_')[0]
    return redirect(url_for('wallet_detail', wallet_id=wallet_id))

@app.route('/reports')
def reports():
    return render_template('reports.html', active_nav='reports')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
