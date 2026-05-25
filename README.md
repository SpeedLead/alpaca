# Alpaca Paper-Trading: TSLA Trailing Stop

Automated trailing-stop + ladder-buy strategy on Tesla, running against an Alpaca paper-trading account. The monitor is a Python script triggered every 15 minutes by macOS `launchd`.

## Strategy

- **Entry:** 10-share market buy of TSLA at the prevailing price.
- **Hard floor:** stop-loss at `entry × 0.90` (−10%) — limits loss on a fast drop.
- **Trailing floor:** once price reaches `entry × 1.10` (+10%), trailing activates. Stop moves to `peak × 0.95` and **only moves UP, never down**.
- **Ladder buys** (limit, GTC):
  - `entry × 0.80` (−20%) → buy 20 shares
  - `entry × 0.70` (−30%) → buy 10 shares

After each ladder fill, the stop order is replaced to cover the new share count.

## Files

| File | Purpose |
|---|---|
| `tsla_trailing_stop.py` | The monitor. Idempotent; safe to run repeatedly. |
| `trailing_stop_state.json` | Persisted state (entry, peak, current stop, order IDs). |
| `trailing_stop.log` | App log written by the script itself. |
| `trailing_stop_monitor.sh` | Earlier bash prototype — superseded. |
| `.env` | Alpaca credentials (gitignored; mode `0600`). |
| `~/Library/LaunchAgents/com.user.tsla-trailing-stop.plist` | macOS LaunchAgent that fires the monitor every 15 min. |
| `~/Library/Logs/tsla-trailing-stop.launchd.log` | launchd stdout/stderr (separate from the app log; for crash diagnosis). |

See `CLAUDE.md` for Alpaca API endpoint / header reference.

## Running

The monitor self-checks the market clock and no-ops outside trading hours.

```bash
# manual run
python3 tsla_trailing_stop.py

# trigger the launchd job once
launchctl kickstart "gui/$(id -u)/com.user.tsla-trailing-stop"

# tail the app log
tail -f trailing_stop.log

# stop / reload the schedule (bootout unloads the agent, bootstrap reloads it)
launchctl bootout   "gui/$(id -u)/com.user.tsla-trailing-stop"
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.user.tsla-trailing-stop.plist
```

## How the schedule works

`launchd` invokes the script through `/bin/zsh -c "..."` every 900 seconds (15 min). The shell wrapper exists because macOS TCC blocks `launchd` from directly spawning processes that read `~/Desktop`; routing through the user-context shell sidesteps it. For the same reason, launchd's stdout/stderr is redirected to `~/Library/Logs/` (not TCC-protected) while the script's own logger continues writing to `trailing_stop.log` on Desktop.

Caveats:
- The job only fires while the Mac is awake. Keep it plugged in (or use `caffeinate`) during market hours.
- `StartInterval=900` does not queue missed runs — if the Mac was asleep at the fire time, that tick is skipped.
- DST transition (early November) shifts US market hours by 1 hour UTC; the script's internal `market_open()` check handles this automatically, no plist edit needed.

## Safety

- `.env` is `chmod 600`. Both the log file and state file are in `.gitignore`.
- The monitor uses `fcntl.flock(LOCK_NB)` — concurrent invocations skip with a log line rather than racing and double-placing orders.
- State writes are atomic (`tmp → os.replace`) to survive crashes mid-write.
- Protective stop is `stop_limit` with a `sell_to_close` position-intent — avoids Alpaca's wash-trade rejection when ladder BUY orders sit below.

## Project history (original prompts)

> Hey, what I want you to do is I just gave you the documentation and my keys to connect to my Alpaca trading account. I'm just testing the connection right now. Can you please buy one share of Apple? I want to see it inside my account.

> Hey, can you sell that share of Apple and then buy a share of Tesla?

> Hey, can you make sure in this folder you save these credentials so I don't have to keep giving it to you when we want to trade? We're going to be using this account, and in this folder we're going to be doing a lot of trades.

> Alright, so I want your help to actually schedule a trailing stop strategy on, let's say, Tesla. I want you to buy Tesla using a paper trading account I don't know, like 10 shares at the market price right now and set up these rules:
> - The floor: if the stock drops, let's say, by 10%, sell everything. That's my stop loss. I don't want to lose more than that on this trade.
> - The trailing floor: if the stock goes up 10% from what I paid, move my stop loss up. Maybe move it up 5% below the current price every time it climbs. Move another 5% up the floor again, so the floor only goes up, never down.
> - I want you to also ladder in: if the stock drops, let's say, by over 20~30%, buy a bunch more shares, let's say 10 more shares. If it drops by, let's say, 20%, buy 20 shares. This way I'm getting better prices on the way down instead of just losing money.
>
>   After you set this up, show me a summary of every order and right after you place it so I can confirm this looks right.

> Hey, can you set up during market hours every day that you're checking consistently when we need to move our floor up or need to make new stop losses or re-enter? Use the /schedule to make sure we have that going and set your own schedules.

> Hey, so just briefly and really quickly, can you tell me what would happen if, let's say, Tesla shoots up to $500 randomly? What would you do?

### Why local `launchd` instead of a remote routine

A remote (cloud) routine via `/schedule` was considered and tried. It was disabled in favor of local `launchd` because:

1. **Min interval is 1 hour** for remote routines — can't honor the 15-minute target.
2. **No git remote** on this repo, so a cloud sandbox can't see local files. The full script would need to be embedded in the routine prompt.
3. **Credentials would be inlined** into the routine config stored on Anthropic's servers. Paper account so low risk, but still avoidable.
4. **State file is local** and cloud sandboxes are ephemeral; the script would need a rewrite to reconstruct state from Alpaca on every run.

Local `launchd` keeps the existing script, `.env`, and state file untouched and gets a true 15-minute cadence — at the cost of needing the Mac to be awake.
