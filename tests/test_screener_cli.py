"""Offline coverage for the GitHub Actions symbol-selection entrypoint."""
import pandas as pd
import pytest

from scripts import fetch_screener_snapshots as cli


def test_symbols_file_fetches_unique_non_index_symbols(tmp_path, monkeypatch):
    symbols = tmp_path / 'universe.csv'
    symbols.write_text('symbol,name\n TEST ,Example\nTEST,Duplicate\nINDEX_NIFTY50,Index\n,Missing\nOTHER,Other\n')
    out = tmp_path / 'statements.csv'
    calls = []

    class Client:
        def fetch(self, symbol, refresh=False):
            calls.append((symbol, refresh))
            return symbol

    monkeypatch.setattr(cli, 'ScreenerClient', Client)
    monkeypatch.setattr(cli, 'normalize', lambda symbol: [{'symbol': symbol}])
    monkeypatch.setattr(cli, 'merge_fundamentals', lambda old, new: new)
    assert cli.main(['--symbols-file', str(symbols), '--refresh', '--out', str(out)]) == 0
    assert calls == [('TEST', True), ('OTHER', True)]
    assert pd.read_csv(out).symbol.tolist() == ['TEST', 'OTHER']


@pytest.mark.parametrize('content', ['name\nExample\n', 'symbol\nINDEX_NIFTY50\n'])
def test_invalid_or_empty_universe_fails(tmp_path, content):
    symbols = tmp_path / 'universe.csv'
    symbols.write_text(content)
    with pytest.raises(SystemExit) as exc:
        cli.main(['--symbols-file', str(symbols), '--out', str(tmp_path / 'out.csv')])
    assert exc.value.code == 2


def test_symbol_selection_options_are_exclusive():
    with pytest.raises(SystemExit) as exc:
        cli.main(['--symbol', 'TEST', '--symbols-file', 'universe.csv'])
    assert exc.value.code == 2
