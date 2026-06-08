import argparse
import time
from datetime import datetime, timedelta, timezone

from market_state_engine.report import build_status_report, send_status_report


KST = timezone(timedelta(hours=9))


def main():
    args = parse_args()
    send_times = parse_send_times(args.send_times)
    sent_keys = set()

    while True:
        now_kst = datetime.now(KST)
        current_hhmm = now_kst.strftime("%H:%M")
        send_key = now_kst.strftime("%Y-%m-%d") + "-" + current_hhmm

        if current_hhmm in send_times and send_key not in sent_keys:
            message = build_status_report(
                state_log_path=args.state_log,
                trade_log_path=args.trade_log,
                title=args.title,
            )
            print(message)
            send_status_report(message)
            sent_keys.add(send_key)

        if args.once:
            break

        time.sleep(args.check_interval_sec)


def parse_send_times(value):
    return {part.strip() for part in value.split(",") if part.strip()}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-times", default="09:10,21:10")
    parser.add_argument("--check-interval-sec", type=int, default=30)
    parser.add_argument("--state-log", default="work/logs/market_state_log.jsonl")
    parser.add_argument("--trade-log", default="work/logs/trade_log.jsonl")
    parser.add_argument("--title", default="Market State Check")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
