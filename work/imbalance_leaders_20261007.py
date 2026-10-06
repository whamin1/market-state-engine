"""Read-only market event study. No forecasts, orders, or strategy edits."""
import bisect
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics as st
import sys
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent
DOWNLOADS = Path('C:/Users/chlgh/Downloads')
KST = timezone(timedelta(hours=9))
COMPONENTS = ['price_position', 'body', 'volume', 'trend_continuity', 'range', 'liquidation', 'activity_direction']
NAMES = dict(zip(COMPONENTS, ['장기 가격', '몸통', '거래량', '추세', 'RANGE', '청산', 'ATR 방향']))
FIELDS = ['timestamp', 'price', 'long_score', 'short_score', 'strategy_version', 'strategy_config_json',
          'score_components_json', 'atr', 'candle_open', 'candle_close', 'expected_final_volume',
          'range_high', 'range_low', 'short_liq_1h', 'long_liq_1h']


def read_connection(c):
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute('SELECT ' + ','.join(FIELDS) + " FROM market_state WHERE symbol='BTCUSDT'")]


def load():
    merged = {}
    sources = []
    base = DOWNLOADS / 'btc_market_state_20260920_043441.db'
    with closing(sqlite3.connect(base.as_uri() + '?mode=ro&immutable=1', uri=True)) as c:
        merged.update({r['timestamp']: r for r in read_connection(c)})
    sources.append(str(base))
    for path in sorted(DOWNLOADS.glob('btc_market_state_delta_*.zip')):
        with zipfile.ZipFile(path) as z:
            raw = z.read('delta.db')
            manifest = json.loads(z.read('manifest.json'))
            assert hashlib.sha256(raw).hexdigest() == manifest['delta_sha256']
            with closing(sqlite3.connect(':memory:')) as c:
                c.deserialize(raw)
                merged.update({r['timestamp']: r for r in read_connection(c)})
        sources.append(str(path))
    last = Path('C:/Users/chlgh/AppData/Local/Temp/delta (3).db')
    manifest = json.loads(last.with_name('manifest (4).json').read_text(encoding='utf-8'))
    assert hashlib.sha256(last.read_bytes()).hexdigest() == manifest['delta_sha256']
    with closing(sqlite3.connect(last.as_uri() + '?mode=ro&immutable=1', uri=True)) as c:
        merged.update({r['timestamp']: r for r in read_connection(c)})
    sources.append(str(last))
    rows = sorted(merged.values(), key=lambda r: r['timestamp'])
    for r in rows:
        r['t'] = datetime.fromisoformat(r['timestamp']).timestamp()
        r['components'] = json.loads(r.pop('score_components_json') or '{}')
        config = json.dumps(json.loads(r.pop('strategy_config_json') or '{}'), sort_keys=True)
        r['group'] = r['strategy_version'] + ':' + hashlib.sha256(config.encode()).hexdigest()[:8]
    return rows, sources


def band(gap):
    if gap < 0: return 'opposite_leads'
    if gap < 5: return '0~4'
    if gap < 10: return '5~9'
    if gap < 15: return '10~14'
    return '15+'


def score(row, side):
    return row[side.lower() + '_score']


def component(row, side, key):
    return row['components'].get(key, {}).get(side.lower() + '_score')


def stats(values):
    values = [v for v in values if v is not None]
    return {'n': len(values), 'mean': st.mean(values) if values else None,
            'median': st.median(values) if values else None,
            'positive_pct': 100 * sum(v > 0 for v in values) / len(values) if values else None}


def change(a, b):
    return None if a is None or b is None else b - a


def pct(a, b):
    return None if a is None or b is None or a <= 0 else 100 * (b / a - 1)


def spaced(events, seconds=14400):
    kept = []
    for event in events:
        if not kept or event['t'] - kept[-1]['t'] >= seconds:
            kept.append(event)
    return kept


def first_crossing(rows, times, start, end, side, threshold):
    return next((j for j in range(start+1, end+1)
                 if times[j] <= times[start]+14400 and score(rows[j], side) >= threshold), None)


def summarize(events):
    out = {'n': len(events)}
    for m in (15, 60, 240):
        out[str(m)] = stats([e['returns'][str(m)] for e in events])
    return out


def low_summary(events):
    out = summarize(events)
    for threshold in (10, 14):
        out['hit' + str(threshold)] = {str(m): sum(e['hits'][str(threshold)] is not None and e['hits'][str(threshold)] <= m for e in events) for m in (15, 60, 240)}
    return out


def run():
    rows, sources = load()
    times = [r['t'] for r in rows]
    breaks = [0]
    for i in range(1, len(rows)):
        breaks.append(breaks[-1] + int(times[i] - times[i-1] > 180 or rows[i]['group'] != rows[i-1]['group']))
    market = []
    low = []
    for i, r in enumerate(rows):
        past = bisect.bisect_right(times, r['t'] - 900) - 1
        future = {str(m): bisect.bisect_left(times, r['t'] + m * 60) for m in (15, 60, 240)}
        end = future['240']
        if past < 0 or end >= len(rows) or breaks[end] != breaks[past]: continue
        if r['t'] - 900 - times[past] > 120: continue
        if any(times[j] - r['t'] - int(m) * 60 > 120 for m, j in future.items()): continue
        raw_returns = {m: pct(r['price'], rows[j]['price']) for m, j in future.items()}
        diff = r['long_score'] - r['short_score']
        market.append({'t': r['t'], 'group': r['group'], 'diff': diff,
                       'diff_change15': diff - (rows[past]['long_score'] - rows[past]['short_score']),
                       'returns': raw_returns})
        for side, other, sign in [('LONG', 'SHORT', 1), ('SHORT', 'LONG', -1)]:
            if not 1 <= score(r, side) <= 5: continue
            # The four-hour landmark is chosen without inspecting its future.
            e = {'t': r['t'], 'group': r['group'], 'side': side, 'own': score(r, side),
                 'other': score(r, other), 'both_low': 1 <= score(r, other) <= 5,
                 'returns': {m: sign * v for m, v in raw_returns.items()}, 'hits': {},
                 'features': {}, 'crossing': None}
            e['features']['own_score_change15'] = score(r, side) - score(rows[past], side)
            e['features']['other_score_change15'] = score(r, other) - score(rows[past], other)
            e['features']['price_return15'] = sign * pct(rows[past]['price'], r['price'])
            e['features']['projected_volume_change15_pct'] = pct(rows[past]['expected_final_volume'], r['expected_final_volume'])
            e['features']['atr_change15_pct'] = pct(rows[past]['atr'], r['atr'])
            e['features']['signed_body_change15_pct'] = change(
                sign * pct(rows[past]['candle_open'], rows[past]['candle_close']),
                sign * pct(r['candle_open'], r['candle_close'])) if rows[past]['candle_open'] == r['candle_open'] else None
            for k in COMPONENTS:
                e['features'][k + '_change15'] = change(component(rows[past], side, k), component(r, side, k))
            for threshold in (10, 14):
                crossing = first_crossing(rows, times, i, end, side, threshold)
                e['hits'][str(threshold)] = (times[crossing] - r['t']) / 60 if crossing is not None else None
                if threshold == 10 and crossing is not None:
                    contributions = {k: change(component(r, side, k), component(rows[crossing], side, k)) for k in COMPONENTS}
                    first = {}
                    for k in COMPONENTS:
                        for j in range(i+1, crossing+1):
                            delta = change(component(rows[j-1], side, k), component(rows[j], side, k))
                            if delta is not None and delta > 0:
                                first[k] = {'t': times[j], 'lead_minutes': (times[crossing]-times[j])/60}
                                break
                    earliest = min((v['t'] for v in first.values()), default=None)
                    e['crossing'] = {'minutes': e['hits']['10'], 'contributions': contributions,
                                     'first': first, 'earliest': [k for k, v in first.items() if v['t'] == earliest],
                                     'price_already_moved_pct': sign * pct(r['price'], rows[crossing]['price']),
                                     'price_after_cross_to_4h_pct': sign * pct(rows[crossing]['price'], rows[end]['price'])}
            low.append(e)
    groups = {}
    for group in sorted({r['group'] for r in rows}):
        observed = [r for r in rows if r['group'] == group]
        g = {'start': datetime.fromtimestamp(observed[0]['t'], KST).isoformat(),
             'end': datetime.fromtimestamp(observed[-1]['t'], KST).isoformat(), 'rows': len(observed)}
        candidates = [e for e in market if e['group'] == group]
        g['spread'] = {}
        for mode, sample in [('all_minutes', candidates), ('non_overlapping', spaced(candidates))]:
            output = {}
            for side, sign in [('LONG', 1), ('SHORT', -1)]:
                aligned = [{**e, 'returns': {m: sign*v for m, v in e['returns'].items()}} for e in sample if sign*e['diff'] > 0]
                output[side] = {b: summarize([e for e in aligned if band(abs(e['diff'])) == b]) for b in ('0~4', '5~9', '10~14', '15+')}
                output[side]['flow'] = {label: summarize([e for e in aligned if check(sign*e['diff_change15'])]) for label, check in [('widening', lambda v:v>=2), ('flat', lambda v: -2<v<2), ('narrowing', lambda v:v<=-2)]}
            output['tie'] = summarize([e for e in sample if e['diff'] == 0])
            g['spread'][mode] = output
        g['low'] = {}
        for side in ('LONG', 'SHORT'):
            all_low = [e for e in low if e['group'] == group and e['side'] == side]
            sample = spaced(all_low)
            successes = [e for e in sample if e['crossing'] is not None]
            failures = [e for e in sample if e['crossing'] is None]
            data = {'all_minutes_n': len(all_low), 'summary': low_summary(sample),
                    'success': low_summary(successes), 'not_reached': low_summary(failures),
                    'both_low': low_summary([e for e in sample if e['both_low']]), 'features': {}, 'crossing': {}}
            data['opponent_bands'] = {label: low_summary([e for e in sample if lo <= e['other'] < hi])
                                      for label,lo,hi in [('0~5',0,6),('6~9',6,10),('10+',10,float('inf'))]}
            for name in (list(sample[0]['features']) if sample else []):
                valid = [e for e in sample if e['features'][name] is not None]
                positive = [e for e in valid if e['features'][name] > 0]
                nonpositive = [e for e in valid if e['features'][name] <= 0]
                data['features'][name] = {'success_values': stats([e['features'][name] for e in successes]),
                                          'failure_values': stats([e['features'][name] for e in failures]),
                                          'increased': low_summary(positive), 'not_increased': low_summary(nonpositive)}
            for k in COMPONENTS:
                leads = [e['crossing']['first'][k]['lead_minutes'] for e in successes if k in e['crossing']['first']]
                data['crossing'][k] = {'net_contribution': stats([e['crossing']['contributions'][k] for e in successes]),
                                      'earliest_including_ties': sum(k in e['crossing']['earliest'] for e in successes),
                                      'lead_minutes': stats(leads), 'strictly_before_count': sum(v>0 for v in leads)}
            data['price_at_crossing'] = stats([e['crossing']['price_already_moved_pct'] for e in successes])
            data['price_after_crossing'] = stats([e['crossing']['price_after_cross_to_4h_pct'] for e in successes])
            g['low'][side] = data
        groups[group] = g
    result = {'sources': sources, 'groups': groups,
              'low_landmarks': [e for group in groups for side in ('LONG','SHORT') for e in spaced([x for x in low if x['group']==group and x['side']==side])]}
    (ROOT/'imbalance_leaders_20261007.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    report(result)
    print(json.dumps({k:{'rows':v['rows'],'spread_n':sum(v['spread']['non_overlapping'][s][b]['n'] for s in ('LONG','SHORT') for b in ('0~4','5~9','10~14','15+')),
                          'low_n':{s:v['low'][s]['summary']['n'] for s in ('LONG','SHORT')}} for k,v in groups.items()}, ensure_ascii=False))


def number(v):
    return '-' if v is None else f'{v:.2f}'


def hit_text(s):
    n = s['n']; h = s['hit10']['240']
    return f'{h}/{n} ({100*h/n:.1f}%)' if n else '-'


def report(result):
    largest = max(result['groups'], key=lambda k: result['groups'][k]['rows'])
    main = result['groups'][largest]
    lines = ['# 점수 불균형과 가격, 낮은 점수의 선행 변화', '', '## 먼저 읽을 결론',
             f"아래 핵심 수치는 가장 긴 동일 설정 구간({main['start'][:10]} ~ {main['end'][:10]}, {largest}) 기준. 나머지 설정은 아래에 별도 표기했다.",
             '- 점수 차이가 커질수록 이후 가격이 더 유리해지는 일관된 단계별 관계는 확인되지 않았다. LONG과 SHORT도 결과가 달랐다.',
             '- 청산은 낮은 점수에서 10점 도달까지의 큰 배점 기여 항목이었다. 그러나 낮은 점수 관측 전 청산 점수 증가가 이후 도달률을 높여주지는 않았다.',
             '- 점수 상승의 구성요소와 가격의 선행 원인은 다르다. 사후 기여를 매수 신호로 해석하지 않는다.',
             '', '|방향|저점수 표본|4시간 내 10점 도달|도달 시 청산 평균 순기여|도달 전 이미 유리하게 움직인 가격|', '|---|---:|---|---:|---:|']
    for side in ('LONG', 'SHORT'):
        d = main['low'][side]
        lines.append(f"|{side}|{d['summary']['n']}|{hit_text(d['summary'])}|{number(d['crossing']['liquidation']['net_contribution']['mean'])}점|{number(d['price_at_crossing']['mean'])}%|")
    lines += ['', '마지막 열은 나중에 도달한 사례만 고른 사후 결과이며 실시간 진입의 기대 수익률이 아니다.',
              '', '## 방법과 주의점',
             '- 실제 진입 여부와 관계없이 전체 시장 기록을 사용. 원본 읽기 전용, 매매 규칙 변경 없음.',
             '- 전략 버전과 전체 설정을 분리. 과거 청산 배점의 결과를 최신 배점의 성능으로 해석하지 않음.',
             '- 미래 15분/1시간/4시간 관측이 있고 전후 15분~4시간에 3분 초과 공백이나 설정 변경이 없는 시점만 포함.',
             '- 주 표는 시간순 최소 4시간 간격 표본. 모든 분 표본 결과도 JSON에 보존. 4시간 간격도 통계적 독립을 보장하지 않음.',
             '- 가격 수익률은 LONG이면 상승이 양수, SHORT이면 하락이 양수. 비용/레버리지/주문/청산 규칙 없는 가격 변화이며 실제 매매 손익이 아님.',
             '- 점수 차이 표: 전체 유효 시점을 먼저 4시간 간격으로 뽑은 뒤 우위 방향과 점수 차이별 분류. 차이가 0인 시점은 JSON에 별도 보존.',
             '- 저점수 표: 해당 방향 1~5점을 시간순으로 4시간 간격 선정. 반대 방향은 제한하지 않고 양쪽 1~5점은 별도 집계.',
             '- 높은 점수는 10점 도달로 정의, 14점 도달도 JSON에 보존. 도달은 중간에 한 번이라도 기준 이상인 경우.',
             '- 선행 조건은 저점수 관측 이전 15분의 변화가 양수인지로 비교. 상승하지 않은 사례도 함께 포함.',
             '- 최초 증가와 도달 순간 순기여는 사후 설명. 도달한 분과 동시에 오른 항목은 선행 신호가 아님. 여러 항목 동시 증가를 중복 인정.',
             '- 구성요소는 RANGE 전체/ATR 방향 점수. 점수 제한과 별도 보너스 때문에 구성요소 변화의 합이 최종 점수 변화와 항상 같지는 않음.',
             '- OI와 1분 실제 체결 거래량은 이번 분석에 포함하지 않음. 거래량 원본 비교는 예상 하루 거래량 변화, ATR은 저장된 원값 변화.',
             '- 여러 항목을 탐색한 결과이며 원인/인과관계나 미래 수익을 증명하지 않음. 작은 표본 및 시장 시기 차이에 주의.', '']
    for group, g in result['groups'].items():
        lines += [f'## {group}', f"{g['start']} ~ {g['end']} / {g['rows']}분 기록", '',
                  '### 점수 차이 이후 가격: 4시간 간격 표본', '|우위|점수 차이|건수|15분 평균 %|1시간 평균 %|4시간 평균 %|4시간 중앙값 %|4시간 유리한 비율 %|', '|---|---|---:|---:|---:|---:|---:|---:|']
        for side in ('LONG','SHORT'):
            for b in ('0~4','5~9','10~14','15+'):
                s=g['spread']['non_overlapping'][side][b]
                lines.append(f"|{side}|{b}|{s['n']}|{number(s['15']['mean'])}|{number(s['60']['mean'])}|{number(s['240']['mean'])}|{number(s['240']['median'])}|{number(s['240']['positive_pct'])}|")
        for side in ('LONG','SHORT'):
            d=g['low'][side]
            lines += ['', f'### {side} 1~5점 출발', f"4시간 이내 10점 도달: {hit_text(d['summary'])}. 양쪽 모두 1~5점: {hit_text(d['both_low'])}.",
                      '|결과|건수|15분 가격 %|1시간 가격 %|4시간 가격 %|', '|---|---:|---:|---:|---:|']
            for label,key in [('10점 도달','success'),('10점 미도달','not_reached')]:
                s=d[key]
                lines.append(f"|{label}|{s['n']}|{number(s['15']['mean'])}|{number(s['60']['mean'])}|{number(s['240']['mean'])}|")
            lines += ['', '|반대 점수|10점 도달|4시간 보유 방향 가격 %|', '|---|---|---:|']
            for label,s in d['opponent_bands'].items():
                lines.append(f"|{label}|{hit_text(s)}|{number(s['240']['mean'])}|")
            lines += ['', '|관측 전 15분 변화|증가했을 때 10점 도달|증가 안 했을 때 10점 도달|증가 시 4시간 가격 %|미증가 시 4시간 가격 %|', '|---|---|---|---:|---:|']
            for name, f in d['features'].items():
                label = NAMES.get(name.removesuffix('_change15'), name)
                label = {'own_score_change15':'내 총점','other_score_change15':'반대 총점','price_return15':'보유 방향 가격','projected_volume_change15_pct':'예상 하루 거래량','atr_change15_pct':'ATR 원값','signed_body_change15_pct':'보유 방향 일봉 몸통 원값'}.get(name,label)
                lines.append(f"|{label}|{hit_text(f['increased'])}|{hit_text(f['not_increased'])}|{number(f['increased']['240']['mean'])}|{number(f['not_increased']['240']['mean'])}|")
            lines += ['', '|10점 도달 사례 사후분해|평균 순기여 점수|가장 먼저 증가(동시 포함)|도달보다 먼저 증가한 건수|', '|---|---:|---:|---:|']
            for k,c in d['crossing'].items():
                lines.append(f"|{NAMES[k]}|{number(c['net_contribution']['mean'])}|{c['earliest_including_ties']}|{c['strictly_before_count']}|")
            lines += [f"10점 도달까지 이미 움직인 가격 평균: {number(d['price_at_crossing']['mean'])}%. 도달 후 최초 관측+4시간까지: {number(d['price_after_crossing']['mean'])}% (도달 시각마다 남은 시간이 다름).", '']
    lines += ['## 재현', '`python work/imbalance_leaders_20261007.py`', '`python work/imbalance_leaders_20261007.py --test`', '세부 수치와 관측 시점별 자료: imbalance_leaders_20261007.json']
    (ROOT/'IMBALANCE_LEADERS_20261007.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


class Tests(unittest.TestCase):
    def test_bands(self):
        self.assertEqual([band(x) for x in (0,4,5,9,10,14,15)], ['0~4','0~4','5~9','5~9','10~14','10~14','15+'])

    def test_non_overlap(self):
        self.assertEqual([e['t'] for e in spaced([{'t':x} for x in (0,60,14400,14500,28800)])], [0,14400,28800])

    def test_missing(self):
        self.assertIsNone(change(None,3))
        self.assertIsNone(pct(0,5))
        self.assertEqual(stats([None,1,3])['n'],2)

    def test_crossing_deadline_and_first(self):
        rows = [{'long_score':v,'short_score':0} for v in (3,9,10,15)]
        self.assertEqual(first_crossing(rows,[0,60,120,14401],0,3,'LONG',10),2)
        self.assertIsNone(first_crossing(rows,[0,60,120,14401],0,3,'LONG',14))
        self.assertEqual(first_crossing(rows,[0,60,120,14400],0,3,'LONG',14),3)


if __name__ == '__main__':
    if '--test' in sys.argv:
        unittest.main(argv=[sys.argv[0]])
    else:
        run()
