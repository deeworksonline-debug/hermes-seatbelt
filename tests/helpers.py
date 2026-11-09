def order(symbol="BTC", side="buy", amount=0.001, price=50000, leverage=None, exchange="bybit", **extra):
    args = {"symbol": symbol, "side": side, "amount": amount, "price": price, "exchange": exchange}
    if leverage is not None:
        args["leverage"] = leverage
    args.update(extra)
    return args
