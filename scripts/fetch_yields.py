"""Fetch supply-side USDG yields across chains/protocols and write docs/data/yields.json.

Sources:
  - DefiLlama yields API (all protocols/chains, plus per-pool APY history)
  - Morpho GraphQL API (vault curators, direct Morpho Blue markets lending USDG)

Stdlib only so it runs anywhere (GitHub Actions, locally) without installs.
"""

import json
import os
import re
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
    "kamino-vault": ("Curated vault", "Kamino Earn vault: a curator (Steakhouse, Sentora, Elemental, OnRe...) allocates USDG across Kamino lending reserves on Solana. Yield = borrower interest from those reserves net of fees, plus any Kamino farm rewards. Its deposits sit inside the Kamino reserves listed separately, so it is excluded from totals."),
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

USDG_SOLANA = "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH"
USDG_ETH = "0xe343167631d89b6ffc58b88d6b7fb0228795491d"
MAPLE_SYRUP_USDG = "0x87b65c4aaffa76881f9e96f3e7ed945ddfc3cd7a"
AAVE_V4_HUBS = {"Core": "0xCca852Bc40e560adC3b1Cc58CA5b55638ce826c9",
                "Global Dollar": "0x62d63197660c080236193CA60b70E49A08E90368"}


def safe(label, fn, default=None):
    """Run a first-party source; on failure log and fall back to DefiLlama values."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"{label} failed: {e}", file=sys.stderr)
        return default


def fetch_merkl():
    """Live Merkl campaigns mentioning USDG (rewards paid on top of protocol yield)."""
    opps = []
    for page in range(5):
        batch = get_json(f"https://api.merkl.xyz/v4/opportunities?search=USDG&status=LIVE&items=100&page={page}")
        opps += batch
        if len(batch) < 100:
            break
    return [o for o in opps if o.get("action") in ("LEND", "HOLD") and o.get("status") == "LIVE"]


def merkl_reward_apr(o):
    # Realised reward APR = daily rewards / eligible TVL. Merkl's `apr` is total APR for some
    # campaign types (e.g. Aave V4 net lending), so derive it from the payout instead.
    tvl = o.get("tvl") or 0
    return (o.get("dailyRewards") or 0) * 365 / tvl * 100 if tvl else 0


def fetch_aave_v4():
    out = {}
    for name, hub in AAVE_V4_HUBS.items():
        q = ('{ hubAssets(request:{query:{hubInput:{chainId:1, address:"%s"}}, orderBy:{assetName:ASC}})'
             '{ underlying { address } summary { supplied { exchange { value } } availableLiquidity { exchange { value } } '
             'supplyApy { normalized } } } }' % hub)
        d = get_json("https://api.v4.aave.com/graphql", {"query": q})
        for a in d["data"]["hubAssets"]:
            if a["underlying"]["address"].lower() == USDG_ETH:
                s = a["summary"]
                out[name] = {"supplied": float(s["supplied"]["exchange"]["value"]),
                             "available": float(s["availableLiquidity"]["exchange"]["value"]),
                             "apy": float(s["supplyApy"]["normalized"])}
    return out


def fetch_maple():
    q = '{ poolV2(id:"%s"){ weeklyApy monthlyApy spotApy totalAssets } }' % MAPLE_SYRUP_USDG
    p = get_json("https://api.maple.finance/v2/graphql", {"query": q})["data"]["poolV2"]
    return {"apy": int(p["weeklyApy"]) / 1e28, "monthlyApy": int(p["monthlyApy"]) / 1e28,
            "spotApy": int(p["spotApy"]) / 1e28, "tvl": int(p["totalAssets"]) / 1e6}


def fetch_jupiter():
    for t in get_json("https://lite-api.jup.ag/lend/v1/earn/tokens"):
        if t.get("assetAddress") == USDG_SOLANA:
            price = float(t["asset"].get("price") or 1)
            return {"base": int(t["supplyRate"]) / 100, "reward": int(t["rewardsRate"]) / 100,
                    "tvl": int(t["totalAssets"]) / 10 ** int(t["decimals"]) * price, "address": t["address"]}
    return None


def fetch_kamino_vaults():
    rows = []
    for v in get_json("https://api.kamino.finance/kvaults/vaults"):
        st = v.get("state") or {}
        if st.get("tokenMint") != USDG_SOLANA:
            continue
        m = get_json(f"https://api.kamino.finance/kvaults/{v['address']}/metrics")
        tvl = float(m.get("tokensInvestedUsd") or 0) + float(m.get("tokensAvailableUsd") or 0)
        if tvl < MIN_SUPPLY_TVL:
            continue
        reward = (float(m.get("apyFarmRewards") or 0) + float(m.get("apyIncentives") or 0)
                  + float(m.get("apyReservesIncentives") or 0)) * 100
        rows.append({"address": v["address"], "name": st.get("name") or "Kamino vault", "tvl": tvl,
                     "base": float(m.get("apy") or 0) * 100, "reward": reward,
                     "apy7d": float(m.get("apy7d") or 0) * 100, "apy30d": float(m.get("apy30d") or 0) * 100,
                     "perfFee": (st.get("performanceFeeBps") or 0) / 1e4, "mgmtFee": (st.get("managementFeeBps") or 0) / 1e4,
                     "available": float(m.get("tokensAvailableUsd") or 0)})
        time.sleep(0.2)
    return rows


def fetch_loopscale():
    out = []
    for page in range(5):
        d = get_json("https://tars.loopscale.com/v1/markets/lending_vaults/info", {"page": page, "pageSize": 50})
        for v in d.get("lendVaults") or []:
            st = ((v.get("vaultStrategy") or {}).get("strategy")) or {}
            if st.get("principalMint") != USDG_SOLANA:
                continue
            idle = int(st.get("tokenBalance") or 0) / 1e6
            total = idle + (int(st.get("currentDeployedAmount") or 0) + int(st.get("outstandingInterestAmount") or 0)) / 1e6
            out.append({"name": (v.get("vaultMetadata") or {}).get("name") or "Loopscale vault", "idle": idle, "total": total})
        if not d.get("hasMore"):
            break
    return out


def set_apy(row, base=None, reward=None):
    if base is not None:
        row["apyBase"] = r(base)
    if reward is not None:
        row["apyReward"] = r(reward)
    row["apy"] = r((row["apyBase"] or 0) + (row["apyReward"] or 0))


def apply_first_party(supply, lend_borrow):
    """Replace DefiLlama figures with protocol-native data where the audit found gaps."""
    # 1. Lending pools: DefiLlama tvlUsd is unborrowed liquidity; show total deposits instead.
    for row in supply:
        lb = lend_borrow.get(row["id"])
        if lb and lb.get("totalSupplyUsd"):
            row["available"] = row["tvl"]
            row["tvl"] = round(lb["totalSupplyUsd"])

    # 2. Aave V4 hubs (official Aave V4 API).
    v4 = safe("aave v4 api", fetch_aave_v4, {})
    for row in supply:
        if row["project"] == "aave-v4" and row.get("meta") in v4:
            h = v4[row["meta"]]
            row["available"], row["tvl"] = round(h["available"]), round(h["supplied"])
            set_apy(row, base=h["apy"])
            row["meta"] = f"{row['meta']} hub"
            row["dataSource"] = "Aave V4 API"

    # 3. Maple syrupUSDG (Maple API; DefiLlama TVL was overstated).
    mp = safe("maple api", fetch_maple)
    for row in supply:
        if mp and row["project"] == "maple" and (row.get("meta") or "").lower() == "syrupusdg":
            row["tvl"] = round(mp["tvl"])
            set_apy(row, base=mp["apy"])
            row["apyMean30d"] = r(mp["monthlyApy"])
            row["spotApy"] = r(mp["spotApy"])
            row["dataSource"] = "Maple API"
            row["url"] = "https://app.maple.finance/earn"

    # 4. Jupiter Lend (DefiLlama pool is mis-mapped: wrong TVL and rate).
    jup = safe("jupiter api", fetch_jupiter)
    if jup:
        for row in [x for x in supply if x["project"] == "jupiter-lend"]:
            supply.remove(row)
        supply.append({
            "id": "jupiter-" + jup["address"], "project": "jupiter-lend", "protocol": "Jupiter Lend",
            "chain": "Solana", "symbol": "jlUSDG", "meta": "Earn", "category": SOURCES["jupiter-lend"][0],
            "source": SOURCES["jupiter-lend"][1], "tvl": round(jup["tvl"]), "apy": r(jup["base"] + jup["reward"]),
            "apyBase": r(jup["base"]), "apyReward": r(jup["reward"]), "apyMean30d": None, "apyPct7D": None,
            "url": "https://jup.ag/lend/earn", "dataSource": "Jupiter API"})

    # 5. Kamino vaults (Kamino API) replace DefiLlama's single curator row. They allocate into the
    #    Kamino reserves listed above, so they are excluded from totals.
    kv = safe("kamino vaults api", fetch_kamino_vaults)
    if kv is not None:
        for row in [x for x in supply if x["project"] == "sentora-curator"]:
            supply.remove(row)
        for v in kv:
            row = {
                "id": "kvault-" + v["address"], "project": "kamino-vault", "protocol": "Kamino Vault",
                "name": v["name"], "chain": "Solana", "symbol": "USDG", "meta": None,
                "category": "Curated vault", "source": SOURCES["kamino-vault"][1], "tvl": round(v["tvl"]),
                "apyMean30d": r(v["apy30d"]), "apyPct7D": None, "available": round(v["available"]),
                "performanceFee": v["perfFee"], "url": f"https://app.kamino.finance/earn/lend/{v['address']}",
                "dataSource": "Kamino API", "excludeFromTotals": True, "apyBase": 0, "apyReward": 0,
            }
            set_apy(row, base=v["base"], reward=v["reward"])
            if v["reward"]:
                row["rewardNote"] = "Kamino farm rewards, funded in tranches; can stop without notice."
            supply.append(row)

    # 6. Loopscale: DefiLlama TVL is the vault's idle balance. Match each row to its vault by that
    #    idle balance and show total deposits (idle + deployed + accrued interest). Rates verified OK.
    vaults = safe("loopscale api", fetch_loopscale, [])
    for row in supply:
        if row["project"] != "loopscale-lending":
            continue
        for v in vaults:
            if abs(v["idle"] - row["tvl"]) <= max(0.01 * row["tvl"], 50):
                row["available"], row["tvl"] = row["tvl"], round(v["total"])
                row["name"] = v["name"]
                row["dataSource"] += " + Loopscale API"
                break

    # 7. Merkl reward campaigns: authoritative reward APR, end date and eligibility.
    merkl = safe("merkl api", fetch_merkl, [])
    v4_rows = [x for x in supply if x["project"] == "aave-v4"]
    for o in merkl:
        target = None
        ident = (o.get("identifier") or "").replace("WHITELIST_CAMPAIGN", "").lower()
        explorer = (o.get("explorerAddress") or "").lower()
        if o.get("type") == "AAVE_V4_HUB_NET_LENDING" and v4_rows and o.get("nativeApr") is not None:
            # Match the campaign to its hub by native APR (Merkl does not expose the hub address).
            target = min(v4_rows, key=lambda x: abs(x["apyBase"] - o["nativeApr"]))
        else:
            for row in supply:
                if row["project"] == "morpho-market":
                    continue
                if ident and ident.startswith("0x") and ident in row["url"].lower():
                    target = row
                elif o.get("type") == "AAVE_NET_LENDING" and o.get("chainId") == 57073 and row["project"] == "tydro":
                    target = row
                if target:
                    break
        if not target:
            continue
        apr = merkl_reward_apr(o)
        end = datetime.fromtimestamp(int(o.get("latestCampaignEnd") or 0), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        name = o.get("name") or ""
        restricted = "only]" in name.lower() or "WHITELIST_CAMPAIGN" in (o.get("identifier") or "")
        tokens = sorted({t.get("symbol") for t in o.get("tokens") or [] if t.get("symbol")})
        note = f"Merkl campaign \"{name.strip()}\", ends {end}."
        if "net lending" in (o.get("description") or "").lower():
            note += " Paid on net lending: borrowing stablecoins on the same market reduces it."
        if (o.get("maxApr") or 0) > 0:
            note += f" Tops total APR up to a {o['maxApr'] * 100:.0f}% cap."
        target["rewardEnds"] = end
        if apr == 0:
            target["rewardNote"] = note + " Rewards are points with no stated value."
            continue
        if restricted:
            # Not available to a regular on-chain depositor: show it separately, not in the headline APY.
            target["restrictedReward"] = r(apr)
            target["restrictedNote"] = note
            m = re.match(r"\[(.+?)\]", name.strip())
            target["restrictedLabel"] = m.group(1) if m else "Eligible users only"
            # DefiLlama's 30d mean/history include this reward, so they overstate open-access APY.
            target["apyMean30d"] = None
            target["historyIncludesRestricted"] = True
            set_apy(target, reward=0)
        else:
            target["rewardNote"] = note
            set_apy(target, reward=apr)
        target["dataSource"] = target["dataSource"] + " + Merkl"


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

    lend_borrow = {x["pool"]: x for x in safe("lendBorrow", lambda: get_json("https://yields.llama.fi/lendBorrow"), [])}
    apply_first_party(supply, lend_borrow)

    # APY/TVL history for supply venues from DefiLlama.
    for row in supply:
        if row["project"] in ("morpho-market", "jupiter-lend", "kamino-vault") or row["tvl"] < MIN_HISTORY_TVL:
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
