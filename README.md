# USDG Yield Monitor

A dashboard of supply-side yields for **USDG (Paxos Global Dollar)** across chains (Ethereum, Solana, Robinhood Chain, Arbitrum, Ink, X Layer) and protocols (Aave V3/V4, Morpho, Maple, Kamino, Jupiter Lend, Spark, Pendle, Loopscale, and others).

**Live:** https://kubesqrt.github.io/usdg-yield-dashboard/

## How it works

- `scripts/fetch_yields.py` pulls data from:
  - [DefiLlama Yields](https://yields.llama.fi/pools): every pool whose underlying token is USDG, plus 90-day APY/TVL history per venue
  - [Morpho API](https://api.morpho.org/graphql): vault curators, fees, and liquidity, plus the Morpho Blue markets where vault deposits are lent out
- The script writes `docs/data/yields.json`. A GitHub Action (`.github/workflows/update-yields.yml`) runs it every 2 hours and commits the result.
- `docs/index.html` is a static, dependency-free dashboard served by GitHub Pages.

Pools are split into:
- **Supply / lending**: you deposit USDG alone (lending markets, curated vaults, savings, institutional credit, Pendle PT fixed rate)
- **DEX liquidity pools**: USDG paired with another token (trading fees and incentives, with impermanent-loss exposure)

## Run locally

```bash
python scripts/fetch_yields.py
python -m http.server 8765 --directory docs
```

Not investment advice.
