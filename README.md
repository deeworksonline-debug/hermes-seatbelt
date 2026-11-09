# Seatbelt for Hermes

**Drive your agents. Limit the damage.**

Seatbelt is a Hermes plugin that puts *your* limits in front of every trading-looking tool call (perps and futures, prediction markets, NFTs, swaps). It does not pick trades and it does not make the agent smarter. It decides, in code, whether each action may happen: **allow**, **block**, **ask you**, or **clamp** (lower the leverage to your limit; recorded in the log, and in a paper-mode result the agent sees).

- **Paper mode is the default.** Trades are simulated; nothing is sent until you type `/seatbelt live I-ACCEPT-RISK`. Hard limits and `/seatbelt halt` apply in paper mode too; it just never interrupts you with approval prompts, because nothing real can happen.
- **Limits:** per-order size, auto-approve threshold, max leverage, symbol/venue allow-lists, orders per day, notional per day.
- **Kill switch:** `/seatbelt halt` (or `/halt`) blocks every trading action, including from scheduled/cron runs, and survives restarts.
- **Audit log:** `/seatbelt log` shows everything the agent tried, what was decided and why.
- **Venue-agnostic:** small YAML *adapters* map any tool (Binance, Bybit, Hyperliquid, Polymarket, OpenSea...) to a common trade. Anything trading-looking with no adapter is never silently allowed.
- **Fail-closed:** unreadable orders ask the human; a Seatbelt error blocks the trading call; no human present means no trade.

Works alongside, not instead of: `true402-swap-guard` checks whether a *token* is a scam; Seatbelt checks whether *you* want your agent doing this at all. For undo of file changes use Hermes' built-in `/rollback` checkpoints; for AI-cost budgets use `cost-sentinel`.

## Commands

| Command | What it does |
|---|---|
| `/seatbelt status` | Mode, halt state, today's usage, active limits |
| `/seatbelt log [n]` | Last n decisions |
| `/seatbelt halt` / `/halt` | Block all trading actions now |
| `/seatbelt resume` | Allow trading again (local terminal or configured admin only) |
| `/seatbelt paper` | Simulate trades (default) |
| `/seatbelt live I-ACCEPT-RISK` | Allow real orders, still within limits |
| `/seatbelt limits [key value]` | Show or change a limit, e.g. `limits max_order_usd 50` |
| `/seatbelt learn` | Trading-looking tools that have no adapter yet |
| `/seatbelt reset` | Restore default limits |

Limits are changed only through these commands, never by editing a file the agent could also edit.

## Adding a venue

Copy `venues/generic_exchange.yaml` into `<hermes home>/plugin-data/seatbelt/adapters/`, run `/seatbelt learn` to see the real tool and argument names, and edit. `venues/polymarket_pmxt.yaml` is verified against `hermes-pmxt` 0.4.0 (orders go through `pmxt_call` with a state-changing `method`).

## Honest limits

Seatbelt is a seatbelt, not a vault.

- It only sees actions that go through Hermes tools. Trades made in an exchange's own app are invisible to it.
- Plugins run in-process and are not sandboxed. Seatbelt blocks the obvious routes around itself (editing its files, `hermes plugins disable seatbelt`, order-placing code in `execute_code`/`terminal`), but this is best-effort pattern matching, and a determined agent may find another route.
- It cannot undo a trade that was sent, and it cannot tell whether a trade is a good idea.
- Order size is read from tool arguments, and the **worst case wins**: if several argument names disagree the largest size and leverage are used, a price on a market order is not trusted, and a declared notional can only raise the checked size. If size cannot be determined the order is escalated, not guessed. Prediction-market orders without a price are bounded at $1 per share.
- If a tool call carries no `tool_call_id`, Seatbelt never reuses an earlier decision; at worst an order is counted twice, which errs on the safe side.
- Loss limits are not included: Seatbelt does not know your profit and loss. Limits are on order size, count and notional.
- "Ask you" goes through Hermes' own approval gate, so it follows your Hermes approval settings: if you turn approvals off or run in a mode that auto-approves (for example `approvals.single_query_mode: approve`), Seatbelt's asks are approved automatically. Hard blocks, clamps, paper mode and `/seatbelt halt` do not depend on that setting.
- I have not tested whether the hook fires for tools delivered through MCP servers; adapters match by tool name, so check `/seatbelt learn` after connecting one.
- It does not stop prompt injection. It makes the consequences small.

**Also set exchange-side protection:** use API keys with *trade-only, no withdrawal* permission and IP allow-lists. Never put real keys in a test environment. Nothing here is financial advice.

## Development

```
python -m pytest            # 67 tests, no Hermes install needed
hermes plugins validate ./seatbelt --install-deps
```

MIT licensed.
