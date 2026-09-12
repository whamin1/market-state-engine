"""Statistics and human-readable digests for research-only score forecasts."""

from datetime import datetime, timedelta, timezone
from statistics import mean


KST = timezone(timedelta(hours=9))
HORIZON_MINUTES = {"15m": 15, "1h": 60, "4h": 240}


def score_band(score):
    return "0_9" if score < 10 else "10_plus"


def _quantile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def summarize_score_changes(current_score, cases, min_case_count):
    # Apply historical changes to today's score, keeping the starting bands separate.
    changes = [end - start for start, end in cases
               if start is not None and end is not None and score_band(start) == score_band(current_score)]
    result = {"start_score": current_score, "start_band": score_band(current_score),
              "case_count": len(changes), "ready": len(changes) >= min_case_count}
    if not changes:
        return result
    scores = [max(0, current_score + change) for change in changes]
    # Keep probabilities consistent with the projected scores' nonnegative floor.
    changes = [score - current_score for score in scores]
    result.update({
        "expected_score": mean(scores),
        "median_score": _quantile(scores, 0.5),
        "score_p10": _quantile(scores, 0.1),
        "score_p90": _quantile(scores, 0.9),
        "median_change": _quantile(changes, 0.5),
    })
    for threshold in (7, 10):
        result[f"up_{threshold}_pct"] = 100 * sum(x >= threshold for x in changes) / len(changes)
        result[f"down_{threshold}_pct"] = 100 * sum(x <= -threshold for x in changes) / len(changes)
        result[f"stable_{threshold}_pct"] = 100 * sum(-threshold < x < threshold for x in changes) / len(changes)
    return result


def _classify(change, threshold):
    return "up" if change >= threshold else "down" if change <= -threshold else "stable"


def _most_likely(probabilities):
    # On a tie retain the neutral class; do not manufacture a directional signal.
    return max(("stable", "up", "down"), key=lambda key: probabilities[key])


def _empty_metric():
    return {"count": 0, "hits": 0, "always_stable_hits": 0,
            "actual_changes": 0, "change_hits": 0, "brier_sum": 0.0}


def _update_metric(metric, probabilities, actual):
    prediction = _most_likely(probabilities)
    metric["count"] += 1
    metric["hits"] += int(prediction == actual)
    metric["always_stable_hits"] += int(actual == "stable")
    metric["actual_changes"] += int(actual != "stable")
    metric["change_hits"] += int(prediction == actual and actual != "stable")
    metric["brier_sum"] += sum((probabilities[key] / 100 - int(key == actual)) ** 2
                                for key in ("up", "down", "stable"))


def evaluate_forecasts(records, now, max_snapshot_age_seconds=120):
    output = {"record_count": len(records), "stale_excluded": 0, "horizons": {}}
    for name in HORIZON_MINUTES:
        output["horizons"][name] = {
            "baseline": _empty_metric(), "pending": 0, "missing_actual": 0,
            "sides": {side: {"7": _empty_metric(), "10": _empty_metric(),
                             "bands": {band: _empty_metric() for band in ("0_9", "10_plus")},
                             "score_error_sum": 0.0, "score_error_count": 0}
                      for side in ("LONG", "SHORT")},
        }
    for record in records:
        source_time = datetime.fromisoformat(record["source_timestamp"])
        age = (datetime.fromisoformat(record["created_at"]) - source_time).total_seconds()
        if age < 0 or age > max_snapshot_age_seconds:
            output["stale_excluded"] += 1
            continue
        forecast = record["forecast"]
        for name, minutes in HORIZON_MINUTES.items():
            metrics = output["horizons"][name]
            actual = (record.get("actual_outcomes") or {}).get(name) or {}
            actual_time = actual.get("timestamp")
            if not actual_time or datetime.fromisoformat(actual_time) > now:
                metrics["pending" if now < source_time + timedelta(minutes=minutes) else "missing_actual"] += 1
                continue
            predicted = forecast["horizons"][name]
            if predicted.get("ready") and actual.get("core_score_change") is not None:
                probabilities = {"up": predicted["long_strengthen_pct"],
                                 "down": predicted["short_strengthen_pct"], "stable": predicted["stable_pct"]}
                _update_metric(metrics["baseline"], probabilities, _classify(actual["core_score_change"], 3))
            for side in ("LONG", "SHORT"):
                item = (predicted.get("score_changes") or {}).get(side) or {}
                actual_score = actual.get(f"{side.lower()}_score")
                if not item.get("ready") or actual_score is None:
                    continue
                change = actual_score - item["start_score"]
                side_metrics = metrics["sides"][side]
                side_metrics["score_error_sum"] += abs(actual_score - item["median_score"])
                side_metrics["score_error_count"] += 1
                for threshold in (7, 10):
                    probabilities = {key: item[f"{key}_{threshold}_pct"] for key in ("up", "down", "stable")}
                    classification = _classify(change, threshold)
                    _update_metric(side_metrics[str(threshold)], probabilities, classification)
                    if threshold == 7:
                        _update_metric(side_metrics["bands"][item["start_band"]], probabilities, classification)
    return output


def _ratio(metric):
    return f"{metric['hits']}/{metric['count']}" if metric["count"] else "평가 없음"


def format_forecast_scores(forecast):
    lines = ["current_scores:", f"LONG {forecast['long_score']} / SHORT {forecast['short_score']} / ATR {forecast['atr_activity_score']}",
             "예상 점수: 중앙값 [과거 사례의 10~90% 범위]"]
    for name in HORIZON_MINUTES:
        horizon = forecast["horizons"][name]
        lines.append(name)
        for side in ("LONG", "SHORT"):
            item = (horizon.get("score_changes") or {}).get(side) or {}
            if not item.get("ready"):
                lines.append(f" {side}: 사례 부족 ({item.get('case_count', 0)})")
                continue
            change = item["median_score"] - item["start_score"]
            lines.append(f" {side}: {item['start_score']} -> 예상 {item['median_score']:.1f} ({change:+.1f})"
                         f" [{item['score_p10']:.1f}~{item['score_p90']:.1f}] 사례 {item['case_count']}")
            lines.append(f" +7 {item['up_7_pct']:.0f}% / -7 {item['down_7_pct']:.0f}% | +10 {item['up_10_pct']:.0f}% / -10 {item['down_10_pct']:.0f}%")
    return lines


def format_prediction_digest(forecast, recent_records, evaluation, now, days=7, warning=None):
    lines = ["BTCUSDT 점수 예측 연구", now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")]
    if warning:
        lines.append(warning)
    if forecast:
        lines.extend(["", *format_forecast_scores(forecast)])
    lines.extend(["", "최근 6시간 예측의 실제 변화 (LONG/SHORT)"])
    for record in recent_records[-6:]:
        actual = record.get("actual_outcomes") or {}
        source = record["forecast"]
        parts = []
        for name, minutes in HORIZON_MINUTES.items():
            item = actual.get(name) or {}
            if item.get("long_score") is not None and item.get("short_score") is not None:
                parts.append(f"{name} {item['long_score'] - source['long_score']:+d}/{item['short_score'] - source['short_score']:+d}")
            else:
                due = datetime.fromisoformat(record["source_timestamp"]) + timedelta(minutes=minutes)
                parts.append(f"{name} {'대기' if now < due else '자료 없음'}")
        lines.append(f"{record['schedule_key'][-2:]}시: " + " | ".join(parts))
    if not recent_records:
        lines.append("기록 없음")
    lines.extend(["", f"최근 {days}일 성적: 최고확률 분류 적중/평가건수", "7·10 = 각 점수의 상승/하락/유지 분류"])
    for name, metrics in evaluation["horizons"].items():
        baseline = metrics["baseline"]
        lines.append(f"{name}: 기존 점수차3점(비교용) {_ratio(baseline)} (항상유지 {baseline['always_stable_hits']}/{baseline['count']})")
        for side, stats in metrics["sides"].items():
            metric = stats["7"]
            lines.append(f" {side}: 7점 {_ratio(metric)}, 10점 {_ratio(stats['10'])}; 실제변화 포착 {metric['change_hits']}/{metric['actual_changes']}")
            if stats["score_error_count"]:
                error = stats["score_error_sum"] / stats["score_error_count"]
                lines.append(f"  예상 점수 평균 절대오차: {error:.1f}점 ({stats['score_error_count']}건)")
        low = " / ".join(f"{side} {_ratio(stats['bands']['0_9'])}" for side, stats in metrics["sides"].items())
        high = " / ".join(f"{side} {_ratio(stats['bands']['10_plus'])}" for side, stats in metrics["sides"].items())
        lines.append(f" 시작0~9: {low}; 시작10+: {high}")
        lines.append(f" 평가대기 {metrics['pending']} / 결과누락 {metrics['missing_actual']}")
    lines.extend(["", "점수 변화 예측이며 가격 방향·매매 수익률 예측이 아닙니다.",
                  "사례 수에는 겹치는 1분 기록이 포함됩니다. +7/-7 및 +10/-10은 이상 변화 확률입니다."])
    return "\n".join(lines)
