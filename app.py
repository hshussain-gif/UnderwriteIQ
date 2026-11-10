"""CreditScope AI: deterministic underwriting. Run: streamlit run app.py.
Dependencies: streamlit, pandas, plotly. All monetary inputs are full USD.
"""
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from edgar_import import (MAX_TOTAL_BYTES, build_import_matrix, fact_browser,
                          merge_filings, select_fact)

YEARS = ["Year 1", "Year 2", "Year 3"]
STATEMENTS = {
    "Income Statement": {
        "Revenue": [40_000_000, 45_000_000, 50_000_000],
        "Cost of goods sold": [26_000_000, 29_250_000, 32_500_000],
        "Operating expenses (reported)": [8_300_000, 9_250_000, 10_200_000],
        "Depreciation and amortization": [1_500_000, 1_600_000, 1_700_000],
        "Interest expense": [900_000, 900_000, 900_000],
        "Other non-operating income / (expense)": [0, 0, 0],
        "Income tax expense": [1_300_000, 1_400_000, 1_500_000],
    },
    "Balance Sheet": {
        "Cash and equivalents": [2_000_000, 2_500_000, 3_000_000],
        "Short-term investments": [500_000, 500_000, 500_000],
        "Accounts receivable": [5_000_000, 6_000_000, 7_000_000],
        "Inventory": [6_000_000, 6_500_000, 7_000_000],
        "Other current assets": [500_000, 500_000, 500_000],
        "Net property, plant and equipment": [16_000_000, 17_000_000, 18_000_000],
        "Other noncurrent assets": [0, 0, 0],
        "Accounts payable and accruals": [4_000_000, 4_500_000, 5_000_000],
        "Short-term debt": [1_000_000, 1_000_000, 1_000_000],
        "Current portion of long-term debt": [1_500_000, 1_500_000, 1_500_000],
        "Other current liabilities": [0, 0, 0],
        "Long-term debt (excluding current portion)": [9_500_000, 9_500_000, 9_500_000],
        "Other noncurrent liabilities": [2_000_000, 2_500_000, 3_000_000],
        "Shareholders' equity": [12_000_000, 14_000_000, 16_000_000],
    },
    "Cash Flow": {
        "Beginning cash": [1_500_000, 2_000_000, 2_500_000],
        "Cash from operations (after interest and taxes)": [5_000_000, 6_700_000, 8_400_000],
        "Capital expenditures": [3_000_000, 4_000_000, 5_000_000],
        "Other investing cash flow": [0, 0, 0],
        "Existing debt proceeds": [1_500_000, 1_500_000, 1_500_000],
        "Existing principal repayments": [1_500_000, 1_500_000, 1_500_000],
        "Dividends": [1_500_000, 2_200_000, 2_900_000],
        "Other financing cash flow": [0, 0, 0],
        "FX effect on cash": [0, 0, 0],
        "Existing cash interest paid": [900_000, 900_000, 900_000],
    },
}


FACILITY_TYPES = ("Fully Amortizing", "Interest-Only (I/O) Period", "Revolver")
RISK_LABELS = {1: "Low Risk", 2: "Moderate Risk", 3: "Acceptable Risk", 4: "Watch", 5: "High Risk"}
PALETTE = {"background": "#0e1117", "panel": "#11141a", "grid": "#262730",
           "text": "#e6edf3", "green": "#00e676", "blue": "#00b0ff", "stress": "#ff3d00"}


@dataclass(frozen=True)
class Facility:
    """Amount means term-loan principal or revolver commitment; rates are percentages."""
    amount: float = 8_000_000.0
    annual_rate: float = 7.5
    years: int = 10
    payments_per_year: int = 12
    kind: str = "Fully Amortizing"
    io_months: int = 0
    utilization: float = 60.0
    commitment_fee: float = 0.50


def load_baseline_data():
    """Canonical loader contract: one unique row per (Statement, Line item), USD year columns.

    A future CSV/Excel parser must return this schema and pass validate_financial_data.
    No calculation relies on row order, editor position, or loader implementation.
    """
    return pd.DataFrame([{"Statement": statement, "Line item": item,
                          **dict(zip(YEARS, map(float, amounts)))}
                         for statement, items in STATEMENTS.items() for item, amounts in items.items()])


def validate_financial_data(frame):
    """Reject schema errors, duplicate/missing rows, nonfinite values and invalid signs."""
    columns = ["Statement", "Line item", *YEARS]
    if not isinstance(frame, pd.DataFrame) or set(frame.columns) != set(columns):
        raise ValueError(f"Financial data must contain exactly these columns: {columns}")
    result = frame[columns].copy()
    required = {(s, item) for s, items in STATEMENTS.items() for item in items}
    keys = list(zip(result["Statement"], result["Line item"]))
    if len(set(keys)) != len(keys) or set(keys) != required:
        raise ValueError("Statement/line-item pairs must be unique and match the baseline schema.")
    signed = {"Shareholders' equity", "Income tax expense", "Cash from operations (after interest and taxes)",
              "Other non-operating income / (expense)", "Other investing cash flow",
              "Other financing cash flow", "FX effect on cash"}
    for year in YEARS:
        result[year] = pd.to_numeric(result[year], errors="coerce").astype(float)
        if not result[year].map(lambda value: math.isfinite(float(value))).all():
            raise ValueError(f"{year}: every financial input must be a finite number.")
        invalid = (result[year] < 0) & ~result["Line item"].isin(signed)
        if invalid.any():
            raise ValueError(f"{year}: negative values are not allowed for {', '.join(result.loc[invalid, 'Line item'])}.")
    return result


def ratio(numerator, denominator):
    return numerator / denominator if denominator > 0 else float("nan")


def money(value):
    return "Unbounded under assumptions" if math.isinf(value) else f"${value:,.2f}"


def multiple(value):
    return f"{value:.2f}x" if math.isfinite(value) else "N/M"


def financials(frame, year):
    """Read named rows from the canonical matrix; callers validate once at the boundary."""
    values = frame.set_index("Line item")[year].to_dict()
    gross = values["Revenue"] - values["Cost of goods sold"]
    ebitda = gross - values["Operating expenses (reported)"] + values["Depreciation and amortization"]
    pretax = (ebitda - values["Depreciation and amortization"] - values["Interest expense"]
              + values["Other non-operating income / (expense)"])
    quick = sum(values[k] for k in ("Cash and equivalents", "Short-term investments", "Accounts receivable"))
    current_assets = quick + values["Inventory"] + values["Other current assets"]
    current_liabilities = sum(values[k] for k in ("Accounts payable and accruals", "Short-term debt", "Current portion of long-term debt", "Other current liabilities"))
    debt = sum(values[k] for k in ("Short-term debt", "Current portion of long-term debt", "Long-term debt (excluding current portion)"))
    service = values["Existing principal repayments"] + values["Existing cash interest paid"]
    cash = (values["Beginning cash"] + values["Cash from operations (after interest and taxes)"]
            - values["Capital expenditures"] + values["Existing debt proceeds"]
            - values["Existing principal repayments"] - values["Dividends"]
            + values["Other investing cash flow"] + values["Other financing cash flow"] + values["FX effect on cash"])
    balance_gap = (current_assets + values["Net property, plant and equipment"] + values["Other noncurrent assets"] - current_liabilities
                   - values["Long-term debt (excluding current portion)"]
                   - values["Other noncurrent liabilities"] - values["Shareholders' equity"])
    return {"values": values, "ebitda": ebitda, "pretax": pretax, "debt": debt, "service": service,
            "balance_gap": balance_gap, "cash_gap": cash - values["Cash and equivalents"],
            "ratios": {"Current Ratio": ratio(current_assets, current_liabilities), "Quick Ratio": ratio(quick, current_liabilities),
                       "Total Debt / EBITDA": ratio(debt, ebitda), "Debt-to-Equity": ratio(debt, values["Shareholders' equity"]),
                       "Gross Margin": ratio(gross, values["Revenue"]), "EBITDA Margin": ratio(ebitda, values["Revenue"]),
                       "Net Margin": ratio(pretax - values["Income tax expense"], values["Revenue"]), "DSCR": ratio(ebitda, service)}}


def consistency_issues(frame):
    issues = []
    for year in YEARS:
        result = financials(frame, year)
        for label, key in (("balance sheet", "balance_gap"), ("cash reconciliation", "cash_gap")):
            if abs(result[key]) > 1:
                issues.append(f"{year} {label} difference: {money(result[key])}")
    for previous, current in zip(YEARS, YEARS[1:]):
        if abs(financials(frame, current)["values"]["Beginning cash"] - financials(frame, previous)["values"]["Cash and equivalents"]) > 1:
            issues.append(f"{current} opening cash differs from {previous} closing cash.")
    return issues + list(frame.attrs.get("source_reconciliation_issues", []))


def source_reconciliations(report, frame):
    """Compare reviewed inputs to separately tagged statement totals, not just plugs."""
    rows = []
    for year, (start, end) in zip(YEARS, report["periods"]):
        history = financials(frame, year)
        v = history["values"]
        checks = [
            ("Operating income", ("OperatingIncomeLoss",), False, history["ebitda"] - v["Depreciation and amortization"]),
            ("Pretax income", ("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",), False, history["pretax"]),
            ("Net income", ("ProfitLoss", "NetIncomeLoss"), False, history["pretax"] - v["Income tax expense"]),
            ("Total assets", ("Assets",), True, sum(v[k] for k in ("Cash and equivalents", "Short-term investments", "Accounts receivable", "Inventory", "Other current assets", "Net property, plant and equipment", "Other noncurrent assets"))),
        ]
        for label, tags, instant, modeled in checks:
            expected, status, source = select_fact(report, tags, start, end, instant)
            rows.append({"Year": year, "Check": label, "Reported USD": expected,
                         "Modeled USD": modeled, "Difference USD": modeled - expected,
                         "Source status": status, "Source": source})
    return pd.DataFrame(rows)


def render_financial_input():
    """Review boundary: imported data cannot reach any engine until completed and approved."""
    st.subheader("Financial data source")
    mode = st.radio("Choose financial data", ["Sample company / manual entry", "Upload EDGAR 10-K files"],
                    horizontal=True, key="input_mode")
    config = {year: st.column_config.NumberColumn(year, format="$%.0f", required=True) for year in YEARS}
    if mode == "Sample company / manual entry":
        st.caption("Demo defaults only. Full USD; enter reported COGS and reported operating expenses, including their embedded operating D&A. Total operating D&A is added back once for EBITDA.")
        return st.data_editor(load_baseline_data(), hide_index=True, num_rows="fixed",
            disabled=["Statement", "Line item"], column_config=config, width="stretch", height=420, key="financial_matrix")

    st.info("REQUIRED FILE TYPE: Original SEC EDGAR 10-K / 10-K/A .htm or .html files containing embedded Inline XBRL tags. "
            "PDFs, scans, spreadsheets, and a saved EDGAR viewer/search page will not work. Use USD-reporting US-GAAP non-financial companies. "
            "A valid file enables extraction, but missing/custom tags and D&A/debt classifications can still require manual completion.")
    with st.expander("Exactly which files to download"):
        st.markdown("""
        1. On the company's EDGAR filing details page, find the primary **10-K** document.
        2. Open the **original HTML document**, outside the Inline XBRL viewer. Use the viewer's
           **Open as HTML** option if available, or open the underlying `/Archives/edgar/data/...htm` document.
        3. Save/download the original page as **HTML only** (`.htm` or `.html`). Do not print to PDF or save the viewer shell.
        4. Upload the latest **two annual 10-K filings** for the same company. If earlier balance-sheet or
           beginning-cash values are absent, add another older 10-K (up to four files).
        5. The importer selects three consecutive full fiscal years, prefers newer filings for comparable values,
           and excludes segment/dimensional facts. Conflicting values remain unresolved.

        Limit: **15 MB per file, 40 MB total**. No filing is fetched from the internet or executed by the app.
        Uploaded values are processed in this hosted app session; files are not committed to GitHub or saved to an app database.
        """)
    uploads = st.file_uploader("Drop original EDGAR Inline XBRL HTML filings here", type=["htm", "html"],
                              accept_multiple_files=True, key="edgar_uploads")
    if not uploads:
        st.caption("Upload filings to populate a blank review matrix. Demo data is never mixed into an import.")
        st.stop()
    files = sorted([(upload.name, upload.getvalue()) for upload in uploads], key=lambda item: item[0])
    if sum(len(data) for _, data in files) > MAX_TOTAL_BYTES:
        st.error("Combined files exceed 40 MB. Upload fewer/smaller original HTML filings.")
        st.stop()
    fingerprint = hashlib.sha256(b"".join(name.encode() + data for name, data in files)).hexdigest()
    try:
        if st.session_state.get("_filing_fingerprint") != fingerprint:
            with st.spinner("Reading consolidated financial tags and fiscal periods…"):
                report = merge_filings(files)
            st.session_state["_filing_report"] = report
            st.session_state["_filing_fingerprint"] = fingerprint
        report = st.session_state["_filing_report"]
    except ValueError as error:
        st.error(str(error))
        st.stop()
    st.success(f"Loaded {report['company']} · CIK {report['entity']} · {len(report['sources'])} filing(s)")
    periods = pd.DataFrame([{"Model column": year, "Fiscal start": start, "Fiscal end": end}
                           for year, (start, end) in zip(YEARS, report["periods"])])
    st.dataframe(periods, hide_index=True, width="stretch")
    seed = fingerprint[:16]
    imported, audit = build_import_matrix(report, load_baseline_data())
    with st.expander("1 · Extracted financial inputs and source evidence", expanded=True):
        st.caption("Blank cells mean missing or ambiguous—not zero. Fill them from the filing, including verified true zeros. "
                   "Derived rows are proposals to review. Pay particular attention to short-term borrowings, leases, dividends, other financing flows and cash interest. "
                   "Historical cash repayments can include refinancing or revolving turnover and may differ from prospective scheduled debt service.")
        edited = st.data_editor(imported, hide_index=True, num_rows="fixed", width="stretch", height=520,
            disabled=["Statement", "Line item"], column_config=config, key=f"import_matrix_{seed}")
        # Rebuild evidence after edits so manual values cannot masquerade as extracted facts.
        for index, record in audit.iterrows():
            row = edited.loc[edited["Line item"] == record["Line item"]].iloc[0]
            reviewed = row[record["Year"]]
            original = record["Imported value"]
            changed = pd.notna(reviewed) and (pd.isna(original) or reviewed != original)
            audit.at[index, "Reviewed value"] = reviewed
            if changed:
                audit.at[index, "Status"] = "User supplied / overridden — verify source"
        st.dataframe(audit, hide_index=True, width="stretch", height=280)
        st.download_button("Download extraction evidence / reviewed values", audit.to_csv(index=False),
                           "underwriteiq_import_audit.csv", "text/csv", key=f"audit_{seed}")
    with st.expander("Find unsupported/custom facts or verify a source number"):
        query = st.text_input("Search concept or source label", key=f"fact_search_{fingerprint}")
        browser = fact_browser(report)
        if query:
            browser = browser[browser["Concept"].str.contains(query, case=False, regex=False)
                              | browser["Source label"].str.contains(query, case=False, regex=False)]
        st.dataframe(browser.head(1000), hide_index=True, width="stretch", height=300)
        st.caption("Showing up to 1,000 matching facts. Segment facts are excluded from automatic mapping. "
                   "Custom facts require manual review; source labels may be truncated. All values shown here are full USD.")
        st.download_button("Download all extracted monetary facts", fact_browser(report).to_csv(index=False),
                           "underwriteiq_extracted_facts.csv", "text/csv")
        if report["warnings"]:
            st.warning(f"{len(report['warnings'])} facts were skipped because of unsupported values or transformations.")
            st.write(report["warnings"][:30])
    missing = edited[YEARS].isna()
    if missing.any().any():
        st.warning(f"{int(missing.sum().sum())} inputs remain blank. Complete the matrix before analysis can run.")
        st.stop()
    try:
        validated = validate_financial_data(edited)
    except ValueError as error:
        st.error(str(error))
        st.stop()
    checks = source_reconciliations(report, validated)
    with st.expander("2 · Reconcile against reported statement totals", expanded=True):
        st.dataframe(checks, hide_index=True, width="stretch")
        st.caption("Missing source totals are not verified. Differences may indicate a mapping issue, restricted cash, "
                   "noncontrolling interests or a broader reported expense scope. Review the filing before proceeding.")
    tolerance = 1.0
    gaps = checks[checks["Difference USD"].abs() > tolerance]
    validated.attrs["source_reconciliation_issues"] = [
        f"{row['Year']} {row['Check']} differs from the filing by {money(row['Difference USD'])}"
        for _, row in gaps.iterrows()]
    validated.attrs["filing_company"] = report["company"]
    validated.attrs["filing_periods"] = report["periods"]
    # Content-dependent confirmation expires on every financial edit.
    approval_key = hashlib.sha256((seed + validated.to_csv(index=False)).encode()).hexdigest()[:16]
    approved = st.checkbox("I reviewed all three years, corrected missing/custom mappings, verified that operating D&A is added back once and checked all debt repayments, "
                           "and reviewed the reconciliation differences. Run analysis on these reviewed values.", key=f"approve_import_{approval_key}")
    if not approved:
        st.info("Analysis, risk ratings and the credit memo unlock after review confirmation.")
        st.stop()
    return validated


def validate_facility(facility):
    numbers = (facility.amount, facility.annual_rate, facility.years, facility.payments_per_year,
               facility.io_months, facility.utilization, facility.commitment_fee)
    if not all(math.isfinite(float(v)) for v in numbers):
        raise ValueError("Facility parameters must be finite.")
    if facility.kind not in FACILITY_TYPES or facility.payments_per_year not in (4, 12):
        raise ValueError("Select a supported facility and monthly or quarterly payments.")
    if facility.years != int(facility.years) or not 1 <= facility.years <= 40:
        raise ValueError("Term must be a whole number from 1 to 40 years.")
    if facility.amount < 0 or facility.annual_rate < 0 or facility.commitment_fee < 0 or not 0 <= facility.utilization <= 100:
        raise ValueError("Use nonnegative amounts/rates and utilization between 0% and 100%.")
    if facility.kind == FACILITY_TYPES[1]:
        interval = 12 // facility.payments_per_year
        if facility.io_months < 0 or facility.io_months >= facility.years * 12 or facility.io_months % interval:
            raise ValueError(f"I/O months must be a multiple of {interval} and shorter than the term.")


def facility_schedule(facility):
    """One schedule engine for all facilities; end-of-period cash flows, USD cents.

    Revolver average utilization is constant: debt service = drawn interest +
    undrawn commitment fee. Principal repayment at maturity is NOT assumed.
    Term I/O preserves original maturity, amortizing over the remaining periods.
    """
    validate_facility(facility)
    cents = lambda value: Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    amount = cents(facility.amount)
    frequency = facility.payments_per_year
    count = int(facility.years) * frequency
    rate = Decimal(str(facility.annual_rate)) / 100 / frequency
    is_revolver = facility.kind == "Revolver"
    balance = cents(amount * Decimal(str(facility.utilization)) / 100) if is_revolver else amount
    io_periods = int(facility.io_months * frequency // 12) if facility.kind == FACILITY_TYPES[1] else 0
    amort_periods = count - io_periods
    payment = (amount / amort_periods if rate == 0 else amount * rate / (1 - (1 + rate) ** (-amort_periods)))
    payment = cents(payment)
    rows = []
    for period in range(1, count + 1):
        opening = balance
        interest = cents(opening * rate)
        fee = cents((amount - balance) * Decimal(str(facility.commitment_fee)) / 100 / frequency) if is_revolver else Decimal(0)
        principal = (Decimal(0) if is_revolver or period <= io_periods else
                     opening if period == count else min(opening, max(Decimal(0), payment - interest)))
        balance -= principal
        rows.append({"Period": period, "Loan year": (period - 1) // frequency + 1,
                     "Opening balance": float(opening), "Principal": float(principal),
                     "Interest": float(interest), "Commitment fee": float(fee),
                     "Debt service": float(principal + interest + fee), "Closing balance": float(balance)})
    return pd.DataFrame(rows)


def annual_schedule(schedule):
    return schedule.groupby("Loan year", as_index=False).agg({"Opening balance": "first", "Principal": "sum",
        "Interest": "sum", "Commitment fee": "sum", "Debt service": "sum", "Closing balance": "last"})


def assess_credit_risk(dscr, leverage, ebitda):
    if ebitda <= 0 or not all(math.isfinite(v) for v in (dscr, leverage, ebitda)):
        return {"grade": 5, "label": RISK_LABELS[5], "reason": "Nonpositive EBITDA or undefined coverage/leverage; conservative grade 5 and input review."}
    coverage_grade = 1 if dscr > 2 else 2 if dscr >= 1.5 else 3 if dscr >= 1.25 else 4 if dscr >= 1 else 5
    leverage_grade = 1 if leverage < 2.5 else 2 if leverage <= 3.5 else 3 if leverage <= 4.5 else 4 if leverage <= 5.5 else 5
    grade = max(coverage_grade, leverage_grade)
    return {"grade": grade, "label": RISK_LABELS[grade],
            "reason": f"DSCR {multiple(dscr)} maps to grade {coverage_grade}; leverage {multiple(leverage)} maps to grade {leverage_grade}. The weaker factor sets grade {grade}, using unrounded metrics."}


def evaluate_scenario(frame, facility, haircut=0, compression_bps=0, shock_bps=0):
    """Single source of truth for base/stress ratios, memo, capacity and dialogue."""
    if not all(math.isfinite(float(v)) for v in (haircut, compression_bps, shock_bps)) or not (0 <= haircut <= 50 and 0 <= compression_bps <= 3000 and 0 <= shock_bps <= 500):
        raise ValueError("Stress bounds: haircut 0–50%, compression 0–3000 bps, rate shock 0–500 bps.")
    history = financials(frame, "Year 3")
    values = history["values"]
    if values["Revenue"] <= 0:
        raise ValueError("Year 3 revenue must be positive for margin-based scenarios.")
    stressed_facility = replace(facility, annual_rate=facility.annual_rate + shock_bps / 100)
    schedule = facility_schedule(stressed_facility)
    annual = annual_schedule(schedule)
    first = annual.iloc[0]
    margin = history["ratios"]["EBITDA Margin"] - compression_bps / 10000
    revenue = values["Revenue"] * (1 - haircut / 100)
    ebitda = revenue * margin
    cogs = values["Cost of goods sold"] * (1 - haircut / 100)
    pretax = (ebitda - values["Depreciation and amortization"] - values["Interest expense"]
              - first["Interest"] - first["Commitment fee"] + values["Other non-operating income / (expense)"])
    tax_rate = min(1, max(0, values["Income tax expense"] / history["pretax"])) if history["pretax"] > 0 else 0
    service = history["service"] + first["Debt service"]
    debt = history["debt"] + schedule.iloc[0]["Opening balance"]  # Drawn debt, not undrawn commitment.
    dscr, leverage = ratio(ebitda, service), ratio(debt, ebitda)
    # CFO already includes existing cash interest/tax; deduct only incremental charges.
    residual = (values["Cash from operations (after interest and taxes)"] + ebitda - history["ebitda"]
                - first["Interest"] - first["Commitment fee"] - values["Capital expenditures"]
                - values["Existing principal repayments"] - first["Principal"] - values["Dividends"]
                + min(0, values["Other investing cash flow"]) + min(0, values["Other financing cash flow"]))
    if not all(math.isfinite(v) for v in (ebitda, debt, service, residual, pretax)):
        raise ValueError("Derived totals overflow; reduce the magnitude of financial inputs.")
    return {"facility": stressed_facility, "schedule": schedule, "annual": annual, "ebitda": ebitda,
        "margin": margin, "debt": debt, "service": service, "dscr": dscr, "leverage": leverage,
        "residual_cash": residual, "haircut": haircut,
        "risk": assess_credit_risk(dscr, leverage, ebitda),
        "income": {"Revenue": revenue, "COGS": cogs, "Gross profit": revenue - cogs,
                   "Operating expenses (reported)": revenue - cogs - ebitda + values["Depreciation and amortization"],
                   "Operating income / EBIT": ebitda - values["Depreciation and amortization"],
                   "D&A add-back (embedded in costs)": values["Depreciation and amortization"], "EBITDA": ebitda,
                   "D&A deduction from EBITDA": values["Depreciation and amortization"], "Existing book interest": values["Interest expense"],
                   "Other non-operating income / (expense)": values["Other non-operating income / (expense)"],
                   "New facility interest": first["Interest"], "Commitment fee": first["Commitment fee"],
                   "Pretax income": pretax, "Tax expense": max(0, pretax) * tax_rate,
                   "Net income": pretax - max(0, pretax) * tax_rate}}


def debt_capacity(frame, scenario, target=1.25, leverage_limit=4.5):
    """Maximize commitment/principal using cent bisection and actual schedules.

    Existing debt remains fixed; same facility terms/utilization apply to all new debt.
    No finite maximum is invented for a zero-draw, zero-fee undrawn commitment.
    """
    if target <= 0 or leverage_limit <= 0:
        raise ValueError("Capacity limits must be positive.")
    history = financials(frame, "Year 3")
    facility = scenario["facility"]
    budget = scenario["ebitda"] / target - history["service"]
    utilization = facility.utilization / 100 if facility.kind == "Revolver" else 1
    debt_budget = leverage_limit * scenario["ebitda"] - history["debt"]
    if scenario["ebitda"] <= 0 or budget < 0 or debt_budget < 0:
        return {"maximum": 0.0, "headroom": 0.0, "reduction": facility.amount,
                "reason": "Existing obligations already breach the policy; reducing only the new facility is insufficient."}
    def feasible(cents):
        schedule = facility_schedule(replace(facility, amount=cents / 100))
        peak_service = annual_schedule(schedule)["Debt service"].max()
        return peak_service <= budget and schedule.iloc[0]["Opening balance"] <= debt_budget
    if utilization == 0 and facility.commitment_fee == 0:
        maximum = math.inf
    else:
        low, high = 0, 100_000_000  # $1M initial bracket, expanded deterministically.
        while feasible(high):
            high *= 2
        while low + 1 < high:
            mid = (low + high) // 2
            if feasible(mid):
                low = mid
            else:
                high = mid
        maximum = low / 100
    return {"maximum": maximum, "headroom": max(0, maximum - facility.amount),
            "reduction": max(0, facility.amount - maximum),
            "reason": "Maximum new facility including the proposal, constrained by 1.25x DSCR in every modeled loan year and 4.50x funded leverage at constant stressed EBITDA; no collateral or maturity refinancing constraint is modeled."}


def credit_memo(frame, base, stressed, capacity, haircut, compression, shock):
    history = financials(frame, "Year 3")
    first_revenue = financials(frame, "Year 1")["values"]["Revenue"]
    values, facility = history["values"], stressed["facility"]
    issues = consistency_issues(frame)
    if issues:
        decision = "DEFER — reconcile inputs: " + "; ".join(issues)
    elif stressed["risk"]["grade"] == 5:
        decision = "DECLINE AS STRUCTURED — restore sustainable coverage and reduce leverage before resubmission."
    elif (stressed["risk"]["grade"] == 4 or stressed["residual_cash"] < 0
          or facility.amount > capacity["maximum"] + 0.005):
        decision = "RESTRUCTURE / DEFER — address first-year and later-year coverage, leverage, capex and distribution requirements before advancing."
    else:
        decision = "CONDITIONAL SUPPORT — proceed to collateral, management, loan-purpose and covenant diligence."
    vulnerability = ("CRITICAL VULNERABILITY: Stressed DSCR falls below the project's 1.15x threshold, indicating limited repayment cushion. "
                     + ("EBITDA does not cover scheduled service." if stressed["dscr"] < 1 else "Scheduled service remains covered, but the screening buffer is inadequate.")
                     if math.isfinite(stressed["dscr"]) and stressed["dscr"] < 1.15 else
                     "Selected stress retains the 1.15x warning buffer." if math.isfinite(stressed["dscr"]) else
                     "DSCR is undefined; review debt-service inputs.")
    maturity = (f"Revolver utilization is {facility.utilization:.1f}%; funded debt is based on average draws, while commitment is {money(facility.amount)}. "
                f"The {facility.commitment_fee:.2f}% fee applies only to undrawn commitment. The modeled "
                f"{money(stressed['schedule'].iloc[-1]['Closing balance'])} maturity draw requires repayment or refinancing; principal takeout is excluded from DSCR."
                if facility.kind == "Revolver" else
                f"The facility fully amortizes by maturity; the I/O period is {facility.io_months if facility.kind == FACILITY_TYPES[1] else 0} months.")
    growth = f"Two-year revenue CAGR is {(values['Revenue'] / first_revenue) ** 0.5 - 1:.1%}." if first_revenue > 0 else "Revenue CAGR is undefined."
    company = frame.attrs.get("filing_company", "Hypothetical manufacturing borrower")
    # Treat source metadata as literal text rather than user-authored Markdown.
    company = "".join(ch for ch in str(company) if ch.isalnum() or ch in " .,&()-")
    source_periods = frame.attrs.get("filing_periods", [])
    origin = (f"Reviewed EDGAR import: {company}. Fiscal years end " + ", ".join(end for _, end in source_periods) + "."
              if source_periods else "Sample/manual financial inputs.")
    return f"""# UnderwriteIQ · Credit Memorandum

{origin}

## Executive Summary
{company} is evaluated for a proposed **{money(facility.amount)} {facility.kind}** facility over
**{facility.years} years**, with {facility.payments_per_year} payment periods per year.
Base nominal rate is {base['facility'].annual_rate:.2f}%; stressed rate is {facility.annual_rate:.2f}%.
Base DSCR is **{multiple(base['dscr'])}**, stressed DSCR **{multiple(stressed['dscr'])}**,
and stressed risk rating **{stressed['risk']['grade']} / 5 — {stressed['risk']['label']}**.

## Financial Performance Analysis
Year 1 revenue of {money(first_revenue)} compares with Year 3 revenue of {money(values['Revenue'])}.
{growth} Historical EBITDA is {money(history['ebitda'])} at a {history['ratios']['EBITDA Margin']:.1%} margin.
Current and quick ratios are {multiple(history['ratios']['Current Ratio'])} and {multiple(history['ratios']['Quick Ratio'])}.
Funded debt is {money(stressed['debt'])}; base/stressed Debt/EBITDA is
{multiple(base['leverage'])} / {multiple(stressed['leverage'])}.

## Stress Test Vulnerabilities
The custom scenario applies a {haircut}% revenue haircut, {compression} bps margin compression,
and +{shock} bps rate shock. EBITDA becomes {money(stressed['ebitda'])}; annual combined debt service
is {money(stressed['service'])}. **{vulnerability}**

Estimated residual cash after capex, scheduled principal and dividends is **{money(stressed['residual_cash'])}**.
This cash proxy holds existing taxes and working-capital movements fixed. Other investing/financing
outflows reduce available cash; positive non-operating inflows are not assumed to recur. {maturity}
{stressed['risk']['reason']}

## Credit Committee Recommendation
**{decision}**

Maximum new facility capacity under 1.25x DSCR / 4.50x leverage is **{money(capacity['maximum'])}**,
including the current proposal. Additional headroom is **{money(capacity['headroom'])}**;
required proposal reduction is **{money(capacity['reduction'])}**. {capacity['reason']}

Require reporting, verified collateral/lien priority, distribution and additional-debt controls,
and documented repayment sources. I/O relief may increase later payments; a sweep requires genuine
surplus cash. For revolvers, establish a borrowing base, draw controls and a credible maturity takeout.

*Deterministic expert-system output; no LLM or API calls. Thresholds are project policies, not universal
bank standards. EBITDA coverage excludes capex, tax and working-capital needs; the model does not
verify collateral, legal enforceability or management quality.*
"""


CHALLENGES = {"A": "What if revenue drops by 25%? Does the deal survive?",
              "B": "How much additional debt can this capital structure support?",
              "C": "Which structural enhancements mitigate our exposure?"}


def analyst_answer(challenge, frame, facility, stressed, capacity, compression, shock):
    if challenge == "A":
        case = evaluate_scenario(frame, facility, 25, compression, shock)
        covers = math.isfinite(case["dscr"]) and case["dscr"] >= 1
        passes = covers and case["dscr"] >= 1.25 and case["leverage"] <= 4.5
        return (f"**{'Passes quantitative screening' if passes else 'Fails quantitative screening'}. "
                f"At a 25% revenue decline, EBITDA is {money(case['ebitda'])}, DSCR {multiple(case['dscr'])}, "
                f"and leverage {multiple(case['leverage'])}. Scheduled service is {'covered' if covers else 'not demonstrated as covered'}. "
                f"This replaces the haircut with 25%, retaining {compression} bps margin compression and +{shock} bps rate shock. "
                "Screening requires ≥1.25x DSCR and ≤4.50x leverage. Costs scale at the selected margin; fixed-cost stress could be worse. "
                "For a revolver, survival excludes repayment of the maturity draw.")
    if challenge == "B":
        return (f"**Maximum new facility: {money(capacity['maximum'])}.** Additional headroom beyond the current "
                f"proposal: **{money(capacity['headroom'])}**; required reduction: **{money(capacity['reduction'])}**. "
                f"{capacity['reason']} The solver checks every annual cent-rounded schedule at the custom stressed rate "
                "and earnings, retaining facility type, utilization, fee and original I/O months. "
                "Revolver capacity is commitment capacity, while leverage uses only average drawn debt. "
                "Zero utilization and zero fee leave commitment unconstrained by these two ratios; this is not an unlimited lending recommendation.")
    if challenge != "C":
        raise ValueError("Choose challenge A, B or C.")
    if facility.kind == "Revolver":
        grace = "A principal grace period adds no relief: the revolver already has zero scheduled principal. Require borrowing-base controls, clean-down terms and a verified maturity repayment source."
    else:
        interval = 12 // facility.payments_per_year
        io_months = max(12, facility.io_months if facility.kind == FACILITY_TYPES[1] else 0)
        if io_months >= facility.years * 12:
            grace = "A full-year I/O period cannot fit the existing maturity while preserving amortization; it would require a balloon or term extension."
        else:
            adjusted = replace(facility, kind=FACILITY_TYPES[1], io_months=(io_months // interval) * interval)
            case = evaluate_scenario(frame, adjusted, stressed["haircut"], compression, shock)
            history = financials(frame, "Year 3")
            peak = history["service"] + case["annual"]["Debt service"].max()
            grace = (f"Using {io_months} I/O months at unchanged maturity gives first-year DSCR "
                     f"{multiple(case['dscr'])}, but worst annual scheduled DSCR is {multiple(ratio(case['ebitda'], peak))} "
                     "at constant stressed EBITDA. Remaining principal amortizes over the shorter remaining term.")
    first_balance = stressed["annual"].iloc[0]["Closing balance"]
    sweep = min(first_balance, max(0, stressed["residual_cash"]) * 0.5)
    return (f"**Principal structure.** {grace}\n\n**Cash sweep.** A separate 50% sweep of positive estimated residual "
            f"cash produces {money(sweep)} of year-end prepayment, leaving {money(first_balance - sweep)} of the "
            "new facility drawn. The proxy starts with historical CFO, adjusts for the EBITDA change, subtracts "
            "new interest/fees, capex, all scheduled principal, dividends and other investing/financing outflows, and holds existing taxes/working "
            "capital fixed. No additional borrowings fund the sweep. It does not improve first-year scheduled DSCR; "
            "future revolver redraws could reverse the reduction.\n\n**Controls.** Use reporting and distribution "
            "restrictions, 1.25x coverage and 4.50x leverage tests, verified security and draw controls. "
            "Grace and sweep are separate illustrations, not a combined forecast.")


def terminal_figure(title):
    """Explicit shared layout; theme=None prevents Streamlit from replacing the palette."""
    figure = go.Figure()
    figure.update_layout(template="plotly_dark", title=title, height=350,
        paper_bgcolor=PALETTE["background"], plot_bgcolor=PALETTE["background"],
        font=dict(family="Arial, sans-serif", size=12, color=PALETTE["text"]),
        margin=dict(l=35, r=20, t=50, b=35), hovermode="x unified",
        legend=dict(orientation="h", y=1.15, x=0),
        xaxis=dict(gridcolor=PALETTE["grid"], zerolinecolor=PALETTE["grid"]),
        yaxis=dict(gridcolor=PALETTE["grid"], zerolinecolor=PALETTE["grid"]))
    return figure


def render_risk_matrix():
    with st.expander("Transparent risk matrix and boundary conventions"):
        st.dataframe(pd.DataFrame([
            [1, "Low", ">2.00", "<2.50"], [2, "Moderate", "1.50–2.00 inclusive", "2.50–3.50 inclusive"],
            [3, "Acceptable", "1.25 ≤ DSCR <1.50", "3.50 < leverage ≤4.50"],
            [4, "Watch", "1.00 ≤ DSCR <1.25", "4.50 < leverage ≤5.50"],
            [5, "High Risk", "<1.00", ">5.50"]], columns=["Grade", "Rating", "DSCR (x)", "Debt/EBITDA (x)"]), hide_index=True)
        st.write("Grade each factor and take the worse grade. Nonpositive EBITDA or undefined ratios receive grade 5. These are illustrative project policies; grades use full precision.")


def main():
    st.set_page_config(page_title="UnderwriteIQ", page_icon="🏦", layout="wide")
    st.markdown("""<style>
    .stApp {background-color:#0e1117;color:#e6edf3;}
    [data-testid="stSidebar"] {background-color:#11141a;}
    [data-testid="stMetric"] {border:1px solid #262730;border-radius:6px;padding:12px;}
    </style>""", unsafe_allow_html=True)
    st.title("UnderwriteIQ")
    st.caption("COMMERCIAL CREDIT INTELLIGENCE | CUSTOM FINANCIAL ENGINE | ZERO API COSTS")
    with st.sidebar:
        st.header("Facility structure")
        kind = st.selectbox("Facility Type", FACILITY_TYPES, key="facility_type")
        amount = st.number_input("Principal / commitment (USD)", 0.0, 1e12, 8_000_000.0, 100_000.0, key="amount")
        rate = st.number_input("Annual nominal interest rate (%)", 0.0, 100.0, 7.5, 0.25, key="rate")
        years = st.number_input("Term (years)", 1, 40, 10, key="years")
        frequency = st.selectbox("Payment frequency", ["Monthly", "Quarterly"], key="frequency")
        per_year = 12 if frequency == "Monthly" else 4
        interval = 12 // per_year
        io = 0
        utilization, fee = 60.0, 0.50
        if kind == FACILITY_TYPES[1]:
            io = st.number_input("I/O Period (Months)", 0, years * 12 - interval,
                                 min(12, years * 12 - interval), interval)
        elif kind == "Revolver":
            utilization = st.slider("Average Draw Utilization %", 0, 100, 60)
            fee = st.number_input("Unused Commitment Fee %", 0.0, 10.0, 0.50, 0.05)
        facility = Facility(amount, rate, years, per_year, kind, io, utilization, fee)
        st.divider()
        st.header("Custom stress")
        haircut = st.slider("Revenue haircut (%)", 0, 50, 0, key="haircut")
        compression = st.slider("EBITDA margin compression (bps)", 0, 3000, 0, 25, key="compression")
        shock = st.select_slider("Loan rate shock (bps)", options=[0, 100, 200, 300, 400, 500], value=0, key="shock")
        st.caption("100 bps = 1 percentage point. New facility reprices at inception; existing debt stays fixed.")
    edited = render_financial_input()
    try:
        frame = validate_financial_data(edited)
        base = evaluate_scenario(frame, facility)
        stressed = evaluate_scenario(frame, facility, haircut, compression, shock)
        capacity = debt_capacity(frame, stressed)
    except (ValueError, ArithmeticError) as error:
        st.error(str(error))
        st.stop()
    issues = consistency_issues(frame)
    if issues:
        with st.expander("Input reconciliation alerts", expanded=True):
            for issue in issues:
                st.warning(issue)
    history_tab, facility_tab, stress_tab, memo_tab, analyst_tab = st.tabs(
        ["Financial analysis", "Facility & amortization", "Stress & risk", "Credit memorandum", "Challenge the Analyst"])
    with history_tab:
        rows = []
        historical = {year: financials(frame, year) for year in YEARS}
        for name in historical["Year 3"]["ratios"]:
            row = {"Metric": name}
            for year in YEARS:
                value = historical[year]["ratios"][name]
                row[year] = f"{value:.1%}" if "Margin" in name and math.isfinite(value) else multiple(value)
            row["Year 3 + facility"] = multiple(base["dscr"] if name == "DSCR" else base["leverage"]) if name in ("DSCR", "Total Debt / EBITDA") else "—"
            rows.append(row)
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.download_button("Download financial ratios", pd.DataFrame(rows).to_csv(index=False), "creditscope_ratios.csv", "text/csv")
        st.caption("Pro forma leverage uses existing interest-bearing debt plus new drawn exposure. Other balance sheet ratios are historical. N/M denotes a nonpositive denominator.")
    with facility_tab:
        annual = base["annual"]
        columns = st.columns(3)
        columns[0].metric("First-year facility service", money(annual.iloc[0]["Debt service"]))
        columns[1].metric("Pro forma DSCR", multiple(base["dscr"]))
        columns[2].metric("Pro forma Debt / EBITDA", multiple(base["leverage"]))
        if kind == "Revolver":
            st.warning(f"Average drawn exposure remains {money(base['schedule'].iloc[0]['Opening balance'])}. Service includes interest and undrawn fees only. Maturity principal repayment/refinancing is excluded.")
        else:
            st.caption("Fixed-rate, end-of-period payments; final principal adjusts for rounding. I/O preserves original maturity. Quarterly I/O periods must align to three-month intervals.")
        figure = terminal_figure("Annual facility cash flows")
        for label, color in (("Principal", "green"), ("Interest", "blue"), ("Commitment fee", "stress")):
            figure.add_trace(go.Scatter(x=annual["Loan year"], y=annual[label], name=label, mode="lines+markers", line=dict(color=PALETTE[color], width=2)))
        figure.update_xaxes(title="Loan year", dtick=1)
        figure.update_yaxes(title="USD", tickprefix="$")
        st.plotly_chart(figure, theme=None, use_container_width=True)
        balance_figure = terminal_figure("Drawn balance / amortization profile")
        balance_figure.add_trace(go.Scatter(x=base["schedule"]["Period"],
            y=base["schedule"]["Closing balance"], name="Closing drawn balance", mode="lines",
            line=dict(color=PALETTE["green"], width=2)))
        balance_figure.update_xaxes(title="Payment period")
        balance_figure.update_yaxes(title="USD", tickprefix="$")
        st.plotly_chart(balance_figure, theme=None, use_container_width=True)
        st.dataframe(annual, hide_index=True, width="stretch")
        with st.expander("Full periodic schedule"):
            st.dataframe(base["schedule"], hide_index=True, width="stretch")
        st.download_button("Download facility schedule", base["schedule"].to_csv(index=False), "creditscope_facility.csv", "text/csv")
    with stress_tab:
        columns = st.columns(4)
        columns[0].metric("Stressed EBITDA", money(stressed["ebitda"]))
        columns[1].metric("Stressed DSCR", multiple(stressed["dscr"]))
        columns[2].metric("Stressed Debt / EBITDA", multiple(stressed["leverage"]))
        columns[3].metric("Risk rating", f"{stressed['risk']['grade']} / 5 · {stressed['risk']['label']}")
        st.write(stressed["risk"]["reason"])
        st.dataframe(pd.DataFrame({"Base Case": base["income"], "Custom Stressed Case": stressed["income"]}).style.format("${:,.0f}"), width="stretch")
        history = financials(frame, "Year 3")
        figure = terminal_figure("DSCR by loan year · constant Year 3 earnings")
        for name, scenario, multiplier, color in (("Base Case", base, 1, "green"),
                ("-10% Cash Flow", base, .9, "blue"), ("-20% Cash Flow", base, .8, "stress"),
                ("Custom Stressed Case", stressed, 1, "stress")):
            coverage = [ratio(scenario["ebitda"] * multiplier, history["service"] + service) for service in scenario["annual"]["Debt service"]]
            figure.add_trace(go.Scatter(x=scenario["annual"]["Loan year"], y=coverage, name=name, mode="lines+markers",
                line=dict(color=PALETTE[color], width=2, dash="dash" if name == "-20% Cash Flow" else "solid")))
        figure.add_hline(y=1.25, line_dash="dot", line_color=PALETTE["text"], annotation_text="1.25x policy floor")
        figure.update_xaxes(title="Loan year", dtick=1)
        figure.update_yaxes(title="DSCR (x)")
        st.plotly_chart(figure, theme=None, use_container_width=True)
        st.caption("Cash Flow presets reduce EBITDA only; future curves hold existing debt service and Year 3 earnings fixed. I/O step-ups are retained. Revolver maturity takeout is excluded. These are sensitivities, not operating forecasts.")
        render_risk_matrix()
        with st.expander("Stress and cash-flow assumptions"):
            st.write("Revenue × (1 − haircut); EBITDA = stressed revenue × (historical margin − bps/10,000). Reported COGS scales with revenue; reported operating expenses are the residual required by the selected margin. Total operating D&A is added back once for EBITDA and deducted once to calculate EBIT. D&A and other non-operating income stay fixed. Taxes use a historical effective rate bounded to 0–100%, with no loss benefit. DSCR includes principal, interest and revolver commitment fees. Residual cash adjusts historical CFO for EBITDA changes and incremental interest/fees, less capex, principal and dividends; existing taxes and working capital stay fixed.")
    with memo_tab:
        memo = credit_memo(frame, base, stressed, capacity, haircut, compression, shock)
        st.markdown(memo)
        st.download_button("Download credit memorandum", memo, "creditscope_credit_memo.md", "text/markdown")
    with analyst_tab:
        challenge = st.selectbox("Credit Committee challenge", list(CHALLENGES), format_func=lambda key: f"{key}: {CHALLENGES[key]}", key="challenge")
        st.markdown(analyst_answer(challenge, frame, facility, stressed, capacity, compression, shock))
        st.caption("Rule-based answers recalculate with every input change. No LLM or external service is called.")


if __name__ == "__main__":
    main()
