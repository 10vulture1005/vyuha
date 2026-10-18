"""Fetch cached Screener statements and gap-fill the investor dataset."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from config.settings import BASE_DIR
from data.screener_client import ScreenerClient
from data.screener_fundamentals import merge_fundamentals, normalize
from investor.data import PricePanel

OUT = BASE_DIR / 'data/raw/fundamentals_investor.csv'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    universe = parser.add_mutually_exclusive_group()
    universe.add_argument('--symbol', action='append', help='repeat for multiple symbols')
    universe.add_argument('--symbols-file', type=Path, help='CSV with a symbol column (for runners without price panels)')
    parser.add_argument('--since', type=lambda s: pd.Timestamp(s).date().isoformat())
    parser.add_argument('--out', type=Path, default=OUT)
    parser.add_argument('--refresh', action='store_true', help='fetch new HTML instead of replaying cache')
    args = parser.parse_args(argv)
    existing = pd.read_csv(args.out) if args.out.exists() else pd.DataFrame()
    if not existing.empty and ('data_source' not in existing or existing.data_source.isna().any()):
        raise ValueError('Existing CSV lacks verified data_source; rebuild with fetch_investor_fundamentals.py first')
    client = ScreenerClient()
    if args.symbols_file:
        frame = pd.read_csv(args.symbols_file)
        if 'symbol' not in frame:
            parser.error('--symbols-file must contain a symbol column')
        symbols = frame.symbol.dropna().astype(str).str.strip().tolist()
    else:
        symbols = args.symbol or PricePanel().available_symbols()
    symbols = list(dict.fromkeys(s for s in symbols if s and not s.startswith('INDEX_')))
    if not symbols:
        parser.error('No symbols found; supply --symbol or --symbols-file, or ingest price panels first')
    failed = []
    rows = []
    for symbol in symbols:
        try:
            records = normalize(client.fetch(symbol, refresh=args.refresh))
            rows.extend(r for r in records if not args.since or r['period_end'] >= args.since)
        except Exception as exc:
            print(f'{symbol}: {exc}', file=sys.stderr)
            failed.append(symbol)
    if rows:
        merged = merge_fundamentals(existing, pd.DataFrame(rows))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        temp = args.out.with_suffix('.tmp')
        merged.to_csv(temp, index=False)
        temp.replace(args.out)
        print(f'Wrote {len(merged)} records to {args.out}')
    return 1 if failed or not rows else 0


if __name__ == '__main__':
    sys.exit(main())
