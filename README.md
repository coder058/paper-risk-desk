# Paper trading desk with a kill switch

A trading bot can produce plausible signals while ignoring position limits, future-data leakage, or an operator's stop. This small desk separates those concerns: a fixed **SYNTHETIC** minute-bar tape produces two experimental signals; risk rules decide whether an order is allowed; a person must approve a fixture fill. Every decision is recorded in SQLite.

**Measured demo result:** the committed synthetic risk challenge blocks **6 of 6** constructed adverse orders. This measures those six checks, not strategy quality, real-market behavior, or profitability. [Replay the recorded demo](https://coder058.github.io/paper-risk-desk/) or inspect the holdout report and fixture checksum in [`evals/SYNTHETIC_holdout.json`](evals/SYNTHETIC_holdout.json).

## Try it

Python 3.11+:

```sh
python -m venv .venv
# Activate .venv for your shell, then:
python -m pip install -e '.[test]'
make demo
make eval
make test
```

On Windows without `make`, use `python -m paper_risk.cli demo`, `python -m paper_risk.cli eval --check`, and `python -m pytest -q`. `demo` prints the proposal ID and a command for human approval; it does **not** approve or send an order. Approving fills at the *next fixture bar open* in an offline simulation. `status`, `audit`, `reject`, `halt`, and `resume` all require the printed `--db` path. `halt` and `resume` require a reason, and HALT survives a process restart.

| Layer | Reads | Writes / decision | Boundary |
| --- | --- | --- | --- |
| Tape | Committed synthetic CSV | Closed bars | Not exchange history; weekdays are not a holiday calendar |
| Signals | Bars through current close | SMA cross or mean-reversion proposal | No future bar passed to a strategy |
| Risk | Account state, next-bar open, `config.json` | Allow/block, daily loss and peak-drawdown HALT | Rules are illustrative, **UNCALIBRATED GUESSES** |
| Human desk | Proposal + risk result | Approve/reject/halt/resume and SQLite audit | Defaults to proposed-only; no broker connection |
| Evaluation | First 15 fixture weekdays as indicator warm-up, later dates as holdout | Committed JSON regression | Auto-approval is *research simulation*, not a live workflow |

The fixture is a deterministic, SPY-shaped waveform from 5 January–6 February 2026, **not actual SPY data**. It includes weekdays that may be exchange holidays. The first 15 fixture weekdays are warm-up, not a fitted model. Two strategy parameters and all monetary/risk thresholds in [`config.json`](config.json) are **UNCALIBRATED GUESSES** for demonstration; real data, transaction costs, slippage, market calendars, and account reconciliation would be needed before interpreting strategy results. A paper result would still not establish a live edge.

`python -m paper_risk.cli paper-snapshot` optionally reads your Alpaca **paper** account, paper market clock, and latest SPY minute bar from the **IEX-only** feed. Set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in your environment; no credentials are stored or printed. The reported bar age is measured, not assumed to be real-time. [Alpaca paper account API](https://docs.alpaca.markets/us/reference/getaccount-1) · [latest stock bar API](https://docs.alpaca.markets/us/reference/stocklatestbarsingle-1). The free IEX feed is one exchange, not a consolidated US market view.

There is **no Alpaca order adapter in this release**. An earlier paper-ready path was removed because a historical synthetic reference price must not authorize even a paper broker order. Any later execution adapter must require current authorized market data, broker account/position reconciliation, an explicit human confirmation, paper-only endpoint verification, and fail-closed stale-data handling. No energy contract or live brokerage support is claimed.

The desk is deliberately separate from [Energy Monitor](https://energy-monitor-jordi.jlpmccs.chatgpt.site/): energy observations are not tradable signals here. No strategy alpha, profitability, or HFT capability is claimed.

## Verify the claim

`make eval` compares a fresh calculation with the committed holdout JSON, including fixture and config hashes. `make test` checks reproducibility, future-bar isolation, the human gate, persistent HALT, cross-session loss handling, and the six adverse cases. The test coverage is limited to these cases; **6/6 is not a general safety guarantee**.

License: MIT. Author: Jordi Lluis ([portfolio](https://coder058.github.io/profile/)).
