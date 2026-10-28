"""Edge math and the scorecard (Brier score, calibration, paper P&L). Pure functions - easy to test."""
from . import config as C


def fee(price):
    """Kalshi taker fee per contract (unrounded): rate * P * (1-P). Real fees round up per ORDER, so tiny orders pay more."""
    return C.FEE_RATE * price * (1 - price)


def best_edge(p, m):
    """Best side to buy at the ASK, after fees. Returns (side, edge, price, fee). p = probability of YES."""
    ya, na = m["yes_ask"], m["no_ask"]
    e_yes = p - ya - fee(ya)
    e_no = (1 - p) - na - fee(na)
    return ("yes", e_yes, ya, fee(ya)) if e_yes >= e_no else ("no", e_no, na, fee(na))


def brier(p, outcome):
    return (p - outcome) ** 2


def _first_per_market(rows):
    """First estimate per ticker, so a market that was re-estimated many times isn't over-weighted."""
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["ts"]):
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            out.append(r)
    return out


def calibration(rows, key="p_model", bins=5):
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        grp = [r for r in rows if lo <= r[key] < hi or (b == bins - 1 and r[key] == 1.0)]
        if grp:
            out.append({"bin": f"{lo:.0%}-{hi:.0%}", "n": len(grp),
                        "mean_p": sum(r[key] for r in grp) / len(grp),
                        "observed": sum(r["outcome"] for r in grp) / len(grp)})
    return out


def scorecard(settled_rows, alerted_rows):
    """settled_rows: estimates with outcome 0/1. alerted_rows: alerted estimates that have settled (pnl set)."""
    first = _first_per_market([r for r in settled_rows if r["outcome"] is not None])
    sc = {"n": len(first), "calibration": calibration(first)}
    if first:
        n = len(first)
        sc["brier_model"] = sum(brier(r["p_model"], r["outcome"]) for r in first) / n
        sc["brier_blend"] = sum(brier(r["p_adj"], r["outcome"]) for r in first) / n
        sc["brier_market"] = sum(brier(r["mid"], r["outcome"]) for r in first) / n
        sc["skill_model"] = 1 - sc["brier_model"] / sc["brier_market"] if sc["brier_market"] else None
        sc["skill_blend"] = 1 - sc["brier_blend"] / sc["brier_market"] if sc["brier_market"] else None
    pr = [r for r in alerted_rows if r["pnl"] is not None]
    sc["paper_n"] = len(pr)
    if pr:
        cost = sum(r["price"] + r["fee"] for r in pr)
        sc["paper_wins"] = sum(1 for r in pr if r["pnl"] > 0)
        sc["paper_pnl"] = sum(r["pnl"] for r in pr)
        sc["paper_avg"] = sc["paper_pnl"] / len(pr)
        sc["paper_roi"] = sc["paper_pnl"] / cost if cost else None
        for d in ("approved", "skipped"):
            g = [r for r in pr if r["decision"] == d]
            sc[f"paper_{d}"] = (len(g), sum(r["pnl"] for r in g) / len(g)) if g else None
    return sc


def format_scorecard(sc):
    if not sc["n"] and not sc["paper_n"]:
        return "No settled estimates yet. Markets must close and settle before anything can be scored."
    lines = []
    if sc["n"]:
        lines.append(f"Probability quality, first estimate per market (n={sc['n']} settled; lower Brier is better):")
        lines.append(f"  model {sc['brier_model']:.3f} · blended {sc['brier_blend']:.3f} · market {sc['brier_market']:.3f}")
        if sc.get("skill_model") is not None:
            lines.append(f"  skill vs market: model {sc['skill_model']:+.0%}, blended {sc['skill_blend']:+.0%} (positive = beats the market)")
        for b in sc["calibration"]:
            lines.append(f"  said {b['bin']:>8}: n={b['n']:<3} avg said {b['mean_p']:.0%} → happened {b['observed']:.0%}")
    if sc["paper_n"]:
        lines.append(f"Paper P&L of alerts (n={sc['paper_n']}, at the ask, after fees): wins {sc['paper_wins']}/{sc['paper_n']}, "
                     f"avg {sc['paper_avg']*100:+.1f}¢/contract, ROI {sc['paper_roi']:+.1%}")
        for d in ("approved", "skipped"):
            if sc.get(f"paper_{d}"):
                k, avg = sc[f"paper_{d}"]
                lines.append(f"  you {d}: n={k}, avg {avg*100:+.1f}¢/contract")
    if sc["n"] < 50:
        lines.append(f"⚠ n={sc['n']} is far too small to conclude anything. Expect ~50+ settled markets before trusting these numbers.")
    return "\n".join(lines)
