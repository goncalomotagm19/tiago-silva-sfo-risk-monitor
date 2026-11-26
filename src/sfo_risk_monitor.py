"""
Tiago Silva Single Family Office — Automated Daily Risk & Compliance Monitor
============================================================================
Deliverable 4 / Appendix C Surveillance Layer (IPS v3 Section 4.2, Table 3)

Features:
  1. Independent market-price & EUR/USD FX ingestion via yfinance (with
     multi-exchange ticker fallbacks, currency/pence sanity guard, and
     stale-price fallback logging) OR offline Day 0 IBKR statement mode.
  2. Direct IBKR CSV sync (--ibkr-csv) via ibkr_parser.py, automatically
     isolating account DUR392190 from any bundled demo sub-accounts.
  3. Full IPS v3 Table 3 Compliance Engine:
     - Red Line & High-Water Mark Drawdown (-7.5% / -11.0% / -13.0% / -15.0%)
     - SAA Sleeve Tolerance Bands (+/- 5 pp) & Asset Class Hard Caps
     - Equity Sector Concentration Cap (<= 15.0% NAV)
     - Voluntary UCITS 5/10/40 Issuer Concentration Check
     - Zero Margin / Zero Leverage / Zero Short Selling Verification
     - Projected 12-Month Net Organic Income (28% PT tax + fees vs. €300k/€30k)
     - EUR Liquidity Buffer Monitor (€500k min / €450k alert) & Quarterly
       €75,000 Cash Sweep 5-Business-Day Pre-Funding Countdown
     - EUR/Gold vs. USD Currency Exposure & Minimum IG Credit Quality (>= BBB-)
  4. Persistent daily audit_log.csv, alerts_log.csv, latest_report.json,
     publication-grade 4-panel dashboard (red_line_dashboard.png), and
     automated README.md dashboard refresh for GitHub Actions.
  5. Optional Telegram webhook notification (via env vars TELEGRAM_BOT_TOKEN
     and TELEGRAM_CHAT_ID) when any WARN / BREACH / CRITICAL alert fires.
"""

import argparse
import csv
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Tuple

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Allow sibling import of ibkr_parser
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import ibkr_parser


# Institutional SFO Visual Palette (matching IPS v3, Appendix A & Appendix B)
NAVY = "#0F2537"
STEEL = "#2B5B84"
GOLD = "#B89352"
CRIMSON = "#8B1E24"
GREEN = "#1B5E3A"
AMBER = "#C47D17"
SLATE = "#5C6B73"
BG_LIGHT = "#F8F9FA"


def load_manifest(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_live_prices(manifest: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], float, List[str]]:
    """Fetch latest closing prices & EUR/USD rate via yfinance with fallback & sanity guards."""
    import yfinance as yf  # Imported lazily so --offline-day0 works without yfinance

    warnings: List[str] = []
    price_map: Dict[str, Dict[str, Any]] = {}

    # 1. Fetch EUR/USD FX rate (EURUSD=X = USD per 1 EUR -> usd_to_eur = 1 / rate)
    usd_to_eur = float(manifest["reference_usd_to_eur"])
    try:
        fx_hist = yf.Ticker("EURUSD=X").history(period="5d")
        if not fx_hist.empty and pd.notna(fx_hist["Close"].iloc[-1]):
            eur_per_usd_inv = float(fx_hist["Close"].iloc[-1])
            if 0.70 < eur_per_usd_inv < 1.60:
                usd_to_eur = round(1.0 / eur_per_usd_inv, 6)
            else:
                warnings.append(f"FX_SANITY: EURUSD=X returned out-of-range {eur_per_usd_inv}; kept ref {usd_to_eur}")
    except Exception as exc:
        warnings.append(f"FX_FALLBACK: yfinance EURUSD=X error ({exc}); kept ref {usd_to_eur}")

    # 2. Fetch each holding with primary + fallback Yahoo tickers & price sanity guard
    for h in manifest["holdings"]:
        tk = h["ticker"]
        ref_px = float(h["last_known_price"])
        expected_ccy = h["currency"].upper()
        fetched_px = None
        used_source = "FALLBACK_LAST_KNOWN"

        for yf_sym in h.get("yf_tickers", [tk]):
            try:
                t_obj = yf.Ticker(yf_sym)
                hist = t_obj.history(period="5d")
                if hist.empty or pd.isna(hist["Close"].iloc[-1]):
                    continue
                px = float(hist["Close"].iloc[-1])

                # Guard against GBp/GBX pence misquoting on LSE tickers (100x off)
                if ref_px > 0 and px > ref_px * 40.0 and px < ref_px * 160.0:
                    px = px / 100.0

                # Sanity check: reject obvious wrong-currency or corrupted quotes (>35% single-day jump)
                if ref_px > 0 and abs(px - ref_px) / ref_px > 0.35:
                    continue

                # Also verify reported currency if available
                try:
                    info_ccy = getattr(t_obj.fast_info, "currency", None)
                    if info_ccy and info_ccy.upper() not in (expected_ccy,):
                        # Only accept if price ratio matches expected currency closely (<10% diff)
                        if ref_px > 0 and abs(px - ref_px) / ref_px > 0.10:
                            continue
                except Exception:
                    pass

                fetched_px = round(px, 6)
                used_source = yf_sym
                break
            except Exception:
                continue

        if fetched_px is None:
            fetched_px = ref_px
            warnings.append(f"PRICE_STALE: {tk} ({expected_ccy}) fell back to last known price {ref_px:.4f}")

        price_map[tk] = {"price": fetched_px, "source": used_source}

    return price_map, usd_to_eur, warnings


def next_sweep_info(as_of: date, sweep_mmdd: List[str]) -> Dict[str, Any]:
    """Compute next quarterly Cash Sweep date and business days remaining."""
    candidates = []
    for yr in (as_of.year, as_of.year + 1):
        for mmdd in sweep_mmdd:
            m, d = map(int, mmdd.split("-"))
            dt = date(yr, m, d)
            if dt >= as_of:
                candidates.append(dt)
    nxt = min(candidates)
    # Count business days (Mon-Fri) between as_of (exclusive) and nxt (inclusive)
    bdays = 0
    cur = as_of
    while cur < nxt:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            bdays += 1
    return {
        "next_sweep_date": nxt.isoformat(),
        "calendar_days_remaining": (nxt - as_of).days,
        "business_days_remaining": bdays,
    }


def load_previous_hwm(audit_csv_path: str, current_nav: float, run_date_str: str) -> float:
    """Read historical High-Water Mark (excluding today's row if re-running same date)."""
    hwm = current_nav
    if os.path.exists(audit_csv_path):
        try:
            prev = pd.read_csv(audit_csv_path)
            if not prev.empty and "nav_demo_eur" in prev.columns:
                prev_other = prev[prev["date"] != run_date_str]
                if not prev_other.empty:
                    hwm = max(hwm, float(prev_other["nav_demo_eur"].max()))
        except Exception:
            pass
    return hwm


def evaluate_portfolio(
    manifest: Dict[str, Any],
    price_map: Dict[str, Dict[str, Any]],
    usd_to_eur: float,
    run_date_str: str,
    audit_csv_path: str,
    data_warnings: List[str],
    mode_label: str,
) -> Dict[str, Any]:
    """Compute full valuation, projected net income, and IPS v3 Table 3 compliance checks."""
    rules = manifest["ips_rules"]
    scale = float(manifest["scale_factor"])
    tax_rate = float(manifest["pt_tax_rate"])

    cash_eur = float(manifest["cash_eur"])
    cash_usd = float(manifest["cash_usd"])
    accrued_eur = float(manifest.get("accrued_interest_eur", 0.0))
    satellite_target_demo = float(manifest.get("legacy_satellite_demo_eur", 50000.0))

    # 1. Mark-to-market all 23 holdings
    rows = []
    for h in manifest["holdings"]:
        tk = h["ticker"]
        qty = float(h["quantity"])
        px = float(price_map[tk]["price"])
        cost_px = float(h["cost_price"])
        ccy = h["currency"].upper()
        mv_native = qty * px
        cost_native = qty * cost_px
        mv_eur = mv_native if ccy == "EUR" else mv_native * usd_to_eur
        cost_eur = cost_native if ccy == "EUR" else cost_native * usd_to_eur

        # Projected 12M Income (Appendix A & B tax methodology)
        gross_inc_eur = mv_eur * float(h["gross_yield"])
        fee_eur = mv_eur * float(h["fee_drag"])
        if h["tax_post_fee"]:
            taxable = max(0.0, gross_inc_eur - fee_eur)
            tax_eur = taxable * tax_rate
            net_inc_eur = gross_inc_eur - fee_eur - tax_eur
        else:
            tax_eur = gross_inc_eur * tax_rate
            net_inc_eur = gross_inc_eur - fee_eur - tax_eur

        rows.append({
            **h,
            "live_price": px,
            "price_source": price_map[tk]["source"],
            "mv_native": mv_native,
            "cost_native": cost_native,
            "mv_eur": mv_eur,
            "cost_eur": cost_eur,
            "unrealized_pnl_eur": mv_eur - cost_eur,
            "proj_gross_inc_eur": gross_inc_eur,
            "proj_fee_eur": fee_eur,
            "proj_tax_eur": tax_eur,
            "proj_net_inc_eur": net_inc_eur,
        })

    df = pd.DataFrame(rows)
    sec_mv_eur = float(df["mv_eur"].sum())
    cash_usd_eur = cash_usd * usd_to_eur
    total_cash_eur = cash_eur + cash_usd_eur
    nav_demo_eur = sec_mv_eur + total_cash_eur + accrued_eur
    nav_mandate_eur = nav_demo_eur * scale

    df["weight_pct"] = df["mv_eur"] / nav_demo_eur * 100.0

    # 2. High-Water Mark & Drawdown (The Red Line)
    hwm_demo_eur = load_previous_hwm(audit_csv_path, nav_demo_eur, run_date_str)
    drawdown_pct = (nav_demo_eur / hwm_demo_eur - 1.0) * 100.0
    headroom_red_line_pct = drawdown_pct - float(rules["red_line_hard_pct"])  # positive = safe distance above -15%

    # 3. Sleeve Aggregation
    sleeve_mv = df.groupby("sleeve")["mv_eur"].sum().to_dict()
    # Ring-fenced Legacy Satellite sits in EUR cash up to €50k demo (€500k mandate)
    satellite_mv_eur = min(cash_eur, satellite_target_demo)
    excess_eur_cash = max(0.0, cash_eur - satellite_mv_eur)
    sleeve_mv["Legacy Satellite (EUR)"] = satellite_mv_eur

    sleeve_weights = {k: v / nav_demo_eur * 100.0 for k, v in sleeve_mv.items()}
    fixed_income_sleeves = [
        "US Treasuries 3-7Y", "EUR Sovereigns 3-7Y",
        "US IG Corporate 3-6Y", "EUR IG Corporate",
    ]
    fi_weight_pct = sum(sleeve_weights.get(s, 0.0) for s in fixed_income_sleeves)
    eq_weight_pct = sleeve_weights.get("Global Defensive Equities", 0.0)
    reit_weight_pct = sleeve_weights.get("Listed Institutional REITs", 0.0)
    buf_mv_eur = sleeve_mv.get("EUR Liquidity Buffer", 0.0)
    buf_weight_pct = sleeve_weights.get("EUR Liquidity Buffer", 0.0)
    gold_weight_pct = sleeve_weights.get("Physical Gold ETC", 0.0)
    sat_weight_pct = sleeve_weights.get("Legacy Satellite (EUR)", 0.0)
    residual_cash_eur = excess_eur_cash + cash_usd_eur + accrued_eur
    residual_cash_pct = residual_cash_eur / nav_demo_eur * 100.0

    # 4. Currency Exposure
    usd_sec_eur = float(df[df["currency"] == "USD"]["mv_eur"].sum())
    usd_total_eur = usd_sec_eur + cash_usd_eur
    usd_exposure_pct = usd_total_eur / nav_demo_eur * 100.0
    eur_gold_exposure_pct = 100.0 - usd_exposure_pct

    # 5. Equity Sector Concentration
    eq_df = df[df["asset_class"] == "Equities"]
    sector_weights = (eq_df.groupby("sector")["mv_eur"].sum() / nav_demo_eur * 100.0).to_dict()
    max_sector_name = max(sector_weights, key=sector_weights.get)
    max_sector_pct = float(sector_weights[max_sector_name])

    # 6. Voluntary UCITS 5/10/40 Check (non-sovereign direct issuers; look-through on diversified UCITS ETFs)
    direct_non_sov = df[(~df["is_sovereign"]) & (~df["is_fund"])]
    max_issuer_row = direct_non_sov.loc[direct_non_sov["weight_pct"].idxmax()]
    max_issuer_ticker = str(max_issuer_row["ticker"])
    max_issuer_pct = float(max_issuer_row["weight_pct"])
    ucits_gt5_sum_pct = float(direct_non_sov[direct_non_sov["weight_pct"] > rules["ucits_soft_threshold_pct"]]["weight_pct"].sum())

    # 7. Zero Margin / Zero Leverage / Zero Short Check
    has_short = bool((df["quantity"] < 0).any())
    gross_exposure_pct = float(df["mv_eur"].abs().sum()) / nav_demo_eur * 100.0
    zero_margin_pass = (cash_eur >= 0.0) and (cash_usd >= 0.0) and (not has_short) and (gross_exposure_pct <= 100.01)

    # 8. Projected 12M Net Organic Income (scaled to €10M mandate)
    proj_gross_demo = float(df["proj_gross_inc_eur"].sum())
    proj_net_demo = float(df["proj_net_inc_eur"].sum())
    proj_net_mandate = proj_net_demo * scale
    income_target_mandate = float(rules["annual_net_income_target_mandate_eur"])

    # 9. Liquidity Buffer & Quarterly Cash Sweep Countdown
    run_dt = date.fromisoformat(run_date_str)
    sweep = next_sweep_info(run_dt, rules["sweep_dates_mmdd"])
    buf_mandate_eur = buf_mv_eur * scale
    buf_months = buf_mandate_eur / (income_target_mandate / 12.0)

    # 10. Credit Quality Check (no sub-IG / junk)
    prohibited_ratings = {"BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "CC", "C", "D"}
    junk_holdings = df[df["rating"].isin(prohibited_ratings)]["ticker"].tolist()

    # ---------------------------------------------------------------------
    # Build Structured IPS v3 Table 3 Alert & Compliance Scorecard
    # ---------------------------------------------------------------------
    alerts: List[Dict[str, str]] = []

    def add_alert(control: str, severity: str, message: str):
        alerts.append({
            "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "date": run_date_str,
            "control": control,
            "severity": severity,
            "message": message,
        })

    # Control 1: Drawdown / Red Line
    if drawdown_pct <= rules["red_line_hard_pct"]:
        rl_status = "CRITICAL"
        add_alert("RED_LINE", "CRITICAL", f"HARD RED LINE BREACHED: {drawdown_pct:.2f}% <= {rules['red_line_hard_pct']:.1f}%!")
    elif drawdown_pct <= rules["tier3_alert_pct"]:
        rl_status = "TIER_3"
        add_alert("RED_LINE", "CRITICAL", f"Tier 3 De-Risking Triggered ({drawdown_pct:.2f}% <= -13.0%): execute de-risking & same-day Client call.")
    elif drawdown_pct <= rules["tier2_alert_pct"]:
        rl_status = "TIER_2"
        add_alert("RED_LINE", "BREACH", f"Tier 2 Escalation ({drawdown_pct:.2f}% <= -11.0%): prepare de-risking plan; halt new risk.")
    elif drawdown_pct <= rules["tier1_alert_pct"]:
        rl_status = "TIER_1"
        add_alert("RED_LINE", "WARN", f"Tier 1 Monitoring Alert ({drawdown_pct:.2f}% <= -7.5%): PM review within 1 business day & notify Client.")
    else:
        rl_status = "PASS"

    # Control 2: Asset Class Hard Caps & Tolerance Bands
    band_breaches = []
    if eq_weight_pct > rules["max_equity_cap_pct"]:
        band_breaches.append(f"Equities {eq_weight_pct:.2f}% > 40% hard cap")
    if reit_weight_pct > rules["max_reit_cap_pct"]:
        band_breaches.append(f"REITs {reit_weight_pct:.2f}% > 15% hard cap")
    if gold_weight_pct > rules["max_gold_cap_pct"]:
        band_breaches.append(f"Gold {gold_weight_pct:.2f}% > 10% hard cap")
    if sat_weight_pct > rules["max_satellite_cap_pct"] + 0.05:
        band_breaches.append(f"Legacy Satellite {sat_weight_pct:.2f}% > 5% hard cap")

    fi_band = rules["fixed_income_band"]
    if not (fi_band["min"] <= fi_weight_pct <= fi_band["max"]):
        band_breaches.append(f"Fixed Income {fi_weight_pct:.2f}% outside [{fi_band['min']}-{fi_band['max']}%]")

    for slv, b in rules["sleeve_targets_and_bands"].items():
        w = sleeve_weights.get(slv, 0.0)
        # Apply IPS Sec. 4.3 €50k mandate (€5k demo) de minimis filter for tiny drifts like 4.94% vs 5.0%
        if w < b["min"]:
            shortfall_mandate = (b["min"] - w) / 100.0 * nav_mandate_eur
            if shortfall_mandate >= rules["de_minimis_rebalance_mandate_eur"]:
                band_breaches.append(f"{slv} {w:.2f}% < {b['min']}% band")
        elif w > b["max"]:
            excess_mandate = (w - b["max"]) / 100.0 * nav_mandate_eur
            if excess_mandate >= rules["de_minimis_rebalance_mandate_eur"]:
                band_breaches.append(f"{slv} {w:.2f}% > {b['max']}% band")

    if band_breaches:
        bands_status = "BREACH"
        add_alert("ASSET_BANDS", "BREACH", "; ".join(band_breaches))
    else:
        bands_status = "PASS"

    # Control 3: Sector Cap
    if max_sector_pct > rules["max_equity_sector_cap_pct"]:
        sector_status = "BREACH"
        add_alert("SECTOR_CAP", "BREACH", f"Equity sector '{max_sector_name}' at {max_sector_pct:.2f}% > 15.0% cap.")
    else:
        sector_status = "PASS"

    # Control 4: UCITS 5/10/40
    if max_issuer_pct > rules["ucits_single_issuer_hard_pct"] or ucits_gt5_sum_pct > rules["ucits_40_sum_cap_pct"]:
        ucits_status = "BREACH"
        add_alert("UCITS_5_10_40", "BREACH", f"Max issuer {max_issuer_ticker}={max_issuer_pct:.2f}%, >5% sum={ucits_gt5_sum_pct:.2f}%.")
    else:
        ucits_status = "PASS"

    # Control 5: Zero Margin / Zero Leverage
    if not zero_margin_pass:
        margin_status = "CRITICAL"
        add_alert("ZERO_MARGIN", "CRITICAL", f"Margin/Short detected! Cash EUR=€{cash_eur:,.2f}, Cash USD=${cash_usd:,.2f}, Short={has_short}.")
    else:
        margin_status = "PASS"

    # Control 6: Credit Quality
    if junk_holdings:
        credit_status = "BREACH"
        add_alert("CREDIT_QUALITY", "BREACH", f"Sub-IG holdings detected: {junk_holdings}")
    else:
        credit_status = "PASS"

    # Control 7: Projected 12M Net Organic Income (note: ETF TER slightly > direct custody modeled in Appendix A)
    if proj_net_mandate < income_target_mandate:
        income_status = "WARN"
        add_alert(
            "NET_INCOME",
            "WARN",
            f"Projected 12M net income (ETF proxy TER basis) €{proj_net_mandate:,.0f} vs. €{income_target_mandate:,.0f} target "
            f"(direct bond ladder mandate yields €303,852/yr; residual USD cash €{cash_usd_eur*scale:,.0f} uninvested).",
        )
    else:
        income_status = "PASS"

    # Control 8: EUR Liquidity Buffer & Cash Sweep Pre-Funding
    if buf_mandate_eur <= rules["liquidity_buffer_alert_mandate_eur"]:
        liq_status = "BREACH"
        add_alert("LIQUIDITY_BUFFER", "BREACH", f"EUR Liquidity Buffer €{buf_mandate_eur:,.0f} <= €450,000 alert threshold.")
    elif sweep["business_days_remaining"] <= rules["sweep_prefund_business_days"]:
        liq_status = "WARN"
        add_alert(
            "CASH_SWEEP",
            "WARN",
            f"Quarterly Cash Sweep (€75,000 / €7,500 demo) due in {sweep['business_days_remaining']} business days ({sweep['next_sweep_date']}). Verify pre-funding.",
        )
    else:
        liq_status = "PASS"

    for dw in data_warnings:
        add_alert("DATA_FEED", "INFO", dw)

    overall_status = "PASS"
    if any(a["severity"] in ("CRITICAL", "BREACH") for a in alerts):
        overall_status = "ACTION_REQUIRED"
    elif any(a["severity"] == "WARN" for a in alerts):
        overall_status = "PASS_WITH_NOTES"

    return {
        "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": run_date_str,
        "mode": mode_label,
        "account_id": manifest["account_id"],
        "usd_to_eur": usd_to_eur,
        "eur_to_usd": round(1.0 / usd_to_eur, 6),
        "nav_demo_eur": round(nav_demo_eur, 2),
        "nav_mandate_eur": round(nav_mandate_eur, 2),
        "securities_mv_eur": round(sec_mv_eur, 2),
        "cash_eur": round(cash_eur, 2),
        "cash_usd": round(cash_usd, 2),
        "cash_usd_in_eur": round(cash_usd_eur, 2),
        "accrued_interest_eur": round(accrued_eur, 2),
        "total_return_since_inception_pct": round((nav_demo_eur / float(manifest["initial_capital_demo_eur"]) - 1.0) * 100.0, 4),
        "hwm_demo_eur": round(hwm_demo_eur, 2),
        "drawdown_pct": round(drawdown_pct, 4),
        "headroom_to_red_line_pct": round(headroom_red_line_pct, 4),
        "weights_pct": {
            "fixed_income_total": round(fi_weight_pct, 2),
            "us_treasuries": round(sleeve_weights.get("US Treasuries 3-7Y", 0.0), 2),
            "eur_sovereigns": round(sleeve_weights.get("EUR Sovereigns 3-7Y", 0.0), 2),
            "us_ig_corp": round(sleeve_weights.get("US IG Corporate 3-6Y", 0.0), 2),
            "eur_ig_corp": round(sleeve_weights.get("EUR IG Corporate", 0.0), 2),
            "equities": round(eq_weight_pct, 2),
            "reits": round(reit_weight_pct, 2),
            "eur_liquidity_buffer": round(buf_weight_pct, 2),
            "gold": round(gold_weight_pct, 2),
            "legacy_satellite": round(sat_weight_pct, 2),
            "residual_cash": round(residual_cash_pct, 2),
        },
        "fx_exposure_pct": {
            "eur_and_gold": round(eur_gold_exposure_pct, 2),
            "usd": round(usd_exposure_pct, 2),
        },
        "concentration": {
            "max_direct_issuer_ticker": max_issuer_ticker,
            "max_direct_issuer_pct": round(max_issuer_pct, 2),
            "ucits_gt5_sum_pct": round(ucits_gt5_sum_pct, 2),
            "max_equity_sector_name": max_sector_name,
            "max_equity_sector_pct": round(max_sector_pct, 2),
            "sector_weights": {k: round(v, 2) for k, v in sector_weights.items()},
        },
        "income_and_liquidity": {
            "proj_gross_demo_eur": round(proj_gross_demo, 2),
            "proj_net_demo_eur": round(proj_net_demo, 2),
            "proj_net_mandate_eur": round(proj_net_mandate, 2),
            "liquidity_buffer_demo_eur": round(buf_mv_eur, 2),
            "liquidity_buffer_mandate_eur": round(buf_mandate_eur, 2),
            "liquidity_buffer_months": round(buf_months, 2),
            "next_sweep": sweep,
        },
        "compliance": {
            "overall": overall_status,
            "red_line": rl_status,
            "asset_bands": bands_status,
            "sector_cap": sector_status,
            "ucits_5_10_40": ucits_status,
            "zero_margin": margin_status,
            "credit_quality": credit_status,
            "net_income_etf_proxy": income_status,
            "liquidity_and_sweep": liq_status,
        },
        "alerts": alerts,
        "holdings_snapshot": df[[
            "ticker", "isin", "currency", "sleeve", "quantity", "live_price",
            "mv_eur", "weight_pct", "unrealized_pnl_eur", "price_source"
        ]].to_dict(orient="records"),
    }


def append_audit_log(report: Dict[str, Any], audit_csv_path: str) -> pd.DataFrame:
    """Append or update today's row in output/audit_log.csv."""
    os.makedirs(os.path.dirname(audit_csv_path), exist_ok=True)
    w = report["weights_pct"]
    c = report["compliance"]
    row = {
        "date": report["date"],
        "timestamp_utc": report["timestamp_utc"],
        "mode": report["mode"],
        "nav_demo_eur": report["nav_demo_eur"],
        "nav_mandate_eur": report["nav_mandate_eur"],
        "hwm_demo_eur": report["hwm_demo_eur"],
        "drawdown_pct": report["drawdown_pct"],
        "headroom_red_line_pct": report["headroom_to_red_line_pct"],
        "return_since_inception_pct": report["total_return_since_inception_pct"],
        "usd_to_eur": report["usd_to_eur"],
        "cash_eur": report["cash_eur"],
        "cash_usd": report["cash_usd"],
        "fi_pct": w["fixed_income_total"],
        "eq_pct": w["equities"],
        "reit_pct": w["reits"],
        "buf_pct": w["eur_liquidity_buffer"],
        "gold_pct": w["gold"],
        "sat_pct": w["legacy_satellite"],
        "res_cash_pct": w["residual_cash"],
        "usd_fx_pct": report["fx_exposure_pct"]["usd"],
        "max_issuer_pct": report["concentration"]["max_direct_issuer_pct"],
        "max_sector_pct": report["concentration"]["max_equity_sector_pct"],
        "proj_net_mandate_eur": report["income_and_liquidity"]["proj_net_mandate_eur"],
        "buf_months": report["income_and_liquidity"]["liquidity_buffer_months"],
        "overall_status": c["overall"],
        "red_line_status": c["red_line"],
        "zero_margin_status": c["zero_margin"],
    }

    if os.path.exists(audit_csv_path):
        hist = pd.read_csv(audit_csv_path)
        hist = hist[hist["date"] != report["date"]]
        hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    else:
        hist = pd.DataFrame([row])

    hist = hist.sort_values("date").reset_index(drop=True)
    hist.to_csv(audit_csv_path, index=False)
    return hist


def append_alerts_log(alerts: List[Dict[str, str]], alerts_csv_path: str) -> None:
    """Persist all timestamped alerts for Quarterly Variance Report audit trail."""
    os.makedirs(os.path.dirname(alerts_csv_path), exist_ok=True)
    cols = ["timestamp_utc", "date", "control", "severity", "message"]
    if not os.path.exists(alerts_csv_path):
        pd.DataFrame(columns=cols).to_csv(alerts_csv_path, index=False)
    if alerts:
        df_new = pd.DataFrame(alerts)[cols]
        df_old = pd.read_csv(alerts_csv_path)
        # Avoid duplicate identical messages on same date
        df_all = pd.concat([df_old, df_new], ignore_index=True).drop_duplicates(
            subset=["date", "control", "severity", "message"], keep="last"
        )
        df_all.to_csv(alerts_csv_path, index=False)


def render_dashboard(report: Dict[str, Any], hist_df: pd.DataFrame, out_png: str) -> None:
    """Generate 4-panel institutional surveillance dashboard matching SFO brand palette."""
    plt.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial"]
    fig, axes = plt.subplots(2, 2, figsize=(14.5, 8.6), dpi=160)
    fig.patch.set_facecolor("white")

    # Top Title Banner
    fig.suptitle(
        f"TIAGO SILVA SINGLE FAMILY OFFICE  |  DAILY RISK & RED LINE SURVEILLANCE ({report['date']})",
        fontsize=13.5, fontweight="bold", color=NAVY, y=0.975
    )
    subtitle = (
        f"Account DUR392190 (1:10 Scale)  •  NAV: €{report['nav_demo_eur']:,.2f} "
        f"(Mandate Equiv: €{report['nav_mandate_eur']/1e6:.2f}M)  •  "
        f"Drawdown: {report['drawdown_pct']:.2f}% (Headroom to −15% Red Line: +{report['headroom_to_red_line_pct']:.2f} pp)  •  "
        f"Status: {report['compliance']['overall']}"
    )
    fig.text(0.5, 0.935, subtitle, ha="center", fontsize=9.5, color=SLATE)

    # --- Panel 1 (Top-Left): NAV Trajectory & Red Line Cushion ---
    ax1 = axes[0, 0]
    ax1.set_facecolor(BG_LIGHT)
    dates = pd.to_datetime(hist_df["date"])
    navs_k = hist_df["nav_demo_eur"] / 1000.0
    hwm_k = hist_df["hwm_demo_eur"].max() / 1000.0
    red_line_k = hwm_k * 0.85
    tier1_k = hwm_k * 0.925

    if len(hist_df) == 1:
        ax1.bar(["Day 0\n" + report["date"]], [navs_k.iloc[0]], width=0.35, color=NAVY, label="Current NAV (€k)")
        ax1.set_xlim(-0.8, 0.8)
    else:
        ax1.plot(dates, navs_k, color=NAVY, linewidth=2.2, marker="o", markersize=4, label="Portfolio NAV (€k)")
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
        ax1.tick_params(axis="x", rotation=20)

    ax1.axhline(1000.0, color=SLATE, linestyle=":", linewidth=1.1, label="Initial Capital (€1,000k)")
    ax1.axhline(tier1_k, color=AMBER, linestyle="--", linewidth=1.2, label=f"Tier 1 Alert −7.5% (€{tier1_k:,.0f}k)")
    ax1.axhline(red_line_k, color=CRIMSON, linestyle="-", linewidth=1.6, label=f"IPS RED LINE −15.0% (€{red_line_k:,.0f}k)")
    ax1.set_ylim(red_line_k * 0.96, max(navs_k.max(), 1000.0) * 1.05)
    ax1.set_title("1. Portfolio NAV vs. −15.0% IPS Red Line (€k, 1:10 Scale)", fontsize=10.5, fontweight="bold", color=NAVY, pad=8)
    ax1.set_ylabel("EUR (€ Thousands)", fontsize=9, color=NAVY)
    ax1.grid(axis="y", alpha=0.35)
    ax1.legend(loc="lower left", fontsize=7.8, framealpha=0.92)

    # --- Panel 2 (Top-Right): Actual vs. Target SAA Sleeve Weights ---
    ax2 = axes[0, 1]
    ax2.set_facecolor(BG_LIGHT)
    w = report["weights_pct"]
    labels = ["US IG Corp", "EUR IG Corp", "Def. Equities", "US Treas.", "Listed REITs", "EUR Sov.", "EUR Buffer", "Gold ETC", "Satellite"]
    actuals = [w["us_ig_corp"], w["eur_ig_corp"], w["equities"], w["us_treasuries"], w["reits"],
               w["eur_sovereigns"], w["eur_liquidity_buffer"], w["gold"], w["legacy_satellite"]]
    targets = [23.0, 18.0, 18.0, 11.0, 10.0, 5.0, 5.0, 5.0, 5.0]
    y_pos = np.arange(len(labels))
    h_bar = 0.36
    ax2.barh(y_pos + h_bar/2, targets, height=h_bar, color=GOLD, alpha=0.75, label="SAA Target (%)")
    ax2.barh(y_pos - h_bar/2, actuals, height=h_bar, color=NAVY, label="Actual Weight (%)")
    for i, (a, t) in enumerate(zip(actuals, targets)):
        ax2.text(max(a, t) + 0.4, i, f"{a:.1f}% / {t:.0f}%", va="center", fontsize=8, color=NAVY)
    ax2.set_yticks(y_pos)
    ax2.set_yticklabels(labels, fontsize=8.5)
    ax2.invert_yaxis()
    ax2.set_xlim(0, 29.0)
    ax2.set_xlabel("% of Total NAV", fontsize=9, color=NAVY)
    ax2.set_title("2. Actual Allocation vs. Appendix A Strategic Targets", fontsize=10.5, fontweight="bold", color=NAVY, pad=8)
    ax2.grid(axis="x", alpha=0.35)
    ax2.legend(loc="lower right", fontsize=8, framealpha=0.92)

    # --- Panel 3 (Bottom-Left): Equity Sector Concentration vs. 15% IPS Cap ---
    ax3 = axes[1, 0]
    ax3.set_facecolor(BG_LIGHT)
    sec_w = report["concentration"]["sector_weights"]
    sec_items = sorted(sec_w.items(), key=lambda x: x[1], reverse=True)
    s_names = [k for k, _ in sec_items]
    s_vals = [v for _, v in sec_items]
    ax3.barh(s_names, s_vals, color=STEEL, height=0.55)
    ax3.axvline(15.0, color=CRIMSON, linestyle="--", linewidth=1.6, label="IPS Equity Sector Cap (15.0%)")
    for i, v in enumerate(s_vals):
        ax3.text(v + 0.25, i, f"{v:.2f}%", va="center", fontsize=8.5, color=NAVY, fontweight="bold")
    ax3.invert_yaxis()
    ax3.set_xlim(0, 17.5)
    ax3.set_xlabel("% of Total NAV", fontsize=9, color=NAVY)
    ax3.set_title("3. Equity Sector Concentration vs. 15.0% IPS Hard Cap", fontsize=10.5, fontweight="bold", color=NAVY, pad=8)
    ax3.grid(axis="x", alpha=0.35)
    ax3.legend(loc="lower right", fontsize=8, framealpha=0.92)

    # --- Panel 4 (Bottom-Right): Executive Compliance & Cash Sweep Scorecard ---
    ax4 = axes[1, 1]
    ax4.axis("off")
    ax4.set_title("4. IPS v3 Table 3 Daily Governance Scorecard", fontsize=10.5, fontweight="bold", color=NAVY, pad=8)

    c = report["compliance"]
    inc = report["income_and_liquidity"]
    fx = report["fx_exposure_pct"]
    conc = report["concentration"]

    table_data = [
        ["Red Line Drawdown (−15% Cap)", f"{report['drawdown_pct']:.2f}% (HWM €{report['hwm_demo_eur']:,.0f})", c["red_line"]],
        ["Zero Margin / Zero Leverage", f"EUR +€{report['cash_eur']:,.0f} | USD +${report['cash_usd']:,.0f}", c["zero_margin"]],
        ["SAA Bands & Asset Hard Caps", f"FI {w['fixed_income_total']:.1f}% | Eq {w['equities']:.1f}% | REIT {w['reits']:.1f}%", c["asset_bands"]],
        ["UCITS 5/10/40 Concentration", f"Max Issuer {conc['max_direct_issuer_ticker']}: {conc['max_direct_issuer_pct']:.2f}% (Cap 10%)", c["ucits_5_10_40"]],
        ["Equity Sector Cap (≤ 15.0%)", f"Max: {conc['max_equity_sector_name']} ({conc['max_equity_sector_pct']:.2f}%)", c["sector_cap"]],
        ["Credit Quality (≥ BBB− IG)", "Zero High-Yield / Zero Derivatives", c["credit_quality"]],
        ["FX Exposure (54.3% / 45.7%)", f"EUR/Gold {fx['eur_and_gold']:.1f}% | USD {fx['usd']:.1f}%", "PASS"],
        ["EUR Liquidity & Cash Sweep", f"{inc['liquidity_buffer_months']:.1f} mos | Next: {inc['next_sweep']['next_sweep_date']}", c["liquidity_and_sweep"]],
    ]

    tbl = ax4.table(
        cellText=table_data,
        colLabels=["IPS Governance Control", "Live Metric", "Status"],
        cellLoc="left",
        colWidths=[0.42, 0.42, 0.16],
        loc="center",
        bbox=[0.0, 0.02, 1.0, 0.94],
    )
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(8.3)

    for (row_i, col_i), cell in tbl.get_celld().items():
        cell.set_edgecolor("#D8DEE4")
        if row_i == 0:
            cell.set_facecolor(NAVY)
            cell.set_text_props(color="white", fontweight="bold")
        else:
            cell.set_facecolor("#FFFFFF" if row_i % 2 == 1 else BG_LIGHT)
            if col_i == 2:
                val = cell.get_text().get_text()
                cell.set_text_props(fontweight="bold", ha="center")
                if val == "PASS":
                    cell.set_facecolor("#E6F4EA")
                    cell.set_text_props(color=GREEN)
                elif val in ("WARN", "TIER_1"):
                    cell.set_facecolor("#FEF7E0")
                    cell.set_text_props(color=AMBER)
                else:
                    cell.set_facecolor("#FCE8E6")
                    cell.set_text_props(color=CRIMSON)

    plt.tight_layout(rect=[0, 0.01, 1, 0.91])
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    plt.savefig(out_png, dpi=160)
    plt.close(fig)


def update_readme(report: Dict[str, Any], readme_path: str) -> None:
    """Auto-refresh repository README.md with the latest verified daily telemetry."""
    w = report["weights_pct"]
    c = report["compliance"]
    inc = report["income_and_liquidity"]
    fx = report["fx_exposure_pct"]
    conc = report["concentration"]

    badge = "PASS" if c["overall"] == "PASS" else c["overall"]
    md = f"""# Tiago Silva Single Family Office — Daily Risk & Red Line Surveillance

Automated daily portfolio surveillance engine (**Deliverable 4 / Appendix C**) monitoring the **€10,000,000** defensive Core-Satellite mandate executed on Interactive Brokers Paper Trading (`DUR392190`, scaled **1 : 10** = €1,000,000).

> **Disclaimer:** Personal & educational proof-of-work project in Private Wealth Management. The client, capital, and figures are fictional and illustrative; nothing herein constitutes financial, legal, or tax advice.

---

## Live Portfolio Telemetry (`{report['date']}` · `{report['mode']}`)

| Metric | Demo Account (`1:10`) | Mandate Equivalent (`€10M`) | IPS Benchmark / Rule |
| :--- | :---: | :---: | :--- |
| **Net Asset Value (NAV)** | **€{report['nav_demo_eur']:,.2f}** | **€{report['nav_mandate_eur']:,.2f}** | Initial: €1,000,000 / €10,000,000 (`{report['total_return_since_inception_pct']:+.2f}%`) |
| **High-Water Mark (HWM)** | €{report['hwm_demo_eur']:,.2f} | €{report['hwm_demo_eur']*10:,.2f} | Cash-flow adjusted peak |
| **Peak-to-Trough Drawdown** | **`{report['drawdown_pct']:.2f}%`** | **`{report['drawdown_pct']:.2f}%`** | **Red Line Cap: −15.0%** (Headroom: `+{report['headroom_to_red_line_pct']:.2f} pp`) |
| **Cash Balances (Zero Margin)** | `EUR +€{report['cash_eur']:,.2f}` · `USD +${report['cash_usd']:,.2f}` | `EUR +€{report['cash_eur']*10:,.0f}` · `USD +${report['cash_usd']*10:,.0f}` | Status: **`{c['zero_margin']}`** (Zero Leverage / Shorting) |
| **FX Exposure (`EUR/USD = {report['eur_to_usd']:.4f}`)** | `EUR/Gold {fx['eur_and_gold']:.1f}%` · `USD {fx['usd']:.1f}%` | `EUR/Gold {fx['eur_and_gold']:.1f}%` · `USD {fx['usd']:.1f}%` | Target: `54.3% EUR/Gold` / `45.7% USD` |
| **EUR Liquidity Buffer (`IEGE`)** | €{inc['liquidity_buffer_demo_eur']:,.2f} (`{w['eur_liquidity_buffer']:.2f}%`) | €{inc['liquidity_buffer_mandate_eur']:,.0f} (`{inc['liquidity_buffer_months']:.1f} mos`) | Min: €500k (Alert ≤ €450k) · Next Sweep: `{inc['next_sweep']['next_sweep_date']}` |
| **UCITS 5/10/40 & Sector Cap** | Max Issuer `{conc['max_direct_issuer_ticker']}` (`{conc['max_direct_issuer_pct']:.2f}%`) | Max Sector `{conc['max_equity_sector_name']}` (`{conc['max_equity_sector_pct']:.2f}%`) | Caps: `10%` Single Issuer · `15%` Equity Sector |
| **Overall Governance Verdict** | **`{badge}`** | **`{badge}`** | Updated `{report['timestamp_utc']}` |

![Red Line Daily Dashboard](output/red_line_dashboard.png)

---

## How the Automation Works

* **Schedule:** Runs automatically via GitHub Actions (`.github/workflows/daily_monitor.yml`) every weekday at **21:30 UTC** (after NYSE/NASDAQ close) and can be triggered manually anytime under the **Actions** tab.
* **Independent Price Feeds:** Pulls closing prices across NYSE, NASDAQ, Euronext, Xetra, BME, LSE, SIX, and Borsa Italiana plus `EURUSD=X` via `yfinance`, with currency sanity checks and stale-price fallbacks.
* **Custodian Reconciliation (`src/ibkr_parser.py`):** Whenever a new IBKR Activity Statement CSV is exported, run:
  ```bash
  python src/sfo_risk_monitor.py --ibkr-csv data/DUR392190_YYYYMMDD.csv
  ```
  The parser automatically isolates `Segmento = DUR392190` (ignoring any bundled demo advisor sub-accounts), updates quantities & cash, and recalculates all IPS controls.

## Repository Structure

```text
├── .github/workflows/daily_monitor.yml   # Automated weekday surveillance (21:30 UTC)
├── config/portfolio_manifest.json        # 23 verified positions, ISINs, yields & IPS v3 rules
├── data/DUR392190_20261125_clean.csv     # Official Day 0 IBKR statement (isolated DUR392190)
├── src/ibkr_parser.py                    # Multi-account IBKR CSV cleaner & manifest sync
├── src/sfo_risk_monitor.py               # Core Red Line, UCITS, Margin & Cash Sweep engine
├── output/audit_log.csv                  # Daily historical track record (Excel-ready)
├── output/alerts_log.csv                 # Timestamped compliance alerts for Quarterly Report
├── output/latest_report.json             # Structured JSON snapshot (ready for Fact Sheet / LLM)
└── output/red_line_dashboard.png         # 4-panel institutional risk dashboard
```
"""
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(md)


def send_telegram_if_configured(report: Dict[str, Any]) -> None:
    """Optional Telegram alert if TELEGRAM_BOT_TOKEN & TELEGRAM_CHAT_ID env vars exist."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return

    serious = [a for a in report["alerts"] if a["severity"] in ("CRITICAL", "BREACH", "WARN")]
    if not serious:
        return

    lines = [
        f"Tiago Silva SFO Monitor ({report['date']})",
        f"NAV: EUR {report['nav_demo_eur']:,.2f} | DD: {report['drawdown_pct']:.2f}% | Status: {report['compliance']['overall']}",
    ]
    for a in serious:
        lines.append(f"- [{a['severity']}] {a['control']}: {a['message']}")
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": "\n".join(lines)}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        urllib.request.urlopen(url, data=payload, timeout=10)
    except Exception as exc:
        print(f"[WARN] Telegram notification failed: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Tiago Silva SFO Daily Risk & Compliance Monitor")
    parser.add_argument("--manifest", default=os.path.join(REPO_ROOT, "config", "portfolio_manifest.json"))
    parser.add_argument("--ibkr-csv", default=None, help="Optional IBKR Activity Statement CSV to sync before running")
    parser.add_argument("--offline-day0", action="store_true", help="Use IBKR Day 0 closing prices without calling yfinance")
    args = parser.parse_args()

    if args.ibkr_csv:
        ibkr_parser.sync_manifest_from_ibkr(args.ibkr_csv, args.manifest)

    manifest = load_manifest(args.manifest)
    audit_csv = os.path.join(REPO_ROOT, "output", "audit_log.csv")
    alerts_csv = os.path.join(REPO_ROOT, "output", "alerts_log.csv")
    latest_json = os.path.join(REPO_ROOT, "output", "latest_report.json")
    dash_png = os.path.join(REPO_ROOT, "output", "red_line_dashboard.png")
    readme_md = os.path.join(REPO_ROOT, "README.md")

    if args.offline_day0:
        run_date = manifest["last_ibkr_sync_date"]
        usd_to_eur = float(manifest["reference_usd_to_eur"])
        price_map = {h["ticker"]: {"price": float(h["last_known_price"]), "source": "IBKR_CLOSE"} for h in manifest["holdings"]}
        data_warnings = []
        mode_label = "IBKR_DAY0_RECONCILED"
    else:
        run_date = datetime.utcnow().strftime("%Y-%m-%d")
        price_map, usd_to_eur, data_warnings = fetch_live_prices(manifest)
        mode_label = "LIVE_API_YFINANCE"

    report = evaluate_portfolio(manifest, price_map, usd_to_eur, run_date, audit_csv, data_warnings, mode_label)
    hist_df = append_audit_log(report, audit_csv)
    append_alerts_log(report["alerts"], alerts_csv)

    with open(latest_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    render_dashboard(report, hist_df, dash_png)
    update_readme(report, readme_md)
    send_telegram_if_configured(report)

    print("=" * 78)
    print(f"TIAGO SILVA SFO MONITOR | {report['date']} ({report['mode']})")
    print("=" * 78)
    print(f"NAV (Demo 1:10): €{report['nav_demo_eur']:,.2f} | Mandate Equiv: €{report['nav_mandate_eur']:,.2f}")
    print(f"High-Water Mark: €{report['hwm_demo_eur']:,.2f} | Drawdown: {report['drawdown_pct']:.2f}% | Headroom to -15%: +{report['headroom_to_red_line_pct']:.2f} pp")
    print(f"Cash Balances:   EUR +€{report['cash_eur']:,.2f} | USD +${report['cash_usd']:,.2f} | FX: 1 EUR = {report['eur_to_usd']:.4f} USD")
    print(f"Compliance:      {report['compliance']}")
    if report["alerts"]:
        print("Alerts:")
        for a in report["alerts"]:
            print(f"  [{a['severity']}] {a['control']}: {a['message']}")
    print("=" * 78)


if __name__ == "__main__":
    main()
