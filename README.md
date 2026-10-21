# Bell Alerts

The Opening Bell Desk's **Proven** alerts, sent to Telegram on every 5-minute close from 9:40 to 11:00 ET. It runs on GitHub Actions, so it doesn't need your Mac, the browser or Claude.

Research only, not investment advice. It never places, reviews or cancels orders.

## What it does each trading morning

1. **9:20 ET:** GitHub starts the job. The app waits for the first 5-minute bar to close at 9:35.
2. **9:35 screen:** this is the desk's Backtest screen.
   - From `pool.txt`, it keeps names that closed above $15, had prior-day volume over 2M and a 10-day average over 5M, closed the prior day up more than 1.5%, and opened up 2% or more.
   - It takes the top 30 by gap, then adds everything in `watchlist.txt`.
   - It sends one "Bell Alerts on" message listing the names.
3. **9:40–10:55 closes:** after each 5-minute close it runs the desk's Proven rules on the finished bars.
   - The rules: 5 of 5 confirms, A+/A grade, not extended, ATR ≥ 3% of price, 20-minute cooldown, and ADD with the first lot's stop raised.
   - Each new alert goes to Telegram with entry, stop, target, quantity for your risk, and the ADD stop move.
4. **11:00:** a summary of the morning.

`tests/parity.py` checks `bell_alerts/engine.py` against the desk's own JavaScript on the same Robinhood bars. Over 140 days, 888 alerts from each and 0 differences.

## Setup (about 10 minutes)

1. **Alpaca:** make a free account at alpaca.markets, then generate an API key (paper trading keys work for data).
2. **Telegram:**
   1. Message **@BotFather**, send `/newbot` and copy the token.
   2. Send your new bot any message.
   3. Run `TELEGRAM_BOT_TOKEN=<token> python main.py chat-id` to get your chat id.
3. **GitHub:** in the repo, go to Settings → Secrets and variables → Actions and add these secrets:
   - `ALPACA_KEY_ID`
   - `ALPACA_SECRET_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. **Optional settings:** add repository *variables* `RISK` (default 100) and `FEED` (`iex` free, or `sip` if you pay for Alpaca's full feed).
5. **Test:** go to Actions → bell-alerts → Run workflow. Outside 9:00–11:00 ET it exits at once, so it's just a check that the job starts. `python main.py test` sends a test message.

## Data notes

- **`FEED=iex` (free):** today's 5-minute bars come from the IEX exchange only. VWAP and rel vol are computed consistently from IEX, but they can differ a little from Robinhood's full-market numbers, so now and then an alert can land a bar earlier or later than the desk's Replay. Older history (clouds, ATR, prior-day high) uses the full SIP feed.
- **`FEED=sip`:** needs Alpaca's paid data plan. This is the closest match to the desk.
- **Bar alignment:** 4-hour clouds are built from 1-hour bars in 4:00 / 8:00 / 12:00 / 16:00 ET buckets. Daily ATR and the prior-day high come from regular-session bars.

## Commands

```
python main.py run                 # live morning loop (what Actions runs)
python main.py replay 2026-10-20   # print what it would have sent that day
python main.py test                # Telegram test message
python main.py chat-id             # your Telegram chat id
```

## GitHub Actions minutes

One run takes about 100 minutes, so roughly 2,200 minutes a month. A **public** repo has unlimited free minutes. A **private** repo on the free plan gets 2,000 a month, which isn't quite enough; GitHub Pro gets 3,000. GitHub also pauses scheduled workflows in a public repo after 60 days with no commits, so push any small change now and then.
