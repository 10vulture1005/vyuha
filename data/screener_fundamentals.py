"""Normalize dated statements only; never import current vendor ratios."""
import re

import pandas as pd

from investor.data import FUNDAMENTAL_FIELDS

KEYS = ['symbol', 'period_end', 'frequency']
MAP = {
    'sales': 'revenue', 'revenue': 'revenue', 'net profit': 'pat',
    'eps in rs': 'eps_basic', 'interest': 'interest_expense',
    'borrowings': 'debt', 'total assets': 'total_assets',
    'current assets': 'current_assets', 'current liabilities': 'current_liabilities',
    'cash from operating activity': 'ocf',
    'pledged percentage': 'promoter_pledge_pct', 'promoter pledge': 'promoter_pledge_pct',
}


def number(text):
    try:
        return float(text.replace(',', '').replace('%', '').strip())
    except ValueError:
        return None


def normalize(page):
    records = {}
    for table in page['tables']:
        section = table['section']
        if section not in ('quarters', 'profit-loss', 'balance-sheet', 'cash-flow', 'shareholding'):
            continue
        rows = table['rows']
        if not rows:
            continue
        dates = {}
        for i, header in enumerate(rows[0][1:], 1):
            if re.fullmatch(r'[A-Za-z]{3} \d{4}', header):
                dates[i] = (pd.to_datetime(header, format='%b %Y') + pd.offsets.MonthEnd()).date().isoformat()
        for i, period in dates.items():
            frequency = 'quarterly' if section in ('quarters', 'shareholding') else 'yearly'
            key = (period, frequency)
            values = {}
            for row in rows[1:]:
                if len(row) <= i:
                    continue
                label = re.sub(r'\s+', ' ', row[0].replace('+', '').replace('%', '').strip()).lower()
                value = number(row[i])
                if value is not None:
                    values[label] = value
            mapped = {}
            for label, field in MAP.items():
                if label in values:
                    mapped[field] = values[label] * (1 if field in ('eps_basic', 'promoter_pledge_pct') else 1e7)
            # Screener statements are in crore INR. Do not equate operating profit
            # (before depreciation) with EBIT, or equity capital with total equity.
            if section == 'balance-sheet' and {'equity capital', 'reserves'} <= values.keys():
                mapped['equity'] = (values['equity capital'] + values['reserves']) * 1e7
            if section in ('profit-loss', 'quarters') and {'profit before tax', 'interest'} <= values.keys():
                mapped['ebit'] = (values['profit before tax'] + values['interest']) * 1e7
            if mapped:
                rec = records.setdefault(key, dict(symbol=page['symbol'], period_end=period,
                    frequency=frequency, filing_date='', data_source='screener', basis=page['basis']))
                rec.update(mapped)
    return list(records.values())


def merge_fundamentals(existing, incoming):
    """Same-period/frequency gap fill only. yfinance wins; no temporal filling."""
    rows = {}
    for frame in (existing, incoming):
        for rec in frame.to_dict('records'):
            if rec.get('data_source') not in ('yfinance', 'screener', 'yfinance+screener'):
                raise ValueError('Refusing unverified fundamentals; verify provenance before merging')
            key = tuple(rec[k] for k in KEYS)
            if key not in rows:
                rows[key] = rec.copy()
                continue
            old = rows[key]
            new_wins = rec['data_source'] == 'yfinance'
            primary, secondary = (rec.copy(), old) if new_wins else (old, rec)
            filled = False
            for field in FUNDAMENTAL_FIELDS:
                if pd.isna(primary.get(field)) and pd.notna(secondary.get(field)):
                    primary[field] = secondary[field]
                    filled = True
            if filled and primary['data_source'] != secondary['data_source']:
                primary['data_source'] = 'yfinance+screener'
            rows[key] = primary
    return pd.DataFrame(rows.values()).sort_values(KEYS) if rows else pd.DataFrame()
