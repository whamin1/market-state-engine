from market_state_engine import BinanceFuturesFetcher
from market_state_engine.binance_signed_client import BinanceSignedClient


def main():
    symbol = "BTCUSDT"
    client = BinanceSignedClient()
    fetcher = BinanceFuturesFetcher()
    current_price = fetcher.fetch_current_daily_candle(symbol)["close"]

    account = client.get_account()
    positions = client.get_position_risk(symbol)

    print(
        {
            "asset": "USDT",
            "total_wallet_balance": account.get("totalWalletBalance"),
            "total_unrealized_profit": account.get("totalUnrealizedProfit"),
            "available_balance": account.get("availableBalance"),
            "current_price": current_price,
            "position": positions[0] if isinstance(positions, list) and positions else positions,
        }
    )


if __name__ == "__main__":
    main()
