#!/usr/bin/env python3
"""
Roller Analytics Data Generator
Fetches data from Roller API and writes static JSON files for the dashboard.
"""
import requests
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CLIENT_ID = os.environ.get('ROLLER_CLIENT_ID')
CLIENT_SECRET = os.environ.get('ROLLER_CLIENT_SECRET')
BASE_URL = 'https://api.roller.app'

# Amsterdam time offset (CEST = UTC+2 in summer, CET = UTC+1 in winter)
# Simplified: always use UTC+2 for now (spring/summer)
AMSTERDAM_OFFSET = timedelta(hours=2)

def get_token():
    r = requests.post(f'{BASE_URL}/token',
                      json={'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET},
                      timeout=30)
    r.raise_for_status()
    return r.json()['access_token']

def api_get(token, path):
    r = requests.get(f'{BASE_URL}{path}',
                     headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
                     timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_all_pages(token, path, max_pages=20):
    all_items = []
    page = 1
    total_pages = 1
    sep = '&' if '?' in path else '?'
    while page <= total_pages and page <= max_pages:
        data = api_get(token, f'{path}{sep}pageNumber={page}&pageSize=200')
        if not data:
            break
        all_items.extend(data.get('items', []))
        total_pages = data.get('totalPages', 1)
        page += 1
    return all_items

def fetch_booking_names(token, date):
    try:
        data = api_get(token, f'/bookings?date={date}&pageSize=200')
        mapping = {}
        for b in data.get('bookings', []):
            if b.get('bookingReference') and b.get('name'):
                mapping[b['bookingReference']] = b['name']
        return mapping
    except Exception as e:
        print(f'  Warning: could not fetch booking names for {date}: {e}')
        return {}

def categorize_entry(entry, booking_name_map):
    ref = entry.get('bookingReference', '') or ''
    name = booking_name_map.get(ref, '')
    if re.match(r'^KF\b', name):
        return 'Kinderfeestjes'
    if re.match(r'^BF\b', name):
        return 'Bedrijfsfeesten'
    loc = (entry.get('transactionLocation', '') or '').lower()
    if loc == 'pos':
        return 'Box office'
    if 'online' in loc:
        return 'Online'
    if 'venue' in loc:
        return 'Venue Manager'
    return 'Overig'

def build_day_stats(date, rev_entries, payments, booking_name_map):
    recognition = [e for e in rev_entries if e.get('entryType') == 'Recognition']
    transactions = [e for e in rev_entries if e.get('entryType') == 'Transaction']

    by_category = {}
    by_product_type = {}
    total_revenue = 0.0
    total_visitors = 0

    for e in recognition:
        cat = categorize_entry(e, booking_name_map)
        if cat not in by_category:
            by_category[cat] = {'revenue': 0.0, 'visitors': 0}
        by_category[cat]['revenue'] += float(e.get('netRevenue', 0) or 0)
        by_category[cat]['visitors'] += int(e.get('redeemedQuantity', 0) or 0)

        pt = e.get('productType', 'Overig') or 'Overig'
        if pt not in by_product_type:
            by_product_type[pt] = {'revenue': 0.0, 'visitors': 0}
        by_product_type[pt]['revenue'] += float(e.get('netRevenue', 0) or 0)
        by_product_type[pt]['visitors'] += int(e.get('redeemedQuantity', 0) or 0)

        total_revenue += float(e.get('netRevenue', 0) or 0)
        total_visitors += int(e.get('redeemedQuantity', 0) or 0)

    by_channel = {}
    total_funds = 0.0
    for e in transactions:
        key = e.get('transactionLocation', 'Onbekend') or 'Onbekend'
        if key not in by_channel:
            by_channel[key] = {'revenue': 0.0, 'count': 0}
        by_channel[key]['revenue'] += float(e.get('fundsReceived', 0) or 0)
        by_channel[key]['count'] += 1
        total_funds += float(e.get('fundsReceived', 0) or 0)

    by_payment_method = {}
    by_device = {}
    for p in payments:
        key = p.get('paymentMethodVariant') or p.get('paymentMethod', 'Onbekend') or 'Onbekend'
        by_payment_method[key] = round(by_payment_method.get(key, 0.0) + float(p.get('total', 0) or 0), 2)
        device_id = str(p.get('deviceId', '') or '')
        if device_id and device_id not in ('0', ''):
            dk = f'Kassa {device_id}'
            by_device[dk] = round(by_device.get(dk, 0.0) + float(p.get('total', 0) or 0), 2)

    # Round revenue values
    for cat in by_category:
        by_category[cat]['revenue'] = round(by_category[cat]['revenue'], 2)
    for pt in by_product_type:
        by_product_type[pt]['revenue'] = round(by_product_type[pt]['revenue'], 2)
    for ch in by_channel:
        by_channel[ch]['revenue'] = round(by_channel[ch]['revenue'], 2)

    return {
        'date': date,
        'generatedAt': datetime.now(timezone.utc).isoformat(),
        'visit': {
            'netRevenue': round(total_revenue, 2),
            'visitors': total_visitors,
            'byCategory': by_category,
            'byProductType': by_product_type,
        },
        'sales': {
            'fundsReceived': round(total_funds, 2),
            'transactionCount': len(payments),
            'byChannel': by_channel,
            'byPaymentMethod': by_payment_method,
            'byDevice': by_device,
        }
    }

def add_days(date_str, n):
    d = datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=n)
    return d.strftime('%Y-%m-%d')

def parse_to_amsterdam_date(raw):
    """Parse an ISO datetime string and convert to Amsterdam date string."""
    if not raw:
        return None
    try:
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        d = datetime.fromisoformat(raw)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        d_ams = d + AMSTERDAM_OFFSET
        return d_ams.strftime('%Y-%m-%d')
    except Exception:
        return raw[:10] if len(raw) >= 10 else None

def fetch_day_data(token, date):
    """Fetch and build stats for a single day."""
    end = add_days(date, 1)
    qs = f'startDate={date}&endDate={end}'
    rev_entries = fetch_all_pages(token, f'/reporting/revenue-entries?{qs}', 10)
    payments = fetch_all_pages(token, f'/data/bookingpayments?{qs}', 5)
    booking_names = fetch_booking_names(token, date)
    return build_day_stats(date, rev_entries, payments, booking_names)

def rebuild_week_files(today, data_dir, token):
    """Rebuild week JSON files from already-fetched day files."""
    for week_offset in range(4):
        week_end = add_days(today, -week_offset * 7)
        week_start = add_days(week_end, -6)
        out_file = data_dir / f'week-{week_end}.json'

        if out_file.exists() and week_offset >= 2:
            print(f'  Skip week ending {week_end} (exists)')
            continue

        print(f'  Generating week {week_start} → {week_end}...')
        try:
            dates_in_week = [add_days(week_start, i) for i in range(7)]
            days_data = []
            for d in dates_in_week:
                day_file = data_dir / f'day-{d}.json'
                if day_file.exists():
                    with open(day_file) as f:
                        days_data.append(json.load(f))
                else:
                    print(f'    Fetching {d}...')
                    stats = fetch_day_data(token, d)
                    days_data.append(stats)
                    with open(day_file, 'w') as f:
                        json.dump(stats, f)

            week_data = {
                'endDate': week_end,
                'startDate': week_start,
                'generatedAt': datetime.now(timezone.utc).isoformat(),
                'days': days_data,
            }
            with open(out_file, 'w') as f:
                json.dump(week_data, f)
            total_rev = sum(d['visit']['netRevenue'] for d in days_data)
            print(f'    → {out_file} (week total rev={round(total_rev, 2)})')
        except Exception as e:
            print(f'    ERROR generating week {week_end}: {e}')
            import traceback; traceback.print_exc()


def main():
    today_only = '--today-only' in sys.argv
    print('=== Roller Analytics Data Generator ===')
    print(f'Mode: {"today only" if today_only else "full refresh"}')
    print('Getting token...')
    token = get_token()
    print('Token OK')

    now_utc = datetime.now(timezone.utc)
    now_ams = now_utc + AMSTERDAM_OFFSET
    today = now_ams.strftime('%Y-%m-%d')
    print(f'Today (Amsterdam): {today}')

    data_dir = Path('data')
    data_dir.mkdir(exist_ok=True)

    if today_only:
        # Only refresh today (and yesterday to catch late entries)
        for date in [today, add_days(today, -1)]:
            out_file = data_dir / f'day-{date}.json'
            print(f'  Refreshing {date}...')
            try:
                day_data = fetch_day_data(token, date)
                with open(out_file, 'w') as f:
                    json.dump(day_data, f)
                print(f'    → rev={day_data["visit"]["netRevenue"]}, visitors={day_data["visit"]["visitors"]}')
            except Exception as e:
                print(f'    ERROR: {e}')
        # Also rebuild current week file so it reflects today's fresh data
        rebuild_week_files(today, data_dir, token)
    else:
        # Full refresh: last 30 days
        days_to_generate = [add_days(today, -i) for i in range(30)]
        for date in days_to_generate:
            out_file = data_dir / f'day-{date}.json'
            if out_file.exists() and date not in (today, add_days(today, -1)):
                print(f'  Skip {date} (exists)')
                continue
            print(f'  Generating day {date}...')
            try:
                day_data = fetch_day_data(token, date)
                with open(out_file, 'w') as f:
                    json.dump(day_data, f)
                print(f'    → rev={day_data["visit"]["netRevenue"]}, visitors={day_data["visit"]["visitors"]}')
            except Exception as e:
                print(f'    ERROR: {e}')
        rebuild_week_files(today, data_dir, token)

    print('=== Done! ===')

if __name__ == '__main__':
    main()
