#!/usr/bin/env python3
"""TSLA trailing stop + ladder-buy monitor.

Strategy
--------
- Entry: 10-share market buy (already filled).
- Hard floor: -10% from entry (initial stop loss).
- Trailing: once price >= entry * 1.10, ratchet stop to 5% below the running
  high-water mark. The stop only moves UP, never down.
- Ladder buys (limit, GTC):
    * -20% from entry -> buy 20 more
    * -30% from entry -> buy 10 more

This script is idempotent: run it as often as you like during market hours.
Each run it (a) refreshes peak/stop, (b) re-places the protective sell at the
current shares-held if the ladder filled, (c) replaces a stale stop order.

Wash-trade workaround: Alpaca paper rejects a plain stop sell while opposite-
side limit BUY orders sit below. We place a stop_limit (stop trigger, limit
floor 1.2% below trigger) instead.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "trailing_stop_state.json"
LOG_FILE = ROOT / "trailing_stop.log"
LOCK_FILE = ROOT / ".tsla_trailing_stop.lock"

SYMBOL = "TSLA"
TRAIL_ACTIVATE_PCT = 0.10  # +10% from entry activates trailing
TRAIL_GAP_PCT = 0.05       # stop sits 5% below the running peak
HARD_STOP_PCT = 0.10       # initial stop is 10% below entry
STOP_LIMIT_BUFFER = 0.012  # stop_limit's limit sits 1.2% below stop_price
LADDER_RULES = [
    (0.20, 20),  # -20% from entry: buy 20
    (0.30, 10),  # -30% from entry: buy 10
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
BASE_URL = ENV["ALPACA_BASE_URL"].rstrip("/")
HEADERS = {
    "APCA-API-KEY-ID": ENV["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": ENV["ALPACA_API_SECRET"],
    "Content-Type": "application/json",
}


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode(errors="replace")
        try:
            return json.loads(body_txt)
        except Exception:
            return {"_http_error": e.code, "_body": body_txt}


@dataclass
class State:
    entry_price: float | None = None
    peak_price: float = 0.0
    current_stop: float | None = None
    trailing_active: bool = False
    stop_order_id: str | None = None
    ladder_order_ids: dict[str, str] | None = None  # "-20%" -> order_id

    @classmethod
    def load(cls) -> "State":
        if not STATE_FILE.exists():
            return cls()
        raw = json.loads(STATE_FILE.read_text())
        # Tolerate the older state-file shape from the bash script.
        return cls(
            entry_price=raw.get("entry_price"),
            peak_price=raw.get("peak_price", 0.0) or 0.0,
            current_stop=raw.get("current_stop"),
            trailing_active=bool(raw.get("trailing_active", False)),
            stop_order_id=raw.get("stop_order_id"),
            ladder_order_ids=raw.get("ladder_order_ids"),
        )

    def save(self) -> None:
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, STATE_FILE)


def market_open() -> bool:
    return bool(api("GET", "clock")["is_open"])


def get_position() -> dict[str, Any] | None:
    pos = api("GET", f"positions/{SYMBOL}")
    if isinstance(pos, dict) and pos.get("code") == 40410000:
        return None  # no position
    return pos


def get_order(order_id: str) -> dict[str, Any]:
    return api("GET", f"orders/{order_id}")


def cancel_order(order_id: str) -> None:
    api("DELETE", f"orders/{order_id}")


def place_protective_stop(qty: int, stop_price: float) -> str | None:
    """Place a stop_limit sell. Returns order id or None on failure."""
    body = {
        "symbol": SYMBOL,
        "qty": str(qty),
        "side": "sell",
        "type": "stop_limit",
        "time_in_force": "gtc",
        "stop_price": f"{stop_price:.2f}",
        "limit_price": f"{stop_price * (1 - STOP_LIMIT_BUFFER):.2f}",
        "position_intent": "sell_to_close",
    }
    resp = api("POST", "orders", body)
    if "id" in resp:
        log(
            f"  -> placed stop_limit {resp['id'][:8]}: {qty} sh, "
            f"stop ${body['stop_price']} / limit ${body['limit_price']}"
        )
        return resp["id"]
    log(f"  !! stop placement FAILED: {resp}")
    return None


def reconcile_stop(state: State, qty: int, desired_stop: float) -> None:
    """Make the resting stop order match `qty` and `desired_stop`."""
    existing = None
    if state.stop_order_id:
        existing = get_order(state.stop_order_id)
        status = (existing or {}).get("status")
        if status not in ("new", "accepted", "held", "pending_new"):
            log(f"  stop {state.stop_order_id[:8]} status={status}, replacing")
            existing = None
            state.stop_order_id = None

    needs_replace = (
        existing is None
        or int(float(existing.get("qty", 0))) != qty
        or abs(float(existing.get("stop_price", 0)) - desired_stop) > 0.01
    )
    if not needs_replace:
        return

    if state.stop_order_id:
        cancel_order(state.stop_order_id)
        log(f"  canceled stop {state.stop_order_id[:8]}")
        state.stop_order_id = None

    new_id = place_protective_stop(qty, desired_stop)
    state.stop_order_id = new_id
    state.current_stop = desired_stop


def reconcile_ladder(state: State, entry_price: float) -> None:
    """Make sure each ladder buy is resting at the right level and qty."""
    if state.ladder_order_ids is None:
        state.ladder_order_ids = {}

    open_orders = api("GET", "orders?status=open&symbols=TSLA") or []
    open_buys = {o["id"]: o for o in open_orders if o["side"] == "buy"}

    for drop_pct, qty in LADDER_RULES:
        key = f"-{int(drop_pct * 100)}%"
        limit_price = round(entry_price * (1 - drop_pct), 2)
        existing_id = state.ladder_order_ids.get(key)
        existing = open_buys.get(existing_id) if existing_id else None
        ok = (
            existing is not None
            and int(float(existing.get("qty", 0))) == qty
            and abs(float(existing.get("limit_price", 0)) - limit_price) < 0.01
        )
        if ok:
            continue
        if existing_id and existing_id in open_buys:
            cancel_order(existing_id)
            log(f"  canceled stale ladder {key} {existing_id[:8]}")
        body = {
            "symbol": SYMBOL,
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "time_in_force": "gtc",
            "limit_price": f"{limit_price:.2f}",
        }
        resp = api("POST", "orders", body)
        if "id" in resp:
            state.ladder_order_ids[key] = resp["id"]
            log(f"  -> placed ladder {key}: {qty} sh @ ${limit_price}")
        else:
            log(f"  !! ladder {key} placement FAILED: {resp}")


def run_once() -> int:
    state = State.load()

    if not market_open():
        log("Market closed; skipping monitor loop.")
        return 0

    position = get_position()
    if position is None or int(float(position.get("qty", 0))) == 0:
        log("No TSLA position. Nothing to monitor.")
        state.save()
        return 0

    qty = int(float(position["qty"]))
    avg_entry = float(position["avg_entry_price"])
    current_price = float(position["current_price"])

    if state.entry_price is None:
        state.entry_price = avg_entry
        state.peak_price = current_price
        log(f"Initialized entry_price={avg_entry} peak={current_price}")
    # If ladder fills moved the avg entry, refresh the reference.
    elif abs(avg_entry - state.entry_price) > 0.01:
        log(f"Avg entry shifted {state.entry_price} -> {avg_entry} (ladder fill)")
        state.entry_price = avg_entry

    if current_price > state.peak_price:
        state.peak_price = current_price

    activate_at = state.entry_price * (1 + TRAIL_ACTIVATE_PCT)
    if not state.trailing_active and state.peak_price >= activate_at:
        state.trailing_active = True
        log(f"TRAILING ACTIVATED: peak {state.peak_price} >= {activate_at:.2f}")

    if state.trailing_active:
        trailing_stop = round(state.peak_price * (1 - TRAIL_GAP_PCT), 2)
        desired_stop = max(trailing_stop, state.current_stop or 0)
    else:
        desired_stop = round(state.entry_price * (1 - HARD_STOP_PCT), 2)

    log(
        f"{SYMBOL} px={current_price} qty={qty} entry={state.entry_price} "
        f"peak={state.peak_price} stop->{desired_stop} trailing={state.trailing_active}"
    )

    reconcile_stop(state, qty, desired_stop)
    reconcile_ladder(state, state.entry_price)

    state.save()
    return 0


def main(argv: list[str]) -> int:
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    # Non-blocking lock — if another instance is mid-run, skip rather than race.
    with LOCK_FILE.open("w") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another instance is running; skipping.")
            return 0
        return run_once()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
