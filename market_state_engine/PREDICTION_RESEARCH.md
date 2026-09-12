# 점수 예측 연구 v2

실제 점수 계산, 주문, 진입 및 청산 규칙은 바꾸지 않습니다.
Binance 호출 없이 기존 SQLite 시장 기록만 사용합니다.

## 무엇을 예측하나요?

- 15분, 1시간, 4시간 뒤 LONG과 SHORT 점수를 각각 추정합니다.
- 예: `LONG: 4 -> 예상 11.0 (+7.0) [8.0~14.0]`.
  위 숫자는 설명용입니다. 괄호는 현재 LONG 대비 변화이며 LONG-SHORT 차이가 아닙니다.
- 예상 점수는 중앙값이고, 대괄호는 과거 사례를 현재 점수에 적용한 10~90백분위 범위입니다.
  확정된 미래나 보장된 신뢰구간이 아닙니다.
- 각 점수가 현재보다 7점 또는 10점 이상 상승/하락할 확률을 함께 저장합니다.
- 평균 예상 점수, 중앙값, 범위, 확률은 `forecast_json`에 저장합니다.
- 기존 ATR 방향 가점을 제외한 LONG-SHORT 차이의 3점 변화 예측은 비교용으로 유지합니다.

## 과거 사례 선택

기존 방식대로 같은 종목과 strategy_version에서 현재 LONG, SHORT, ATR 활동도 점수가
각각 1점 이내인 과거 기록을 찾습니다. 각 방향의 시작 점수는 0~9와 10 이상을 구분합니다.
과거의 이후 점수 변화량을 현재 점수에 적용하되 0점 미만으로 내리지 않습니다.
해당 예측 시점에 이미 결과를 알 수 있었던 사례만 사용합니다. 최소 사례 수는 30개입니다.

1분 기록들이 겹치므로 사례 1,000개가 독립적인 상황 1,000개를 뜻하지 않습니다.
점수 규칙이 바뀌었는데 strategy_version이 같다면 과거 규칙의 자료가 섞일 수 있습니다.
앞으로 규칙 변경 시 전략 버전을 함께 관리해야 합니다.

## 기록과 보고 일정

- 매시간 예측 1건을 저장합니다. 정상 실행 시 하루 24건, 일주일 168건입니다.
- KST 00/06/12/18시에는 아래 5개 항목으로 구성된 요약 보고서를 보냅니다.
- 최근 7일 평가 통계는 계속 계산하고 DB의 보고 기록에 보존하되 텔레그램에서는 생략합니다.
- 성적은 점수의 평균 절대오차와 7/10점 상승·하락·유지 분류 적중 건수입니다.
- 실제 큰 변화를 맞힌 건수와 시작 점수 구간별 결과도 구분합니다.
- 아직 시간이 안 지난 결과는 대기, 시간이 지났지만 데이터가 없는 결과는 누락입니다.
  둘 다 실패 예측으로 계산하지 않습니다.
- 15분 결과도 4시간 결과를 기다리지 않고 다음 연구 실행에서 별도로 갱신합니다.
  매시간 실행이므로 실제 결과 반영까지 추가 지연이 있을 수 있습니다.
- 최신 시장 기록이 2분보다 오래되면 새 예측을 만들지 않습니다.
- 꺼져 있던 시간의 예측을 나중에 실제 발행한 예측처럼 채우지 않습니다.

## 데이터 보존

기본 DB: 프로젝트의 `work/data/btc_market_state.db`.

`prediction_forecast`에 `forecast_version`, `outcomes_complete` 컬럼을 자동 추가합니다.
새 예측 버전은 `per_side_changes_v2`입니다. 기존 예측 JSON은 덮어쓰지 않습니다.
`actual_outcomes_json`에는 각 시점의 실제 LONG/SHORT 및 현재 대비 변화량을 보충합니다.
새 `prediction_digest` 테이블에는 보고서 내용, 평가 통계, 전송 시각을 저장합니다.

같은 종목/시간의 예측은 중복 생성하지 않습니다. 전송 실패 시 같은 보고서를 재시도하며,
전송 완료가 DB에 기록된 보고서는 다시 보내지 않습니다. 외부 전송 성공 직후 프로세스가
중단되어 완료 기록을 못 남긴 경우에는 중복 전송 가능성이 있습니다.

## GCP 적용

로컬 변경을 GitHub에 올린 뒤 GCP 프로젝트 폴더에서 `git pull` 합니다.
매매 봇을 재시작할 필요는 없습니다. 연구 작업은 cron이 실행하는 별도 프로세스입니다.

```bash
cd /home/chlghks343/test-bot/python_bot/market-state-engine
mkdir -p work/logs
crontab -e
```

기존 prediction_research cron 한 줄을 아래 줄로 교체합니다. 다른 정리 작업은 유지합니다.
추가 등록이 아니라 교체해야 합니다. `flock`은 연구 작업이 동시에 실행되지 않게 합니다.

```cron
2 * * * * cd /home/chlghks343/test-bot/python_bot/market-state-engine && /usr/bin/flock -n /tmp/btc-score-research.lock /home/chlghks343/test-bot/python_bot/venv/bin/python -m market_state_engine.prediction_research --send-telegram >> work/logs/prediction_research.log 2>&1
```

매시 2분에 실행하므로 보고 시간도 KST 00:02/06:02/12:02/18:02경입니다.
KST 보고 시간은 코드가 판단합니다. `CRON_TZ` 설정에 의존하지 않습니다.
텔레그램은 기존 `TELEGRAM5_TOKEN`과 `TELEGRAM_CHAT_ID`를 사용합니다.

수동 미리보기는 아래와 같습니다. DB에는 저장하지만 텔레그램은 보내지 않습니다.
같은 시간에 이미 저장한 예측은 재사용합니다.

```bash
/home/chlghks343/test-bot/python_bot/venv/bin/python -m market_state_engine.prediction_research --force
```

`--force --send-telegram`은 정기 보고 시간 밖에도 수동 전송하므로 평소 cron에는
`--force`를 넣지 않습니다. 로그 파일은 계속 커지므로 서버의 로그 순환 정책으로 관리합니다.

## 테스트

```bash
python -m unittest discover -s tests -v
```

매시간 저장, 하루 4번 전송, 재시작 중복 방지, 실패 전송 재시도, 과거 기록 보존,
미래 정보 차단, 시점별 실제 결과 갱신, 시작 점수 구간 및 7/10점 확률을 검사합니다.
테스트에서는 텔레그램 전송을 모의 처리하며 실제 주문은 실행하지 않습니다.

## 4시간 예측 vs Persistence 평가

별도 평가 명령은 DB를 읽기 전용으로 열며, 예측 생성이나 라벨 갱신도 하지 않습니다.
프로젝트 폴더에서 실행합니다. cron이나 라이브 봇 실행 설정을 바꿀 필요는 없습니다.

```bash
python -m market_state_engine.prediction_evaluation
python -m market_state_engine.prediction_evaluation --json
```

가상환경 밖의 GCP 터미널에서는 `python` 대신 다음 경로를 사용합니다.
`/home/chlghks343/test-bot/python_bot/venv/bin/python`

다른 백업 파일은 `--market-state-db-path 경로`로 지정합니다.
`--as-of 2026-09-12T01:00:00+00:00`처럼 평가 기준 시각도 지정할 수 있습니다.
현재 저장된 결과만 사용하며, 당시 평가 보고서를 복원하는 기능은 아닙니다.

기존 컬럼 사용:
- `prediction_forecast`: source_timestamp, created_at, source_long_score,
  source_short_score, strategy_version, forecast_json, actual_outcomes_json.
- `forecast_json`: forecast_version=`per_side_changes_v2`,
  horizons.4h.score_changes.LONG/SHORT의 ready, start_score, median_score 및 확률.
- `market_state`: 원본 행의 future_4h_timestamp, future_4h_long_score,
  future_4h_short_score와 원본/실제 미래 행의 strategy_version 및 점수.
- 원본 라벨 시각이 없으면 actual_outcomes_json의 4h 결과를 사용하되,
  실제 미래 행에서 버전과 점수가 일치하는지 검증합니다.
  새 컬럼이나 테이블을 만들지 않습니다.

평가 방법:
- 모델 예상값은 기존 보고서와 동일하게 중앙값(median_score)입니다.
- Persistence 예상값은 예측 시점의 동일 방향 점수입니다.
- LONG/SHORT별로 동일한 유효 표본에서 MAE를 비교합니다.
- 개선율 = (Persistence MAE - 모델 MAE) / Persistence MAE * 100.
  양수는 모델 개선, 음수는 악화입니다. Persistence MAE가 0이면 N/A입니다.
- 전체 표본과 비중첩 표본을 각각 평가합니다. 전략별로 예측의 원본 시각순으로
  가장 이른 유효 표본부터 선택하고, 다음 표본은 최소 4시간 뒤로 제한합니다.
  실제 라벨이 4시간보다 늦으면 그 실제 시각까지 겹치지 않게 합니다.
  이는 상관성을 줄이는 점검이지 표본의 통계적 독립성을 보장하지 않습니다.
- 한쪽만 유효하면 해당 방향만 평가하므로 LONG/SHORT의 n이 다를 수 있습니다.
- ±7/±10점 확률은 기존의 최고확률 분류를 사용합니다. 동률 우선순위는 유지,
  상승, 하락입니다. up/down별 precision, recall, 실제/예측 건수, TP를 표시합니다.
- any_change는 방향을 무시한 큰 변화 발생 여부입니다. 방향을 반대로 예측해도
  양쪽 모두 큰 변화면 any_change에서는 맞힌 것으로 보므로 up/down도 함께 봅니다.
- precision/recall은 0~1 값입니다. 분모가 0이면 N/A이며 확률이 없으면 별도 집계합니다.
- 전략 버전별로만 출력합니다. 버전 미상, 원본·예측·실제 버전 불일치,
  4시간 도중 다른 버전으로 바뀐 구간은 평가에서 제외합니다.
- 미확정 결과, 누락 라벨, 구버전 예측, 오래된 원본 기록, 자료 부족도 제외 건수로
  표시하며 오답으로 넣지 않습니다. 서로 다른 규칙에 같은 버전명을 쓴 것은 탐지할 수 없습니다.

새 v2로 발행한 예측이 아직 없다면 평가 수가 0입니다. 옛 기록으로 새 예측을
다시 만들어 과거에 실제 발행한 것처럼 평가하지 않습니다.

## 텔레그램 보고서 구성

1. 현재 상태: 예측에 사용한 BTC 가격, LONG/SHORT, Spread(LONG-SHORT), 시장 기록 시각.
2. 4시간 전망: 각 방향의 현재 점수, 예상 중앙값, 변화량, 10~90백분위 범위,
   ±7/±10점 이상 변화 확률 및 사례 수. Spread는 두 예상 중앙값의 차이로 표시합니다.
   두 중앙값의 차이를 Spread 자체 분포의 중앙값이라고 해석하면 안 됩니다.
3. 단기 전망: 1H와 15M의 LONG/SHORT 예상 점수 및 현재 대비 변화량을 각각 한 줄로 표시.
4. 지난 변화: 현재 예측의 시장 기록 시각에서 1시간 전 실제 시장 점수와 비교합니다.
   해당 시각보다 2분 넘게 오래된 기록은 대체하지 않고 자료 없음을 표시합니다.
   직전 저장 예측과 최신 4H 전망도 비교하며, 두 목표 시각을 모두 적습니다.
   이는 서로 다른 목표 시각의 rolling forecast 비교이지 동일 목표 시각의 수정 예측이 아닙니다.
   전략/예측 버전이 달라지면 비교하지 않습니다.
5. 최근 6시간 새로 확정된 결과: 전략/예측 버전별로 4H/1H/15M 각각 LONG/SHORT의
   평균 절대오차와 평가 건수를 표시합니다. 최근 확정된 4H 1건의 예상/실제/절대오차도 표시합니다.
   버전이 여러 개이면 텔레그램에는 최대 3개를 표시하고 전체 집계는 DB에 보존합니다.

보고용 비교 데이터는 기존 prediction_digest.evaluation_json의 report 항목에 저장합니다.
prediction_forecast의 기존 actual_outcomes_json에 각 horizon의 confirmed_at을 보충해,
처음 실제 점수가 확인된 시각을 기억합니다. 다른 horizon이 나중에 확정돼도 이 시각은 바뀌지 않습니다.
기존 예측 JSON, 실제 점수, DB 테이블 구조와 예측 계산 방식은 바꾸지 않습니다.

이미 확정됐지만 confirmed_at이 없던 기존 기록은 과거에 언제 확인됐는지 복원할 수 없습니다.
그 경우 실제 시장 결과 시각으로 집계하고 보고서에 이 제한을 명시합니다.
미확정 결과와 예측 자료 부족은 오답이 아니며, 실제 결과 시점의 전략이 다른 경우도 제외합니다.

cron은 매시간 실행해도 됩니다. --send-telegram만으로 매시간 전송되는 것은 아닙니다.
코드가 KST 00/06/12/18시인지 확인한 뒤 전송합니다. 평소 cron에는 --force를 넣지 마세요.
이미 저장한 같은 시간대의 보고서는 재시도 시 원문을 재사용하므로, 업데이트 직후 같은 시간대
미리보기에는 이전 양식이 남아 있을 수 있습니다. 다음 보고서부터 새 양식으로 생성됩니다.
