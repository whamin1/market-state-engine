"""Read-only OI supplement to the weekly trade review."""
import bisect
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile

import analyze_entry_paths as src


def ts(value):
    return datetime.fromisoformat(value).timestamp()


def read_oi(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)) as c:
        c.row_factory=sqlite3.Row
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='oi_snapshots'").fetchone():
            return []
        return [dict(r) for r in c.execute("SELECT * FROM oi_snapshots WHERE symbol='BTCUSDT'")]


def main():
    merged={}
    for archive in sorted(src.DOWNLOADS.glob('btc_market_state_delta_*.zip')):
        with zipfile.ZipFile(archive) as z, tempfile.TemporaryDirectory() as tmp:
            raw=z.read('delta.db')
            assert hashlib.sha256(raw).hexdigest()==json.loads(z.read('manifest.json'))['delta_sha256']
            p=Path(tmp)/'delta.db'
            p.write_bytes(raw)
            merged.update({r['timestamp']:r for r in read_oi(p)})
    merged.update({r['timestamp']:r for r in read_oi(Path('C:/Users/chlgh/AppData/Local/Temp/delta (2).db'))})
    good=[r for r in merged.values() if r['status']=='ok' and r['open_interest'] and r['observed_at']]
    for r in good:
        r['t']=max(ts(r['collected_at']),ts(r['observed_at']))
    good.sort(key=lambda r:r['t'])
    times=[r['t'] for r in good]
    def oi(t):
        i=bisect.bisect_right(times,t)-1
        return good[i]['open_interest'] if i>=0 and t-times[i]<=120 and t-ts(good[i]['observed_at'])<=120 else None
    def change(a,b):
        return (b/a-1)*100 if a and b else None
    rows,_=src.load()
    mt=[r['t'] for r in rows]
    def price(t):
        i=bisect.bisect_right(mt,t)-1
        return rows[i]['price'] if i>=0 and t-mt[i]<=120 else None
    review=json.loads((src.ROOT/'weekly_failure_20261002.json').read_text())
    result=[]
    for trade in review['trades']:
        t=ts(trade['entry_time']); end=t+trade['hours']*3600
        result.append({**{k:trade[k] for k in ('entry','side','net','new_rule')},
                       'oi15':change(oi(t-900),oi(t)),
                       'oi60':change(oi(t-3600),oi(t)),
                       'price60':change(price(t-3600),price(t)),
                       'oi_after60':change(oi(t),oi(t+3600)) if end>=t+3600 else None,
                       'oi_held':change(oi(t),oi(end))})
    def f(v):
        return '자료 없음' if v is None else f'{v:+.3f}%'
    lines=['# 이번주 OI와 거래 결과','',
           'OI는 미결제 계약 수량입니다. 매수·매도 방향을 직접 알려주지 않습니다.',
           '진입 전 값은 관측·수집 시각이 진입보다 늦지 않은 자료만 사용했습니다. 120초 이상 오래된 값은 제외했습니다.',
           '일부 과거 거래에는 OI 정기 수집 이전이라 자료가 없습니다. 보유 중 변화는 사후 설명용입니다.', '',
           '|진입 KST|방향|직전 1시간 가격|직전 15분 OI|직전 1시간 OI|진입 후 1시간 OI|보유 중 OI|순손익 USDT|',
           '|---|---|---:|---:|---:|---:|---:|---:|']
    for r in result:
        lines.append('|'+ '|'.join([r['entry'],r['side']]+[f(r[k]) for k in ('price60','oi15','oi60','oi_after60','oi_held')]+[f"{r['net']:+.2f}"] )+'|')
    groups=[]
    for positive in (True,False):
        selected=[r for r in result if r['new_rule'] and r['oi60'] is not None and (r['oi60']>0)==positive]
        groups.append({'oi_rising':positive,'n':len(selected),'wins':sum(r['net']>0 for r in selected),'net':sum(r['net'] for r in selected)})
    lines+=['','## 같은 신규 규칙 적용 후 진입만 비교','',
            *[f"- 진입 전 1시간 OI {'증가' if g['oi_rising'] else '감소·보합'}: {g['n']}건, 수익 {g['wins']}건, 추정 순손익 {g['net']:+.2f} USDT." for g in groups],
            '- 표본이 작고 시장 시기가 다르므로 증가/감소만으로 진입을 허용하거나 금지할 근거는 아닙니다.',
            '- OI 증가에는 롱과 숏 계약이 함께 늘어납니다. 가격과 결합해도 신규 롱/숏 주도 여부를 확정할 수 없습니다.',
            '- OI 변화와 이후 손익의 동반 관측이며 인과관계나 선행 예측력 검증은 아닙니다.',
            '- 원본 DB와 매매 로직은 변경하지 않았습니다.']
    (src.ROOT/'WEEKLY_OI_20261002.md').write_text('\n'.join(lines),encoding='utf-8')
    (src.ROOT/'weekly_oi_20261002.json').write_text(json.dumps({'rows':result,'groups':groups},indent=2),encoding='utf-8')
    print(json.dumps({'rows':result,'groups':groups},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
