"""Read-only descriptive score paths, using the audited 96-trade cohort."""
import bisect
import hashlib
import json
import statistics as stats
from pathlib import Path
import unittest
import sqlite3
import zipfile

import analyze_entry_paths as source
from early_exit_experiment_20261005 import ts

ROOT = Path(__file__).resolve().parent
COMPONENTS = ['price_position', 'body', 'volume', 'trend_continuity', 'range',
              'liquidation', 'activity_direction']
LABELS = dict(zip(COMPONENTS, ['장기 가격', '몸통', '거래량', '추세', 'RANGE', '청산', 'ATR 방향']))


def values(row, side):
    own = side.lower() + '_score'
    other = ('short' if side == 'LONG' else 'long') + '_score'
    components = json.loads(row['score_components_json'] or '{}')
    out = {'own': row[own], 'other': row[other], 'advantage': row[own] - row[other]}
    for name in COMPONENTS:
        for label, key in [('own', own), ('other', other)]:
            out[name + '_' + label] = components.get(name, {}).get(key)
    return out


def changes(before, after):
    return {k: after[k] - v if v is not None and after.get(k) is not None else None
            for k, v in before.items()}


def aggregate(items):
    result = {'n': len(items)}
    keys = sorted({k for item in items for k in item})
    for key in keys:
        numbers = [item[key] for item in items if item.get(key) is not None]
        if numbers:
            result[key] = {'n': len(numbers), 'mean': stats.mean(numbers),
                           'median': stats.median(numbers)}
    return result


def run():
    audited = json.loads((ROOT / 'early_exit_experiment_20261005.json').read_text(encoding='utf-8'))
    source.FIELDS[:] = ['timestamp', 'symbol', 'price', 'long_score', 'short_score',
                        'strategy_version', 'strategy_config_json', 'score_components_json']
    rows = source.read(source.DOWNLOADS / 'btc_market_state_20260920_043441.db')
    # Deserialize archives in memory: no extracted copies or source writes.
    for archive in sorted(source.DOWNLOADS.glob('btc_market_state_delta_*.zip')):
        with zipfile.ZipFile(archive) as z:
            raw = z.read('delta.db')
            metadata = json.loads(z.read('manifest.json'))
            assert hashlib.sha256(raw).hexdigest() == metadata['delta_sha256']
            connection = sqlite3.connect(':memory:')
            try:
                connection.deserialize(raw)
                connection.row_factory = sqlite3.Row
                rows.extend(dict(r) for r in connection.execute('SELECT ' + ','.join(source.FIELDS) + " FROM market_state WHERE symbol='BTCUSDT'"))
            finally:
                connection.close()
    db = Path('C:/Users/chlgh/AppData/Local/Temp/delta (3).db')
    manifest = json.loads(db.with_name('manifest (4).json').read_text(encoding='utf-8'))
    assert hashlib.sha256(db.read_bytes()).hexdigest() == manifest['delta_sha256']
    merged = {r['timestamp']: r for r in rows}
    merged.update({r['timestamp']: r for r in source.read(db)})
    rows = sorted(merged.values(), key=lambda r: ts(r['timestamp']))
    times = [ts(r['timestamp']) for r in rows]
    details = []
    for trade in audited['trades']:
        a = bisect.bisect_right(times, trade['start_t']) - 1
        b = bisect.bisect_right(times, trade['end_t']) - 1
        assert a >= 0 and trade['start_t'] - times[a] <= 120
        assert trade['end_t'] - times[b] <= 120
        for i in range(a, b + 1):
            config = json.dumps(json.loads(rows[i]['strategy_config_json'] or '{}'), sort_keys=True)
            group = rows[i]['strategy_version'] + ':' + hashlib.sha256(config.encode()).hexdigest()[:8]
            assert group == trade['group']
            if i > a:
                assert times[i] - times[i-1] <= 180
        entry = values(rows[a], trade['side'])
        item = {k: trade[k] for k in ['entry', 'exit', 'group', 'side', 'actual_net', 'hold_hours']}
        item['outcome'] = 'win' if trade['actual_net'] > 0 else 'loss' if trade['actual_net'] < 0 else 'flat'
        item['entry_scores'] = entry
        item['exit_delta'] = changes(entry, values(rows[b], trade['side']))
        for minutes in (15, 60, 240):
            target = trade['start_t'] + minutes * 60
            j = bisect.bisect_left(times, target)
            if j <= b and times[j] < trade['end_t'] and times[j] - target <= 120:
                change = changes(entry, values(rows[j], trade['side']))
                change['price_return_pct'] = (1 if trade['side'] == 'LONG' else -1) * (rows[j]['price'] / trade['entry_price'] - 1) * 100
                item[str(minutes)] = change
        details.append(item)
    groups = {}
    for group in sorted({t['group'] for t in details}):
        sample = [t for t in details if t['group'] == group]
        groups[group] = {'n': len(sample), 'entry_start': min(t['entry'] for t in sample),
                         'exit_end': max(t['exit'] for t in sample)}
        for outcome in ('win', 'loss'):
            subset = [t for t in sample if t['outcome'] == outcome]
            groups[group][outcome] = {phase: aggregate([t[phase] for t in subset if phase in t])
                                     for phase in ('entry_scores', '15', '60', '240', 'exit_delta')}
    result = {'cohort_source': 'early_exit_experiment_20261005.json', 'eligible': len(details),
              'original_exclusions': audited['exclusion_counts'], 'groups': groups, 'trades': details}
    (ROOT / 'score_outcomes_20261007.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    report(result)
    print(json.dumps(groups, ensure_ascii=False))


def fmt(summary, key):
    v = summary.get(key)
    return f"{v['mean']:+.2f}" if v else '자료 없음'


def report(result):
    lines = ['# 수익 거래와 손실 거래의 점수 변화', '',
             '## 비교 방법',
             '- 기존 검증된 96건: 실제 주문 로그와 분 단위 점수 기록을 연결. 원본 및 매매 코드는 변경하지 않음.',
             '- 수익/손실은 기록된 추정 왕복 수수료 차감 손익으로 구분. 펀딩비와 미기록 체결 비용은 포함하지 않음.',
             '- 전략 버전 및 전체 설정별로 분리. 내 점수는 보유 방향, 반대 점수는 반대 방향을 뜻함.',
             '- 시점별 값은 진입 대비 변화의 평균. 진입/청산 점수는 직전 120초 이내 관측치.',
             '- 15분/1시간/4시간은 해당 시점까지 계속 보유한 거래만 포함. 빠른 수익 청산이 빠지는 생존 편향이 있으므로 건수를 함께 볼 것.',
             '- 청산 직전 점수는 청산 규칙의 영향을 받으므로 손실 예측 능력을 증명하지 않음.',
             '- 과거에 이미 살펴본 자료의 탐색 분석이며 독립 검증이 아님. 원인 또는 새로운 진입 조건의 효과로 단정하지 않음.',
             '- ATR 방향 가점과 ATR 원점수는 다름. RANGE는 구성요소 전체이며 내부 위치/돌파를 별도 합산하지 않음.',
             '- 추가 진입, 비용/경계 기록 부족, 보유 중 전략 변경 거래는 기존 표본 선정에서 제외됨.',
             '', '제외 내역: ' + json.dumps(result['original_exclusions'], ensure_ascii=False)]
    for group, g in result['groups'].items():
        lines += ['', f"## {group}", f"{g['entry_start']} ~ {g['exit_end']} KST / {g['n']}건", '',
                  '|시점|결과|건수|내 점수|반대 점수|우위 차이|가격 수익률|', '|---|---|---:|---:|---:|---:|---:|']
        for phase, label in [('entry_scores', '진입 절대점수'), ('15', '15분 변화'), ('60', '1시간 변화'), ('240', '4시간 변화'), ('exit_delta', '청산까지 변화')]:
            for outcome, word in [('win', '수익'), ('loss', '손실')]:
                s = g[outcome][phase]
                lines.append(f"|{label}|{word}|{s['n']}|{fmt(s,'own')}|{fmt(s,'other')}|{fmt(s,'advantage')}|{fmt(s,'price_return_pct')}|")
        for phase, label in [('15', '15분'), ('60', '1시간'), ('exit_delta', '청산까지')]:
            lines += ['', f'### {label} 구성요소 변화', '|항목|수익: 내 점수|손실: 내 점수|수익: 반대 점수|손실: 반대 점수|', '|---|---:|---:|---:|---:|']
            for key in COMPONENTS:
                w, l = g['win'][phase], g['loss'][phase]
                lines.append(f"|{LABELS[key]}|{fmt(w,key+'_own')}|{fmt(l,key+'_own')}|{fmt(w,key+'_other')}|{fmt(l,key+'_other')}|")
    lines += ['', '## 재현', '`python work/score_outcomes_20261007.py`',
              '`python work/score_outcomes_20261007.py --test`',
              '개별 거래, 중앙값, 필드별 유효 건수는 동일 이름 JSON에 저장. 최신 전략의 표본이 충분한지 별도로 확인해야 함.']
    (ROOT / 'SCORE_OUTCOMES_20261007.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


class Tests(unittest.TestCase):
    def test_short_orientation(self):
        v = values({'long_score': 8, 'short_score': 3, 'score_components_json': '{}'}, 'SHORT')
        self.assertEqual((v['own'], v['other'], v['advantage']), (3, 8, -5))
        self.assertIsNone(v['body_own'])

    def test_missing_is_not_zero(self):
        self.assertEqual(changes({'a': None, 'b': 5}, {'a': 3, 'b': 2}), {'a': None, 'b': -3})

    def test_aggregate(self):
        self.assertEqual(aggregate([{'a': 1}, {'a': None}, {'a': 3}])['a'], {'n': 2, 'mean': 2, 'median': 2})


if __name__ == '__main__':
    import sys
    if '--test' in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    else:
        run()
