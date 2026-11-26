"""
Tiago Silva Single Family Office — IBKR Activity Statement Parser
-----------------------------------------------------------------
Extracts ONLY the isolated account segment (DUR392190) from multi-account
or single-account Interactive Brokers CSV Activity Statements (PT or EN),
discarding any bundled demo/advisor sub-accounts (e.g. DUC00074).
"""

import csv
import json
import os
import sys
from datetime import datetime
from typing import Dict, Any, List, Tuple


def _to_float(val: str) -> float:
    if val is None:
        return 0.0
    s = str(val).strip().replace(",", "")
    if not s or s == "--":
        return 0.0
    return float(s)


def extract_account_block(csv_path: str, account_id: str = "DUR392190") -> List[List[str]]:
    """Return only the CSV rows belonging to the target IBKR account segment."""
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    stmt_starts = [
        i for i, r in enumerate(rows)
        if r and r[0] == "Statement" and len(r) > 1 and r[1] == "Header"
    ]
    if not stmt_starts:
        return rows

    # Search for an isolated block matching account_id in Account Information
    for idx, start in enumerate(stmt_starts):
        end = stmt_starts[idx + 1] if idx + 1 < len(stmt_starts) else len(rows)
        block = rows[start:end]
        for r in block:
            if not r or len(r) < 4:
                continue
            sec = r[0].strip()
            if sec in ("Informações da conta", "Account Information") and r[1] == "Data":
                field_val = r[3].strip()
                if field_val == account_id or field_val.startswith(account_id + " "):
                    # Prefer an isolated segment (not consolidated multi-account)
                    if "consolidado" not in field_val.lower() and "consolidated" not in field_val.lower():
                        return block

    # Fallback: single-block CSV
    if len(stmt_starts) == 1:
        return rows
    raise ValueError(f"Isolated block for account '{account_id}' not found in {csv_path}")


def parse_ibkr_csv(csv_path: str, account_id: str = "DUR392190") -> Dict[str, Any]:
    """Parse isolated IBKR statement block into structured portfolio state."""
    block = extract_account_block(csv_path, account_id=account_id)
    sections: Dict[str, List[List[str]]] = {}
    for r in block:
        if r:
            sections.setdefault(r[0].strip(), []).append(r)

    # 1. Statement metadata (period date)
    period_raw = ""
    for r in sections.get("Statement", []):
        if len(r) >= 4 and r[1] == "Data" and r[2].strip() == "Period":
            period_raw = r[3].strip()

    # Convert Portuguese/English IBKR date like "Novembro 25, 2026" -> "2026-11-25"
    pt_months = {
        "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
        "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
        "outubro": 10, "novembro": 11, "dezembro": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
    }
    as_of_date = datetime.utcnow().strftime("%Y-%m-%d")
    if period_raw:
        tokens = period_raw.replace(",", "").split()
        if len(tokens) == 3 and tokens[0].lower() in pt_months:
            as_of_date = f"{int(tokens[2]):04d}-{pt_months[tokens[0].lower()]:02d}-{int(tokens[1]):02d}"

    # 2. FX rate (USD -> EUR)
    usd_to_eur = 0.911140
    fx_sec = sections.get("Taxa de câmbio da moeda-base", []) + sections.get("Base Currency Exchange Rate", [])
    for r in fx_sec:
        if len(r) >= 4 and r[1] == "Data" and r[2].strip() == "USD":
            usd_to_eur = _to_float(r[3])
            break

    # 3. Official NAV & Accrued Interest
    nav_official_eur = 0.0
    accrued_interest_eur = 0.0
    nav_sec = sections.get("Valor patrimonial líquido", []) + sections.get("Net Asset Value", [])
    for r in nav_sec:
        if len(r) >= 7 and r[1] == "Data":
            label = r[2].strip().lower()
            if label == "total":
                nav_official_eur = _to_float(r[6])
            elif "juros acumulados" in label or "accrued interest" in label:
                accrued_interest_eur = _to_float(r[6])

    # 4. Cash Balances (EUR & USD)
    cash_eur = 0.0
    cash_usd = 0.0
    cash_sec = sections.get("Relatório de caixa", []) + sections.get("Cash Report", [])
    for r in cash_sec:
        if len(r) >= 5 and r[1] == "Data":
            metric = r[2].strip().lower()
            ccy = r[3].strip().upper()
            if metric in ("saldo final", "ending cash"):
                if ccy == "EUR":
                    cash_eur = _to_float(r[4])
                elif ccy == "USD":
                    cash_usd = _to_float(r[4])

    # 5. Instrument metadata (ISIN, Exchange, Type, Description)
    inst_map: Dict[str, Dict[str, str]] = {}
    inst_sec = (
        sections.get("Informações sobre instrumento financeiro", [])
        + sections.get("Financial Instrument Information", [])
    )
    for r in inst_sec:
        if len(r) >= 11 and r[1] == "Data":
            sym = r[3].strip()
            inst_map[sym] = {
                "description": r[4].strip(),
                "conid": r[5].strip(),
                "isin": r[6].strip(),
                "exchange": r[8].strip(),
                "security_type": r[10].strip(),
            }

    # 6. Open Positions
    positions: Dict[str, Dict[str, Any]] = {}
    pos_sec = sections.get("Posições abertas", []) + sections.get("Open Positions", [])
    for r in pos_sec:
        if len(r) >= 13 and r[1] == "Data" and r[2].strip() == "Summary":
            ccy = r[4].strip().upper()
            sym = r[5].strip()
            meta = inst_map.get(sym, {})
            positions[sym] = {
                "ticker": sym,
                "currency": ccy,
                "quantity": _to_float(r[6]),
                "cost_price": _to_float(r[8]),
                "cost_basis": _to_float(r[9]),
                "close_price": _to_float(r[10]),
                "market_value_native": _to_float(r[11]),
                "unrealized_pnl_native": _to_float(r[12]),
                "isin": meta.get("isin", ""),
                "exchange": meta.get("exchange", ""),
                "security_type": meta.get("security_type", ""),
                "description": meta.get("description", ""),
            }

    return {
        "account_id": account_id,
        "as_of_date": as_of_date,
        "period_raw": period_raw,
        "usd_to_eur": usd_to_eur,
        "eur_to_usd": round(1.0 / usd_to_eur, 6) if usd_to_eur else 0.0,
        "nav_official_eur": nav_official_eur,
        "accrued_interest_eur": accrued_interest_eur,
        "cash": {"EUR": cash_eur, "USD": cash_usd},
        "positions": positions,
    }


def save_clean_csv(input_csv: str, output_csv: str, account_id: str = "DUR392190") -> int:
    """Export only the isolated DUR392190 segment to a clean standalone CSV."""
    block = extract_account_block(input_csv, account_id=account_id)
    os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
    with open(output_csv, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(block)
    return len(block)


def sync_manifest_from_ibkr(csv_path: str, manifest_path: str, account_id: str = "DUR392190") -> Dict[str, Any]:
    """Update quantities, cash balances, accrued interest, and last prices in portfolio_manifest.json."""
    parsed = parse_ibkr_csv(csv_path, account_id=account_id)
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    manifest["last_ibkr_sync_date"] = parsed["as_of_date"]
    manifest["reference_usd_to_eur"] = parsed["usd_to_eur"]
    manifest["cash_eur"] = round(parsed["cash"]["EUR"], 2)
    manifest["cash_usd"] = round(parsed["cash"]["USD"], 2)
    manifest["accrued_interest_eur"] = round(parsed["accrued_interest_eur"], 2)

    updated = 0
    for h in manifest["holdings"]:
        tk = h["ticker"]
        if tk in parsed["positions"]:
            p = parsed["positions"][tk]
            h["quantity"] = p["quantity"]
            h["cost_price"] = round(p["cost_price"], 6)
            h["last_known_price"] = round(p["close_price"], 6)
            updated += 1

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"[IBKR SYNC] Account {account_id} ({parsed['as_of_date']}): {updated} positions updated | "
          f"EUR Cash: €{parsed['cash']['EUR']:,.2f} | USD Cash: ${parsed['cash']['USD']:,.2f} | "
          f"Official NAV: €{parsed['nav_official_eur']:,.2f}")
    return parsed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python src/ibkr_parser.py <ibkr_statement.csv> [--clean-out <out.csv>] [--sync-manifest <manifest.json>]")
        sys.exit(1)

    in_csv = sys.argv[1]
    res = parse_ibkr_csv(in_csv)
    print(f"Parsed {res['account_id']} ({res['as_of_date']}): {len(res['positions'])} positions | "
          f"NAV €{res['nav_official_eur']:,.2f} | EUR €{res['cash']['EUR']:,.2f} | USD ${res['cash']['USD']:,.2f}")

    if "--clean-out" in sys.argv:
        out_path = sys.argv[sys.argv.index("--clean-out") + 1]
        n = save_clean_csv(in_csv, out_path)
        print(f"Saved clean CSV ({n} rows) to {out_path}")

    if "--sync-manifest" in sys.argv:
        m_path = sys.argv[sys.argv.index("--sync-manifest") + 1]
        sync_manifest_from_ibkr(in_csv, m_path)
