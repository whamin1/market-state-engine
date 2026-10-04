"""Offline score-distribution replay. Does not simulate orders or modify source data."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_state_engine.config import MarketStateConfig
from market_state_engine.liquidation_speed import _Series, _percentile, _score
from liquidation_speed_study import load_raw


def main():
    raw, _, _, _, invalid = load_raw()
    assert invalid == 0
    raw = [r for r in raw if r[3] == 'BTCUSDT']
    series = {side: _Series([(t, value) for t, value, s, _ in raw if s == side])
              for side in ('BUY', 'SELL')}
    start, end = raw[0][0], raw[-1][0]
    config = MarketStateConfig()
    old, new, withheld = Counter(), Counter(), 0
    for now in range(int(start // 60 + 1) * 60, int(end), 60):
        windows = {side: s.window(now, 5, start, config) for side, s in series.items()}
        a, b = (windows[side][0] for side in ('BUY', 'SELL'))
        if not (a['reference_ready'] and b['reference_ready']):
            continue
        total = a['amount_usd'] + b['amount_usd']
        imbalance = (a['amount_usd'] - b['amount_usd']) / total if total else 0
        side = 'BUY' if imbalance > 0 else 'SELL'
        f, history = windows[side]
        passed = total > 0 and abs(imbalance) >= config.liquidation_min_imbalance_ratio
        passed = passed and f['increasing'] and f['above_mean']
        before = _score(_percentile(f['speed_usd_per_min'], history), f['speed_usd_per_min']) if passed else 0
        after = f['raw_score'] if passed else 0
        old[before] += 1
        new[after] += 1
        withheld += bool(passed and not f['rank_ready'])
    result = {
        'source': 'liquidation_research_20261001_211232.tar.gz',
        'source_start_utc': datetime.fromtimestamp(start, timezone.utc).isoformat(),
        'source_end_utc': datetime.fromtimestamp(end, timezone.utc).isoformat(),
        'evaluated_minutes': sum(old.values()),
        'old_direction_score': dict(sorted(old.items())),
        'new_direction_score': dict(sorted(new.items())),
        'withheld_positive_sample_minutes': withheld,
        'limitations': 'Minute samples overlap. Fixed UTC reference bins. Event coverage assumed; '
                       'no file-availability simulation. Not a trade backtest or a profit estimate.',
    }
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
