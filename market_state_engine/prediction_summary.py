"""Statistics and human-readable digests for research-only score forecasts."""

from datetime import datetime, timedelta, timezone
from statistics import mean
from math import isfinite


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


def _report_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def summarize_recent_actuals(records, now):
    """Summarize first confirmations, not the creation times of their forecasts."""
    since = now - timedelta(hours=6)
    groups = {}
    for record in records:
        forecast = record.get("forecast") or {}
        strategy = forecast.get("strategy_version")
        version = forecast.get("forecast_version")
        if not strategy or not version:
            continue
        group = groups.setdefault((strategy, version), {
            "strategy_version": strategy, "forecast_version": version,
            "legacy_time_count": 0, "horizons": {
                name: {"LONG": [], "SHORT": [], "latest": None} for name in HORIZON_MINUTES},
        })
        source_time = _report_time(record["source_timestamp"])
        if not 0 <= (_report_time(record["created_at"]) - source_time).total_seconds() <= 120:
            continue
        for name, minutes in HORIZON_MINUTES.items():
            actual = (record.get("actual_outcomes") or {}).get(name) or {}
            if not actual.get("timestamp") or actual.get("report_strategy_matches") is False:
                continue
            actual_time = _report_time(actual["timestamp"])
            if not source_time + timedelta(minutes=minutes) <= actual_time <= now:
                continue
            confirmed = _report_time(actual.get("confirmed_at") or actual["timestamp"])
            if not since < confirmed <= now:
                continue
            items = (forecast.get("horizons", {}).get(name) or {}).get("score_changes") or {}
            comparison = {}
            for side in ("LONG", "SHORT"):
                item = items.get(side) or {}
                score = actual.get(f"{side.lower()}_score")
                predicted = item.get("median_score")
                if not item.get("ready") or score is None or predicted is None:
                    continue
                error = abs(score - predicted)
                group["horizons"][name][side].append(error)
                comparison[side] = {"predicted": predicted, "actual": score, "absolute_error": error}
            if comparison:
                if not actual.get("confirmed_at"):
                    group["legacy_time_count"] += 1
                latest = group["horizons"][name]["latest"]
                if latest is None or confirmed > _report_time(latest["confirmed_at"]):
                    group["horizons"][name]["latest"] = {
                        "source_timestamp": record["source_timestamp"], "actual_timestamp": actual["timestamp"],
                        "confirmed_at": confirmed.isoformat(), "sides": comparison,
                    }
    for group in groups.values():
        for stats in group["horizons"].values():
            for side in ("LONG", "SHORT"):
                errors = stats[side]
                stats[side] = {"count": len(errors), "mae": mean(errors) if errors else None}
    return list(groups.values())


def _forecast_item(forecast, name, side):
    item = ((forecast.get("horizons") or {}).get(name) or {}).get("score_changes", {}).get(side) or {}
    return item if item.get("ready") and item.get("median_score") is not None else None


def summarize_persistence_performance(records, now, max_snapshot_age_seconds=120):
    """Paired median/persistence errors only; no change to forecast generation."""
    def accumulator():
        return {"count": 0, "forecast_error_sum": 0.0, "persistence_error_sum": 0.0}

    def period():
        return {"horizons": {name: {side: accumulator() for side in ("LONG", "SHORT")}
                             for name in HORIZON_MINUTES},
                "bands_4h": {side: {band: accumulator() for band in ("0_9", "10_plus")}
                             for side in ("LONG", "SHORT")}}

    def add(metric, predicted, current, actual):
        metric["count"] += 1
        metric["forecast_error_sum"] += abs(predicted - actual)
        metric["persistence_error_sum"] += abs(current - actual)

    groups = {}
    for record in records:
        forecast = record.get("forecast") or {}
        strategy = forecast.get("strategy_version")
        if (forecast.get("forecast_version") != "per_side_changes_v2" or not strategy
                or record.get("forecast_version", "per_side_changes_v2") != "per_side_changes_v2"
                or record.get("strategy_version", strategy) != strategy
                or record.get("report_source_matches") is False):
            continue
        source = _report_time(record["source_timestamp"])
        created = _report_time(record["created_at"])
        if created > now or not 0 <= (created - source).total_seconds() <= max_snapshot_age_seconds:
            continue
        group = groups.setdefault(strategy, {"strategy_version": strategy, "forecast_version": "per_side_changes_v2",
                                             "last_7_days": period(), "all_time": period()})
        periods = [group["all_time"]]
        if source >= now - timedelta(days=7):
            periods.append(group["last_7_days"])
        for name, minutes in HORIZON_MINUTES.items():
            actual = (record.get("actual_outcomes") or {}).get(name) or {}
            if not actual.get("timestamp") or actual.get("report_strategy_matches") is False:
                continue
            actual_time = _report_time(actual["timestamp"])
            if not source + timedelta(minutes=minutes) <= actual_time <= now:
                continue
            if actual.get("confirmed_at") and _report_time(actual["confirmed_at"]) > now:
                continue
            horizon = (forecast.get("horizons") or {}).get(name) or {}
            if horizon.get("ready") is False:
                continue
            for side in ("LONG", "SHORT"):
                item = (horizon.get("score_changes") or {}).get(side) or {}
                current = forecast.get(f"{side.lower()}_score")
                predicted, score = item.get("median_score"), actual.get(f"{side.lower()}_score")
                if not item.get("ready") or not all(isinstance(x, (int, float)) and isfinite(x)
                                                    for x in (current, predicted, score)):
                    continue
                for stats in periods:
                    add(stats["horizons"][name][side], predicted, current, score)
                    if name == "4h":
                        add(stats["bands_4h"][side][score_band(current)], predicted, current, score)
    for group in groups.values():
        for key in ("last_7_days", "all_time"):
            stats = group[key]
            metrics = [m for h in stats["horizons"].values() for m in h.values()]
            metrics += [m for side in stats["bands_4h"].values() for m in side.values()]
            for metric in metrics:
                count = metric["count"]
                model = metric["forecast_error_sum"] / count if count else None
                baseline = metric["persistence_error_sum"] / count if count else None
                metric.update(forecast_mae=model, persistence_mae=baseline,
                              improvement_pct=(baseline - model) / baseline * 100 if baseline else None)
    return list(groups.values())


def _format_persistence_performance(groups, strategy):
    # Telegram shows the current strategy; all separated groups remain in evaluation_json.
    group = next((item for item in groups if item["strategy_version"] == strategy), None)
    if group is None:
        return ["", "[최근 7일 / 전체]", "현재 전략의 채점 자료 없음"]
    def number(value):
        return "자료 없음" if value is None else f"{value:.2f}"

    def comparison(metric):
        return (f"우리 {number(metric['forecast_mae'])} / 유지 {number(metric['persistence_mae'])}"
                f" → {_performance_verdict(metric['improvement_pct'])} (채점 {metric['count']}건)")

    lines = ["", f"채점 전략: {strategy[:48]}"]
    for key, label in (("last_7_days", "최근 7일"), ("all_time", "전체")):
        stats = group[key]
        lines.append(f"[{label} · 4시간 평균 오차]")
        for side in ("LONG", "SHORT"):
            lines.append(f"{side}: {comparison(stats['horizons']['4h'][side])}")
    lines.append("[시작 점수 10점 이상 · 전체]")
    for side in ("LONG", "SHORT"):
        metric = group['all_time']['bands_4h'][side]['10_plus']
        lines.append(f"{side}: {_performance_verdict(metric['improvement_pct'])} (채점 {metric['count']}건)")
    return lines


def _display_number(value, signed=False, comma=False):
    if value is None:
        return "자료 없음"
    text = format(value, ("+" if signed else "") + ("," if comma else "") + ".1f")
    return text.rstrip("0").rstrip(".")


def _performance_verdict(value):
    if value is None:
        return "우열 비교 불가"
    if round(value, 1) == 0:
        return "우리 예측과 유지 비슷함"
    return f"우리 예측 {abs(value):.1f}% {'우세' if value > 0 else '뒤짐'}"


def format_prediction_digest(forecast, recent_records, evaluation, now, days=7, warning=None, context=None):
    context = context or {}
    lines = ["BTCUSDT 점수 예측 연구", now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")]
    if warning:
        lines.append(warning)
    lines.extend(["", "① 현재 상태"])
    if forecast:
        spread = forecast["long_score"] - forecast["short_score"]
        lines.extend([f"BTC: {_display_number(forecast.get('price'), comma=True)}",
                      f"LONG {_display_number(forecast['long_score'])} / SHORT {_display_number(forecast['short_score'])} / 점수 차이 {spread:+g}",
                      f"기준: {_report_time(forecast['source_timestamp']).astimezone(KST):%m-%d %H:%M} KST",
                      "", "② 4시간 전망", "예상 점수: 과거 비슷한 경우의 중간값",
                      "범위: 과거 비슷한 경우의 10~90% 범위"])
        medians = {}
        for side in ("LONG", "SHORT"):
            item = _forecast_item(forecast, "4h", side)
            if not item:
                lines.append(f"{side}: 사례 부족")
                continue
            medians[side] = item["median_score"]
            change = item["median_score"] - item["start_score"]
            lines.extend([f"{side} 현재 {_display_number(item['start_score'])} → 예상 {_display_number(item['median_score'])} ({_display_number(change, signed=True)})",
                          f" 범위 {_display_number(item['score_p10'])}~{_display_number(item['score_p90'])} / 비슷한 사례 {item['case_count']}건",
                          f" 7점 이상 상승 {item['up_7_pct']:.0f}% / 하락 {item['down_7_pct']:.0f}%",
                          f" 10점 이상 상승 {item['up_10_pct']:.0f}% / 하락 {item['down_10_pct']:.0f}%"])
        if len(medians) == 2:
            future_spread = medians["LONG"] - medians["SHORT"]
            lines.append(f"예상 점수 차이: {_display_number(future_spread, signed=True)} (현재보다 {_display_number(future_spread - spread, signed=True)})")
            lines.append("점수 차이 = LONG - SHORT")
        lines.extend(["", "③ 단기 전망"])
        for name in ("1h", "15m"):
            parts = []
            for side in ("LONG", "SHORT"):
                item = _forecast_item(forecast, name, side)
                parts.append(f"{side} {_display_number(item['median_score'])} ({_display_number(item['median_score'] - item['start_score'], signed=True)})"
                             if item else f"{side} 사례 부족")
            lines.append(f"{'1시간' if name == '1h' else '15분'} 뒤: " + " / ".join(parts))
    else:
        lines.append("현재 예측 없음")
    lines.extend(["", "④ 지난 변화"])
    earlier = context.get("previous_snapshot")
    if (forecast and earlier and earlier.get("strategy_version") == forecast.get("strategy_version")
            and all(earlier.get(f"{side}_score") is not None for side in ("long", "short"))):
        lines.append(f"1시간 전 실제 점수 ({_report_time(earlier['timestamp']).astimezone(KST):%H:%M} KST) 대비:")
        for side in ("LONG", "SHORT"):
            before, current = earlier[f"{side.lower()}_score"], forecast[f"{side.lower()}_score"]
            lines.append(f"{side}: {_display_number(before)} → {_display_number(current)} ({current - before:+g})")
    else:
        lines.append("1시간 전 비교: 자료 없음 또는 전략 변경")
    previous = (context.get("previous_forecast") or {}).get("forecast")
    if (forecast and previous and previous.get("strategy_version") == forecast.get("strategy_version")
            and previous.get("forecast_version") == forecast.get("forecast_version")):
        old_target = _report_time(previous["source_timestamp"]) + timedelta(hours=4)
        new_target = _report_time(forecast["source_timestamp"]) + timedelta(hours=4)
        lines.extend(["직전 4시간 예측과 비교:",
                      f"지난 예측 목표: {_report_time(old_target).astimezone(KST):%m-%d %H:%M} KST",
                      f"이번 예측 목표: {_report_time(new_target).astimezone(KST):%m-%d %H:%M} KST"])
        for side in ("LONG", "SHORT"):
            old, new = _forecast_item(previous, "4h", side), _forecast_item(forecast, "4h", side)
            lines.append(f"{side} 예상 {_display_number(old['median_score'])} → {_display_number(new['median_score'])}"
                         f" ({_display_number(new['median_score'] - old['median_score'], signed=True)})" if old and new else f"{side}: 비교 자료 부족")
    else:
        lines.append("직전 4시간 예측 비교: 자료 없음 또는 버전 변경")
    lines.append("※ 매시간 새로 계산한 예측은 서로 다른 미래 시각을 봅니다. 같은 목표 시각의 수정 예측이 아닙니다.")
    lines.extend(["", "⑤ 예측 성적", "[최근 6시간 새로 채점 · 4시간 평균 오차]"])
    groups = context.get("recent_actuals")
    if groups is None:
        groups = summarize_recent_actuals(recent_records, now)
    groups = [group for group in groups if any(stats[side]["count"] for stats in group["horizons"].values()
                                              for side in ("LONG", "SHORT"))]
    for group in groups[:3]:
        lines.append(f"전략: {group['strategy_version'][:48]} / {group['forecast_version'][:32]}")
        for name in ("4h",):
            stats = group["horizons"][name]
            parts = [f"{side} {stats[side]['mae']:.2f} (채점 {stats[side]['count']}건)"
                     if stats[side]["count"] else f"{side} 채점 없음" for side in ("LONG", "SHORT")]
            lines.append(" / ".join(parts))
        latest = group["horizons"]["4h"]["latest"]
        if latest:
            lines.append(f"최근 4시간 예측 채점 (실제 {_report_time(latest['actual_timestamp']).astimezone(KST):%m-%d %H:%M} KST):")
            for side, item in latest["sides"].items():
                lines.append(f"{side}: 예상 {_display_number(item['predicted'])} / 실제 {_display_number(item['actual'])} → 오차 {_display_number(item['absolute_error'])}")
        if group["legacy_time_count"]:
            lines.append("기존 확정시각 미기록 건은 실제 시장 시각으로 집계했습니다.")
    if not groups:
        lines.append("새로 채점할 결과 없음")
    if len(groups) > 3:
        lines.append(f"그 외 {len(groups) - 3}개 버전의 상세 집계는 DB 보고 기록에 보존됩니다.")
    if "persistence_performance" in context:
        lines.extend(_format_persistence_performance(context["persistence_performance"],
                                                     (forecast or {}).get("strategy_version")))
    lines.extend(["※ 평균 오차는 낮을수록 좋습니다.",
                  "※ 유지 = 지금 점수가 4시간 뒤에도 같다고 가정한 기준입니다.",
                  "※ 미확정·자료 부족은 오답으로 계산하지 않습니다.",
                  "※ 채점 건수에는 서로 겹치는 시간대의 예측도 포함됩니다.",
                  "※ 점수 예측은 가격·매매 수익 예측이 아닙니다."])
    return "\n".join(lines)
