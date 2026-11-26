"""
Tiago Silva Single Family Office — Automated Excel Portfolio Manager Updater
----------------------------------------------------------------------------
Updates output/Tiago_Silva_SFO_Gestor_Portfolio.xlsx every night when GitHub
Actions runs src/sfo_risk_monitor.py:
  - Preserves 100% of formatting, formulas, charts, dividends, and trade logs.
  - Updates live EUR/USD FX rate, cash balances, accrued interest, and closing
    prices for all 23 holdings on tab '2_Posicoes_Carteira'.
  - Syncs the complete daily track record from output/audit_log.csv into tab
    '3_Evolucao_Diaria' (rows 25..284) and dynamically adjusts the NAV vs.
    Red Line chart ranges on both '3_Evolucao_Diaria' and '1_Dashboard_Executivo'
    so charts always display the exact populated dates with zero blank points.
"""

import os
from datetime import date
from typing import Dict, Any
import pandas as pd
import openpyxl
from openpyxl.chart import Reference


EVO_HDR_ROW = 24
EVO_START_ROW = 25
EVO_MAX_ROW = 284


def _update_line_chart_range(chart, ws_evo, last_row: int) -> None:
    """Safely update a 3-series LineChart (NAV, HWM, Red Line) to exact last_row."""
    # Update series value ranges (cols C=3, H=8, M=13) and category range (col A=1)
    sheet_name = ws_evo.title
    cols = [3, 8, 13]
    col_letters = ["C", "H", "M"]
    for idx, ser in enumerate(chart.series):
        if idx < len(col_letters):
            cl = col_letters[idx]
            if ser.val is not None and ser.val.numRef is not None:
                ser.val.numRef.f = f"'{sheet_name}'!${cl}${EVO_START_ROW}:${cl}${last_row}"
            if ser.cat is not None and ser.cat.numRef is not None:
                ser.cat.numRef.f = f"'{sheet_name}'!$A${EVO_START_ROW}:$A${last_row}"
            elif ser.cat is not None and ser.cat.strRef is not None:
                ser.cat.strRef.f = f"'{sheet_name}'!$A${EVO_START_ROW}:$A${last_row}"


def update_excel_portfolio(
    xlsx_path: str,
    report: Dict[str, Any],
    hist_df: pd.DataFrame,
) -> None:
    """Update the live Excel Portfolio Manager workbook in-place."""
    if not os.path.exists(xlsx_path):
        print(f"[EXCEL] Workbook not found at {xlsx_path}; skipping Excel update.")
        return

    wb = openpyxl.load_workbook(xlsx_path)

    # -----------------------------------------------------------------
    # 1. Update Tab '2_Posicoes_Carteira' (FX, Cash, Accrued, Quantities, Live Prices)
    # -----------------------------------------------------------------
    ws_pos = wb["2_Posicoes_Carteira"]
    ws_pos["C5"] = round(float(report["eur_to_usd"]), 6)
    ws_pos["G5"] = round(float(report["cash_eur"]), 2)
    ws_pos["I5"] = round(float(report["cash_usd"]), 2)
    ws_pos["M5"] = round(float(report.get("accrued_interest_eur", 0.0)), 2)

    snap_by_ticker = {h["ticker"]: h for h in report.get("holdings_snapshot", [])}
    updated_pos = 0
    for r in range(9, 32):
        tk = ws_pos.cell(r, 1).value
        if tk in snap_by_ticker:
            item = snap_by_ticker[tk]
            ws_pos.cell(r, 8, float(item["quantity"]))
            ws_pos.cell(r, 10, round(float(item["live_price"]), 6))
            updated_pos += 1

    # -----------------------------------------------------------------
    # 2. Update Tab '3_Evolucao_Diaria' from audit_log.csv
    # -----------------------------------------------------------------
    ws_evo = wb["3_Evolucao_Diaria"]
    hist_sorted = hist_df.sort_values("date").reset_index(drop=True)
    max_days = EVO_MAX_ROW - EVO_START_ROW + 1
    if len(hist_sorted) > max_days:
        hist_sorted = hist_sorted.tail(max_days).reset_index(drop=True)

    for idx, row in hist_sorted.iterrows():
        r = EVO_START_ROW + idx
        dt_val = date.fromisoformat(str(row["date"])[:10])
        mode_raw = str(row.get("mode", "LIVE_API"))
        mode_short = "IBKR_DAY0" if "DAY0" in mode_raw else ("IBKR_SYNC" if "IBKR" in mode_raw else "LIVE_API")
        usd_to_eur = float(row.get("usd_to_eur", 0.91114))
        eur_to_usd = round(1.0 / usd_to_eur, 4) if usd_to_eur else 1.0975

        ws_evo.cell(r, 1, dt_val)
        ws_evo.cell(r, 2, mode_short)
        ws_evo.cell(r, 3, round(float(row["nav_demo_eur"]), 2))
        ws_evo.cell(r, 4, round(float(row["cash_eur"]), 2))
        ws_evo.cell(r, 5, round(float(row["cash_usd"]), 2))
        ws_evo.cell(r, 6, eur_to_usd)

    # Clear any stale rows beyond current history
    last_pop_row = EVO_START_ROW + len(hist_sorted) - 1
    for r in range(last_pop_row + 1, EVO_MAX_ROW + 1):
        if ws_evo.cell(r, 1).value is not None:
            for ci in range(1, 7):
                ws_evo.cell(r, ci, None)
        else:
            break

    # -----------------------------------------------------------------
    # 3. Dynamically refresh LineChart ranges on Tab 3 & Tab 1
    # -----------------------------------------------------------------
    if len(hist_sorted) >= 1:
        for ch in ws_evo._charts:
            _update_line_chart_range(ch, ws_evo, last_pop_row)
        ws_dash = wb["1_Dashboard_Executivo"]
        # Chart 3 (index 2) on Dashboard is the NAV vs. Red Line LineChart
        for ch in ws_dash._charts:
            if ch.__class__.__name__ == "LineChart":
                _update_line_chart_range(ch, ws_evo, last_pop_row)

    # Trigger full formula recalculation when opened in Excel
    wb.calculation.fullCalcOnLoad = True
    wb.save(xlsx_path)
    print(f"[EXCEL] Updated {xlsx_path}: {updated_pos} live positions | {len(hist_sorted)} daily track-record rows.")
