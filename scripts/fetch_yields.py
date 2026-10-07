"""Fetch supply-side USDG yields across chains/protocols and write docs/data/yields.json.

Sources:
  - DefiLlama yields API (all protocols/chains, plus per-pool APY history)
  - Morpho GraphQL API (vault curators, direct Morpho Blue markets lending USDG)

Stdlib only so it runs anywhere (GitHub Actions, locally) without installs.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "yields.json")

# USDG (Paxos Global Dollar) token addresses per chain. Ethereum and Ink share an address.
USDG_ADDRESSES = {
    "0xe343167631d89b6ffc58b88d6b7fb0228795491d",  # Ethereum, Ink
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # Robinhood Chain
    "0x004b506865409877c9fa29bfb1eba929984b9bbc",  # Arbitrum
    "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8",  # X Layer
    "2u1tszseqz3qbwf3ungpfc8tzmk2tdiwknnrmwgwjgwh",  # Solana (lowercased mint)
}

MIN_SUPPLY_TVL = 10_000
MIN_LP_TVL = 50_000
MIN_HISTORY_TVL = 25_000
HISTORY_DAYS = 90

PROJECT_NAMES = {
    "aave-v3": "Aave V3",
    "aave-v4": "Aave V4",
    "morpho-blue": "Morpho",
    "maple": "Maple",
    "jupiter-lend": "Jupiter Lend",
    "kamino-lend": "Kamino Lend",
    "kamino-liquidity": "Kamino Liquidity",
    "spark-savings": "Spark Savings",
    "tydro": "Tydro",
    "loopscale-lending": "Loopscale",
    "sentora-curator": "Sentora (Kamino vault)",
    "pendle-v2": "Pendle",
    "project-0": "Project 0",
    "arcadia-v2": "Arcadia",
    "flock-credit": "Flock Credit",
    "curve-dex": "Curve",
    "orca-dex": "Orca",
    "uniswap-v4": "Uniswap V4",
    "uniswap-v3": "Uniswap V3",
    "fluid-dex": "Fluid DEX",
    "gmx-v2-perps": "GMX V2",
    "velodrome-v3": "Velodrome",
    "raydium-amm": "Raydium",
    "ekubo": "Ekubo",
    "maverick-v2": "Maverick",
    "alandale-v3": "Alandale",
    "kyberswap-fairflow": "KyberSwap",
    "ripe-protocol": "Ripe",
    "pare": "Pare",
}

# Category + plain-English explanation of where the yield comes from.
SOURCES = {
    "aave-v3": ("Lending", "Interest paid by borrowers of USDG. Variable rate set by the pool's utilization curve; suppliers earn borrow interest minus the Aave reserve factor."),
    "aave-v4": ("Lending", "Aave V4 hub/spoke lending: base rate is borrower interest on USDG routed through the liquidity hub; the reward component is incentive emissions reported for the spoke (e.g. the Global Dollar spoke)."),
    "morpho-blue": ("Curated vault", "A curator (e.g. Steakhouse, Gauntlet) allocates deposits across isolated Morpho Blue markets where borrowers post collateral (USDe, syrupUSDG, tokenized stocks) and pay interest. Net of curator performance fee; rewards are protocol/Merkl incentives."),
    "maple": ("Institutional credit", "syrupUSDG: deposits fund Maple's over-collateralized loans to institutional borrowers (market makers, trading firms). Yield = loan interest net of fees."),
    "jupiter-lend": ("Lending", "Borrower interest from Jupiter Lend (Fluid-powered) on Solana; the Ethena market's demand comes mostly from USDe/sUSDe loopers borrowing USDG."),
    "kamino-lend": ("Lending", "Borrower interest in a specific Kamino isolated market on Solana. Rate depends on each market's collateral (SOL/BTC, OnRe, reUSD, JLP...) and utilization."),
    "spark-savings": ("Savings", "Spark savings vault: USDG deployed by Spark's allocation system, paying a rate tied to the Sky Savings Rate."),
    "tydro": ("Lending", "Aave-fork lending market on Ink; yield is interest paid by USDG borrowers."),
    "loopscale-lending": ("Lending", "Loopscale order-book lending vaults on Solana; lenders earn fixed/variable interest from matched borrowers, plus occasional incentives."),
    "sentora-curator": ("Curated vault", "Sentora-curated Kamino vault that allocates USDG across Kamino lending markets (here the Ethena market)."),
    "pendle-v2": ("Fixed rate", "Buy PT-USDG at a discount and redeem 1:1 at maturity: a fixed yield locked until the expiry date (price risk if sold early)."),
    "project-0": ("Lending", "Borrower interest from Project 0 (marginfi successor) lending pool on Solana."),
    "arcadia-v2": ("Lending", "Arcadia lending pool: USDG lent to leveraged LP/margin accounts, paying interest."),
    "flock-credit": ("Lending", "Small lending vault backed by veUP collateral; very high APY reflects thin TVL and high borrower demand - treat with caution."),
    "morpho-market": ("Direct market", "Supplying directly into a single Morpho Blue market (no curator). Yield = interest from borrowers of that one collateral type; this TVL is mostly the vaults above, so it is excluded from totals."),
}
LP_SOURCE = ("DEX LP", "Trading fees (and sometimes token incentives) from providing USDG liquidity in a pair. Exposed to the other token's price and to impermanent loss; not pure supply yield.")

CHAIN_NAMES = {"Xlayer": "X Layer"}


def get_json(url, data=None, retries=3):
    headers = {"User-Agent": "usdg-yield-dashboard/1.0", "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"retry {url}: {e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def tokens(sym):
    return [t.strip().upper() for t in (sym or "").split("-")]


def is_usdg_token(addr):
    return (addr or "").lower() in USDG_ADDRESSES


def classify(p):
    """Return 'supply', 'lp' or None for a DefiLlama pool."""
    under = p.get("underlyingTokens") or []
    meta = (p.get("poolMeta") or "")
    if p.get("exposure") == "single" and not meta.startswith("For LP"):
        if under and is_usdg_token(under[0]):
            return "supply"
        if not under and (p.get("symbol") or "").upper() == "USDG":
            return "supply"
        return None
    if any(is_usdg_token(u) for u in under) or "USDG" in tokens(p.get("symbol")):
        return "lp"
    return None


def r(x, n=4):
    return None if x is None else round(float(x), n)


def morpho_query(query):
    res = get_json("https://api.morpho.org/graphql", {"query": query})
    if "errors" in res:
        raise RuntimeError(res["errors"])
    return res["data"]


def fetch_morpho():
    addrs = json.dumps([a for a in USDG_ADDRESSES if a.startswith("0x")])
    vaults, markets = [], []
    try:
        d = morpho_query(
            "{ vaultV2s(first:100, where:{assetAddress_in:%s, totalAssetsUsd_gte:10000, listed:true}) "
            "{ items { address name symbol chain { id network } totalAssetsUsd liquidityUsd idleAssetsUsd apy netApy performanceFee "
            "rewards { asset { symbol } supplyApr } curators { items { name } } } } }" % addrs)
        vaults = d["vaultV2s"]["items"]
        d = morpho_query(
            "{ markets(first:100, where:{loanAssetAddress_in:%s, listed:true}) { items { marketId "
            "morphoBlue { chain { id network } } collateralAsset { symbol } lltv "
            "state { supplyAssetsUsd supplyApy netSupplyApy borrowApy utilization } } } }" % addrs)
        markets = d["markets"]["items"]
    except Exception as e:  # noqa: BLE001
        print(f"morpho api failed: {e}", file=sys.stderr)
    return vaults, markets


def morpho_chain(name):
    return {"Arbitrum One": "Arbitrum", "Ethereum": "Ethereum"}.get(name, name)


MORPHO_CHAIN_SLUG = {1: "ethereum", 42161: "arbitrum", 8453: "base", 4663: "robinhood"}


def main():
    pools = get_json("https://yields.llama.fi/pools")["data"]
    morpho_vaults, morpho_markets = fetch_morpho()
    by_symbol = {}
    for v in morpho_vaults:
        by_symbol[(morpho_chain(v["chain"]["network"]), (v["symbol"] or "").upper())] = v

    supply, lp = [], []
    for p in pools:
        kind = classify(p)
        if kind is None:
            continue
        tvl = p.get("tvlUsd") or 0
        if tvl < (MIN_SUPPLY_TVL if kind == "supply" else MIN_LP_TVL):
            continue
        proj = p["project"]
        chain = CHAIN_NAMES.get(p["chain"], p["chain"])
        cat, src = SOURCES.get(proj, LP_SOURCE if kind == "lp" else ("Other", "Supply yield as reported by DefiLlama."))
        if kind == "lp":
            cat, src = LP_SOURCE
        row = {
            "id": p["pool"],
            "project": proj,
            "protocol": PROJECT_NAMES.get(proj, proj.replace("-", " ").title()),
            "chain": chain,
            "symbol": p.get("symbol"),
            "meta": p.get("poolMeta"),
            "category": cat,
            "source": src,
            "tvl": round(tvl),
            "apy": r(p.get("apy")) or 0,
            "apyBase": r(p.get("apyBase")) or 0,
            "apyReward": r(p.get("apyReward")) or 0,
            "apyMean30d": r(p.get("apyMean30d")),
            "apyPct7D": r(p.get("apyPct7D")),
            "rewardTokens": p.get("rewardTokens") or [],
            "url": f"https://defillama.com/yields/pool/{p['pool']}",
            "dataSource": "DefiLlama",
        }
        if proj == "morpho-blue":
            mv = by_symbol.get((chain, (p.get("symbol") or "").upper()))
            if mv:
                row["name"] = mv["name"]
                row["curator"] = ", ".join(c["name"] for c in mv["curators"]["items"]) or None
                row["performanceFee"] = mv.get("performanceFee")
                row["liquidity"] = round(mv.get("liquidityUsd") or 0)
                row["morphoNetApy"] = r((mv.get("netApy") or 0) * 100)
                total_assets = mv.get("totalAssetsUsd") or 0
                if total_assets:
                    row["idleShare"] = r((mv.get("idleAssetsUsd") or 0) / total_assets * 100, 1)
                # Morpho's own numbers beat DefiLlama's for vault base APY (DefiLlama can report 0
                # for vaults with idle cash). Base = gross vault APY net of the performance fee.
                # Rewards: Morpho-native rewards, else DefiLlama's (e.g. off-protocol Merkl campaigns).
                morpho_base = (mv.get("apy") or 0) * (1 - (mv.get("performanceFee") or 0)) * 100
                morpho_reward = sum(rw.get("supplyApr") or 0 for rw in mv.get("rewards") or []) * 100
                row["llamaApy"] = row["apy"]
                row["apyBase"] = r(morpho_base)
                row["apyReward"] = r(morpho_reward) if morpho_reward else row["apyReward"]
                row["apy"] = r(row["apyBase"] + row["apyReward"])
                row["dataSource"] = "Morpho API + DefiLlama"
                slug = MORPHO_CHAIN_SLUG.get(mv["chain"]["id"])
                if slug:
                    row["url"] = f"https://app.morpho.org/{slug}/vault/{mv['address']}"
        (supply if kind == "supply" else lp).append(row)

    # Direct Morpho Blue markets lending USDG (source of the vault yields).
    for m in morpho_markets:
        st = m["state"] or {}
        tvl = st.get("supplyAssetsUsd") or 0
        if tvl < 50_000 or not m.get("collateralAsset"):
            continue
        apy = (st.get("netSupplyApy") or st.get("supplyApy") or 0) * 100
        base = (st.get("supplyApy") or 0) * 100
        cat, src = SOURCES["morpho-market"]
        chain = morpho_chain(m["morphoBlue"]["chain"]["network"])
        slug = MORPHO_CHAIN_SLUG.get(m["morphoBlue"]["chain"]["id"])
        supply.append({
            "id": m["marketId"],
            "project": "morpho-market",
            "protocol": "Morpho Blue market",
            "chain": chain,
            "symbol": "USDG",
            "meta": f"{m['collateralAsset']['symbol']} collateral · LLTV {int(int(m['lltv']) / 1e16)}%",
            "category": cat,
            "source": src,
            "tvl": round(tvl),
            "apy": r(apy),
            "apyBase": r(base),
            "apyReward": r(max(apy - base, 0)),
            "utilization": r((st.get("utilization") or 0) * 100, 2),
            "borrowApy": r((st.get("borrowApy") or 0) * 100),
            "url": f"https://app.morpho.org/{slug}/market/{m['marketId']}" if slug else "https://app.morpho.org",
            "dataSource": "Morpho API",
            "excludeFromTotals": True,
        })

    # APY/TVL history for supply venues from DefiLlama.
    for row in supply:
        if row["project"] == "morpho-market" or row["tvl"] < MIN_HISTORY_TVL:
            continue
        try:
            hist = get_json(f"https://yields.llama.fi/chart/{row['id']}")["data"][-HISTORY_DAYS:]
            row["history"] = [[h["timestamp"][:10], r(h.get("apy"), 3), round(h.get("tvlUsd") or 0)] for h in hist]
        except Exception as e:  # noqa: BLE001
            print(f"history failed {row['id']}: {e}", file=sys.stderr)
        time.sleep(0.3)

    supply.sort(key=lambda x: -x["tvl"])
    lp.sort(key=lambda x: -x["tvl"])
    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": ["https://yields.llama.fi", "https://api.morpho.org/graphql"],
        "supply": supply,
        "lp": lp,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(supply)} supply + {len(lp)} lp rows -> {OUT}")


if __name__ == "__main__":
    main()
