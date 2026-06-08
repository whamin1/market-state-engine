import argparse

from market_state_engine.report import build_status_report, send_status_report


def main():
    args = parse_args()
    message = build_status_report(
        state_log_path=args.state_log,
        trade_log_path=args.trade_log,
        title=args.title,
    )

    print(message)

    if args.telegram:
        send_status_report(message)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-log", default="work/logs/market_state_log.jsonl")
    parser.add_argument("--trade-log", default="work/logs/trade_log.jsonl")
    parser.add_argument("--title", default="Market State Check")
    parser.add_argument("--telegram", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
