"""
Theta Desk executor — runs on a subscriber's own server, logs into THEIR broker account and places
the orders the Theta Desk executor API sends for their plan. It contains no strategy: without a live
access key it does nothing.

    python -m executor check     test settings, API key and broker login (places no orders)
    python -m executor run       run for the day (paper or live, per executor.env)

Files (next to executor.env, default /opt/executor):
    executor.env        settings and credentials (chmod 600)
    executor_state.json cursor, open positions, broker stop orders
    executor.log        everything it did
"""
