# 보고서 변경 사항 (2026-09-20)

## 변경 범위

- `market_state_engine/live_trader.py`: 기록 전용 진입 점수와 청산 직전 상태 복사.
- `market_state_engine/trade_report.py`: 매매 Telegram 메시지 표시 전용 모듈.
- `examples/run_live_loop.py`: 기존 이벤트 formatter에서 새 매매 보고서 호출.
- `market_state_engine/prediction_summary.py`: 누적 MAE 및 persistence 비교 집계/표시.
- `market_state_engine/prediction_research.py`: 기존 예측 기록을 읽는 평가용 조회 추가.
- `tests/test_trade_reports.py`, `tests/test_persistence_summary.py`: 관련 테스트 추가.

예측 계산, 유사 사례 선택, tolerance, min_case_count, 중앙값 계산, 매매 조건,
Binance 주문 호출, LIVE_ADD, 점수 변화 알림, 일반 상태 보고서는 변경하지 않았다.
매시간 예측 저장과 KST 00/06/12/18 Telegram 발송도 그대로다.
forecast version은 `per_side_changes_v2`이며 DB 스키마 변경이나 기존 데이터 삭제는 없다.

## 매매 이벤트 기록

포지션 상태에 `entry_long_score`, `entry_short_score`를 저장하고 재시작 시 복원한다.
진입 이벤트에는 두 점수와 `entry_time`, `entry_price`, `stop_price`를 복사한다.
청산 이벤트에는 두 점수, `entry_time`, `exit_time`, `peak_profit_pct`,
`exit_time_basis=bot_observation`을 복사한다. 기존 필드는 유지한다.
거래소 포지션 종료 감지 이벤트에도 현재 점수와 추정 수익률을 기록한다.
예전 상태에 없는 진입 점수는 현재 점수로 추측하지 않고 `N/A`로 표시한다.

현재 주문 응답만으로 실제 체결가와 체결시각을 항상 확정할 수 없다.
따라서 보고서는 관측 시각/가격, 추정 손익/수수료임을 명시한다.
기존 PnL 계산이나 주문 응답 처리는 변경하지 않았다.
최고 수익률은 봇이 관측한 값이며 틱 단위의 실제 최고값을 보장하지 않는다.

### 진입 메시지 예시 (가상 값)

```text
🟢 LONG 진입
모드: REAL_ORDER / 상태: SENT
시간: 09-20 10:42 KST (봇 관측 시각)
진입가(판단): 76,120.00

[진입 당시 점수]
LONG 11.00 / SHORT 3.00
Spread +8.00 / Activity 4.00

[포지션]
손절가: 75,380.00
설정 마진: 300.00 USDT / 레버리지: 2.00x
주문 수량 기준 포지션(추정): 608.96 USDT

[진입 근거]
- 추세: +3
- RANGE 돌파/이탈: +3
- 거래량: +5
체결가·체결시각 확정 보고가 아닙니다. 거래소 실제 결과와 다를 수 있습니다.
```

위 실질 금액은 예시 수량 0.008 BTC와 판단 가격의 곱이다.
설정 마진 x 레버리지와 주문 수량 반올림 이후 금액은 다를 수 있다.
보고서만 이를 표시하며 기존 수량 계산은 바꾸지 않는다.

### 청산 메시지 예시 (가상 값)

```text
🔴 LONG 청산
모드: REAL_ORDER / 상태: SENT
시간: 09-20 14:18 KST (봇 관측 시각)
진입가: 76,120.00 / 청산가(관측): 75,690.00
수익률(가격 기준·추정): -0.56%
실현손익(수수료 차감·추정): -4.05 USDT
추정 수수료: 0.61 USDT
보유시간(관측): 3h 36m
청산 이유: stop loss

[점수 변화]
진입 LONG 11.00 / SHORT 3.00 / Spread +8.00
청산 LONG 2.00 / SHORT 9.00 / Spread -7.00
변화 LONG -9.00 / SHORT +6.00
Spread +8.00 → -7.00 (변화 -15.00)
최고 수익률(관측): +0.18%
체결가·체결시각 확정 보고가 아닙니다. 거래소 실제 결과와 다를 수 있습니다.
```

## 예측 평가 기준

- 최근 6시간: 기존처럼 horizon별 최초 `confirmed_at`이 `(now-6h, now]`인 확정 결과.
  기존 confirmed_at 미기록 데이터는 실제 시장 시각으로 대체하고 문구로 알린다.
- 최근 7일: `source_timestamp >= now-7days`인 v2 예측의 확정 결과.
- 전체: DB에 남아 있는 모든 v2 확정 결과. 전략 버전별 분리.
- source와 생성 시각 차이는 0~120초여야 한다. 미래 생성/미래 실제/미래 확정 시각 제외.
- 각 horizon 및 방향이 ready 상태이고 실제 점수와 예측 중앙값이 있어야 한다.
- source/target 시장 상태의 전략 버전 일치를 확인하며 누락되거나 불일치하면 제외한다.
- Telegram 누적 성적은 현재 예측의 전략만 표시한다. 다른 전략 집계는
  기존 `prediction_digest.evaluation_json`의 report 안에 분리 저장한다.
- Persistence baseline은 각 방향의 source 현재 점수를 그대로 미래 점수로 예측한다.
- 같은 루프에서 같은 row의 Forecast 오차와 Persistence 오차를 함께 누적한다.
  표본은 방향별로 같으며, LONG/SHORT 결측이 다르면 두 방향의 건수는 다를 수 있다.
- 개선율 = `(Persistence MAE - Forecast MAE) / Persistence MAE * 100`.
  Persistence MAE가 0이거나 평가가 없으면 N/A.
- 4H 구간은 각 방향의 source 점수로 0~9, 10+를 분류한다.
- 이번 변경은 non-overlapping 평가를 추가하지 않는다.

## 실제 DB 확인 결과

기준: `btc_market_state_20260920_043441.db`, 2026-09-20 13:34:11 KST.
전략 `market_state_engine_v1`, 예측 `per_side_changes_v2`.

| 전체 4H 구간 | Forecast MAE | Persistence MAE | 개선율 | 건수 |
|---|---:|---:|---:|---:|
| LONG 0~9 | 2.57 | 2.39 | -7.3% | 143 |
| LONG 10+ | 5.56 | 5.83 | +4.7% | 35 |
| SHORT 0~9 | 1.89 | 1.82 | -3.6% | 168 |
| SHORT 10+ | 7.20 | 5.80 | -24.1% | 10 |

최근 7일 4H는 방향별 156건, 전체 4H는 방향별 178건이다.
참고 예시 숫자를 하드코딩하지 않고 DB에서 계산했다.

### 예측 Telegram 평가 부분 예시 (실제 DB)

기존 ①~④는 그대로이며 아래 내용이 ⑤에 표시된다.

```text
⑤ 최근 6시간 새로 확정된 결과
LONG/SHORT 평균 절대오차 (점수), 평가 건수
전략: market_state_engine_v1 / per_side_changes_v2
4H: LONG 0.33 (6건) / SHORT 6.00 (6건)
1H: LONG 0.00 (7건) / SHORT 3.57 (7건)
15M: LONG 0.00 (6건) / SHORT 0.83 (6건)
최근 확정 4H (실제 09-20 12:00 KST):
LONG: 예상 0.0 / 실제 0 / 오차 0.0
SHORT: 예상 0.0 / 실제 14 / 오차 14.0

누적 평가 전략: market_state_engine_v1 / per_side_changes_v2
[최근 7일]
4H: LONG 3.53 (156건) / SHORT 2.44 (156건)
1H: LONG 2.83 (163건) / SHORT 2.01 (163건)
15M: LONG 1.22 (164건) / SHORT 0.74 (164건)
[4H vs 현재점수 유지 / 최근 7일]
LONG: Forecast 3.53 / Persistence 3.43 / 개선 -2.9% (156건)
SHORT: Forecast 2.44 / Persistence 2.29 / 개선 -6.4% (156건)
[전체 누적]
4H: LONG 3.15 (178건) / SHORT 2.19 (178건)
1H: LONG 2.55 (185건) / SHORT 1.79 (185건)
15M: LONG 1.11 (186건) / SHORT 0.66 (186건)
[4H vs 현재점수 유지 / 전체 누적]
LONG: Forecast 3.15 / Persistence 3.07 / 개선 -2.8% (178건)
SHORT: Forecast 2.19 / Persistence 2.04 / 개선 -6.9% (178건)
[4H 점수 구간별 / 전체]
LONG 0~9: Forecast 2.57 / Persistence 2.39 / 개선 -7.3% (143건)
LONG 10+: Forecast 5.56 / Persistence 5.83 / 개선 +4.7% (35건)
SHORT 0~9: Forecast 1.89 / Persistence 1.82 / 개선 -3.6% (168건)
SHORT 10+: Forecast 7.20 / Persistence 5.80 / 개선 -24.1% (10건)
Persistence baseline = 현재 점수 유지. 개선율 양수는 Forecast 우위.
미확정·자료 부족은 오답으로 계산하지 않습니다.
평가 건수에는 서로 겹치는 시간대의 예측이 포함됩니다.
점수 예측은 가격·매매 수익 예측이 아닙니다. 사례에는 겹치는 1분 기록이 포함됩니다.
```

## 점수 구성 재구성 가능 여부

YES. 현재 recorder는 reasons, 구성요소별 점수, RANGE 감점/가점, 원본 지표를 함께 저장한다.
따라서 한 snapshot으로 그 점수의 근거를 상당 부분 설명할 수 있다.
원래 1년 원자료 전체를 다시 계산하는 것과는 다르며, 구형/결측 snapshot에는 한계가 있다.

## 검증

`python -m unittest discover -s tests -q`: 전체 76개 통과 (기존 60 + 신규 16).
실제 DB 기반 완성 Telegram 메시지: UTF-16 기준 2,043자.
기존 ①~④ 유지, 여러 전략 표시 시 길이, 점수 구간, 같은 표본 비교,
미확정/결측/ready=False/stale 제외, 거래 이벤트 및 재시작 저장을 테스트했다.
실제 주문이나 Telegram 전송은 실행하지 않았다. GitHub/GCP 배포는 별도다.
