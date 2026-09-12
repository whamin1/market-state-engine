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
- KST 00/06/12/18시에는 최신 예측, 최근 6시간의 실제 변화, 최근 7일 성적을 보냅니다.
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
