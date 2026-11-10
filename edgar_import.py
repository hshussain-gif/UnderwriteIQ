"""Local US-GAAP Inline XBRL extraction. No HTTP, model, or external services.

Deliberately conservative: use standard consolidated USD facts; unsupported custom
concepts stay available in the fact browser but never silently populate the model.
Missing and conflicting inputs remain NaN until explicitly reviewed by the user.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import math
import re

import pandas as pd
from lxml import etree

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
ANNUAL_DAYS = (350, 380)

# Alternatives are ordered semantic preferences, never summed indiscriminately.
ALIASES = {
    "Revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"),
    "Cost of goods sold": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "Depreciation and amortization": ("DepreciationDepletionAndAmortization",),
    "Interest expense": ("InterestExpenseNonOperating", "InterestAndDebtExpense", "InterestExpense"),
    "Income tax expense": ("IncomeTaxExpenseBenefit",),
    "Cash and equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
    "Short-term investments": ("ShortTermInvestments", "MarketableSecuritiesCurrent"),
    "Accounts receivable": ("AccountsReceivableNetCurrent", "AccountsNotesAndOtherReceivablesNetCurrent"),
    "Inventory": ("InventoryNet",),
    "Net property, plant and equipment": ("PropertyPlantAndEquipmentNet",),
    "Accounts payable and accruals": ("AccountsPayableAndAccruedLiabilitiesCurrent", "AccountsPayableCurrent"),
    "Short-term debt": ("ShortTermBorrowings", "ShortTermDebtCurrent"),
    "Current portion of long-term debt": ("LongTermDebtCurrent",),
    "Long-term debt (excluding current portion)": ("LongTermDebtNoncurrent",),
    "Shareholders' equity": ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "StockholdersEquity"),
    "Cash from operations (after interest and taxes)": ("NetCashProvidedByUsedInOperatingActivities",),
    "Capital expenditures": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "Existing debt proceeds": ("ProceedsFromIssuanceOfDebt", "ProceedsFromIssuanceOfLongTermDebt"),
    "Existing principal repayments": ("RepaymentsOfDebt", "RepaymentsOfLongTermDebt"),
    "Dividends": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    "Existing cash interest paid": ("InterestPaid", "InterestPaidNet"),
}
INSTANT_FIELDS = {
    "Cash and equivalents", "Short-term investments", "Accounts receivable", "Inventory",
    "Net property, plant and equipment", "Accounts payable and accruals", "Short-term debt",
    "Current portion of long-term debt", "Long-term debt (excluding current portion)", "Shareholders' equity",
}


@dataclass(frozen=True)
class Fact:
    concept: str
    namespace: str
    value: float
    entity: str
    start: str
    end: str
    source: str
    context: str
    fact_id: str
    label: str
    filing_end: str
    dimensional: bool

    @property
    def standard(self):
        return "fasb.org/us-gaap/" in self.namespace

    @property
    def local(self):
        return self.concept.split(":")[-1]


def local_name(node):
    return str(node.tag).split("}")[-1].split(":")[-1].lower()


def child_text(node, name):
    for child in node.iter():
        if local_name(child) == name:
            return "".join(child.itertext()).strip()
    return ""


def parse_number(node):
    """Apply Inline XBRL sign/scale once. Reject transformations not implemented."""
    if any(key.split(":")[-1].lower() == "nil" and value.lower() in ("true", "1")
           for key, value in node.attrib.items()):
        return None
    transformation = node.get("format", "").split(":")[-1].lower()
    supported = {"", "num-dot-decimal", "numdotdecimal", "numcommadot", "num-dot-decimal-in"}
    zero_formats = {"zerodash", "numdash", "fixed-zero"}
    if transformation not in supported | zero_formats:
        raise ValueError(f"Unsupported number transformation: {transformation}")
    # ix:exclude text must not be part of the numeric value.
    pieces = []
    def collect(element):
        if element.text:
            pieces.append(element.text)
        for child in element:
            if local_name(child) != "exclude":
                collect(child)
            if child.tail:
                pieces.append(child.tail)
    collect(node)
    raw = "".join(pieces).strip()
    if transformation in zero_formats:
        number = Decimal(0)
    else:
        raw = re.sub(r"[\s,\u00a0$]", "", raw)
        negative = raw.startswith("(") and raw.endswith(")")
        raw = raw.strip("()")
        number = Decimal(raw)
        if negative:
            number = -abs(number)
    # The explicit sign controls a negative fact, including parentheses renderings.
    if node.get("sign") == "-":
        number = -abs(number)
    scale = int(node.get("scale", "0"))
    if not -12 <= scale <= 12:
        raise ValueError("Unsupported scale outside -12 to 12.")
    value = float(number * (Decimal(10) ** scale))
    if not math.isfinite(value):
        raise ValueError("Nonfinite monetary value.")
    return value


def parse_filing(filename, data):
    if not filename.lower().endswith((".htm", ".html")):
        raise ValueError("Upload original EDGAR .htm or .html Inline XBRL files only.")
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("Each filing must be nonempty and no larger than 15 MB.")
    if data.lstrip().startswith(b"%PDF"):
        raise ValueError("A PDF renamed to HTML is not an Inline XBRL filing.")
    parser = etree.HTMLParser(no_network=True, remove_comments=True, huge_tree=False)
    try:
        root = etree.fromstring(data, parser)
    except (etree.ParserError, etree.XMLSyntaxError) as error:
        raise ValueError("The uploaded file could not be parsed as HTML.") from error
    if root is None:
        raise ValueError("The uploaded file is empty HTML.")
    namespaces, contexts, units, metadata = {}, {}, {}, {}
    nodes = list(root.iter())
    for node in nodes:
        for key, value in node.attrib.items():
            if key.startswith("xmlns:"):
                namespaces[key.split(":", 1)[1]] = value
        name = local_name(node)
        if name == "context":
            identifier = child_text(node, "identifier").lstrip("0") or "0"
            start, end = child_text(node, "startdate"), child_text(node, "enddate")
            instant = child_text(node, "instant")
            dimensional = any(local_name(child) in {"segment", "scenario", "explicitmember", "typedmember"}
                              for child in node.iter())
            contexts[node.get("id")] = (identifier, start, end or instant, dimensional)
        elif name == "unit":
            measures = ["".join(child.itertext()).strip().split(":")[-1]
                        for child in node.iter() if local_name(child) == "measure"]
            units[node.get("id")] = (measures == ["USD"])
        elif name == "nonnumeric":
            concept = node.get("name", "").split(":")[-1]
            if concept in {"DocumentType", "DocumentPeriodEndDate", "EntityRegistrantName", "EntityCentralIndexKey"}:
                metadata[concept] = "".join(node.itertext()).strip()
    if metadata.get("DocumentType", "").upper() not in {"10-K", "10-K/A"}:
        raise ValueError("File must contain 10-K/10-K/A Inline XBRL metadata; a viewer shell, 10-Q, or PDF export will not work.")
    filing_end = metadata.get("DocumentPeriodEndDate", "")
    for pattern in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            filing_end = datetime.strptime(filing_end, pattern).date().isoformat()
            break
        except ValueError:
            pass
    try:
        date.fromisoformat(filing_end)
    except ValueError as error:
        raise ValueError("Missing or invalid DocumentPeriodEndDate in the filing.") from error
    primary_entity = metadata.get("EntityCentralIndexKey", "").lstrip("0")
    if not primary_entity:
        entities = {item[0] for item in contexts.values()}
        if len(entities) != 1:
            raise ValueError("Cannot determine one reporting entity from this filing.")
        primary_entity = entities.pop()
    facts, warnings = [], []
    for node in nodes:
        if local_name(node) != "nonfraction" or node.get("contextref") not in contexts:
            continue
        entity, start, end, dimensional = contexts[node.get("contextref")]
        if entity != primary_entity or not units.get(node.get("unitref"), False):
            continue
        concept = node.get("name", "")
        namespace = namespaces.get(concept.split(":")[0], "")
        try:
            if not end:
                continue
            date.fromisoformat(end)
            if end > filing_end:
                continue
            if start:
                date.fromisoformat(start)
            value = parse_number(node)
        except (ValueError, InvalidOperation) as error:
            warnings.append(f"{filename}: skipped {concept}: {error}")
            continue
        if value is None:
            continue
        row = next((p for p in node.iterancestors() if local_name(p) == "tr"), node)
        label = re.sub(r"\s+", " ", " ".join(row.itertext())).strip()[:220]
        facts.append(Fact(concept, namespace, value, entity, start, end, filename,
                          node.get("contextref", ""), node.get("id", ""), label, filing_end, dimensional))
    if not any(f.standard and not f.dimensional for f in facts):
        raise ValueError("No consolidated USD US-GAAP monetary facts found. IFRS/non-USD filings are not supported by this importer.")
    return {"facts": facts, "warnings": warnings, "entity": primary_entity,
            "company": metadata.get("EntityRegistrantName", "Unspecified registrant"),
            "filing_end": filing_end, "source": filename}


def merge_filings(files):
    if not files or len(files) > 4:
        raise ValueError("Upload one to four 10-K HTML filings for the same company.")
    if sum(len(data) for _, data in files) > MAX_TOTAL_BYTES:
        raise ValueError("Combined uploads must be no larger than 40 MB.")
    filings = []
    for filename, data in files:
        try:
            filings.append(parse_filing(filename, data))
        except ValueError as error:
            raise ValueError(f"{filename}: {error}") from error
    if len({filing["entity"] for filing in filings}) != 1:
        raise ValueError("The files belong to different companies. Upload one company at a time.")
    facts = [fact for filing in filings for fact in filing["facts"]]
    revenue_tags = set(ALIASES["Revenue"])
    periods = sorted({(f.start, f.end) for f in facts if f.standard and not f.dimensional
                      and f.local in revenue_tags and f.start
                      and ANNUAL_DAYS[0] <= (date.fromisoformat(f.end) - date.fromisoformat(f.start)).days <= ANNUAL_DAYS[1]})
    by_end = {}
    for start, end in periods:
        by_end.setdefault(end, set()).add(start)
    if any(len(starts) > 1 for starts in by_end.values()):
        raise ValueError("Overlapping annual revenue contexts have different start dates. Review the filing periods manually.")
    periods = [(next(iter(starts)), end) for end, starts in sorted(by_end.items())][-3:]
    if len(periods) < 3:
        raise ValueError("Fewer than three annual revenue periods were found. Upload the previous 10-K as well (up to four filings).")
    if any(not 350 <= (date.fromisoformat(b[1]) - date.fromisoformat(a[1])).days <= 380
           for a, b in zip(periods, periods[1:])):
        raise ValueError("The three selected annual periods are not consecutive fiscal years.")
    return {"facts": facts, "periods": periods, "company": filings[0]["company"],
            "entity": filings[0]["entity"], "sources": [f["source"] for f in filings],
            "warnings": [warning for filing in filings for warning in filing["warnings"]]}


def select_fact(report, concepts, start, end, instant=False):
    """Prefer latest filing's comparable values; refuse conflicting duplicates."""
    for concept in concepts:
        matches = [f for f in report["facts"] if f.standard and not f.dimensional and f.local == concept
                   and f.end == end and ((not f.start) if instant else f.start == start)]
        if not matches:
            continue
        latest = max(f.filing_end for f in matches)
        matches = [f for f in matches if f.filing_end == latest]
        values = {f.value for f in matches}
        if len(values) > 1:
            return float("nan"), "Ambiguous", "; ".join(f"{f.source} [{f.context}] {f.concept}={f.value:,.2f}" for f in matches)
        fact = matches[0]
        scope_note = (" Scope review required: this tag covers long-term debt only; include any short-term/revolver cash flows manually."
                      if fact.local in {"ProceedsFromIssuanceOfLongTermDebt", "RepaymentsOfLongTermDebt"} else "")
        period_label = f"instant {end}" if instant else f"{start} → {end}"
        return fact.value, "Matched — review scope" if scope_note else "Matched", f"{fact.source} | {fact.concept} | {fact.context} | {fact.fact_id} | {period_label} | USD | {fact.label}{scope_note}"
    return float("nan"), "Missing", "No supported consolidated USD tag for this field/period. Review the fact browser or source filing; do not assume zero."


def build_import_matrix(report, schema):
    """Start completely blank. Never fill any imported cell from demo defaults.

    Reported COGS/operating expenses retain embedded operating D&A.
    Derived residuals preserve total current/noncurrent assets/liabilities and
    other investing/financing/FX cash flows instead of hiding them in a plug.
    """
    frame = schema.copy()
    years = [c for c in frame.columns if c.startswith("Year ")]
    frame[years] = float("nan")
    frame = frame.set_index("Line item", drop=False)
    audit = []
    for year, (start, end) in zip(years, report["periods"]):
        records = {}
        def read(concepts, instant=False, at=None):
            return select_fact(report, concepts, start, at or end, instant)
        def put(item, value, status, source):
            frame.at[item, year] = value
            records[item] = {"Year": year, "Period": f"{start} → {end}", "Line item": item,
                             "Imported value": value, "Status": status, "Source / derivation": source}
        def derive(item, value, components, note):
            if all(math.isfinite(v) for v in components):
                put(item, value, "Derived — review", note)
            else:
                put(item, float("nan"), "Missing", note + " One or more required components are missing/ambiguous.")
        for item, concepts in ALIASES.items():
            put(item, *read(concepts, item in INSTANT_FIELDS))
        def v(item):
            return float(frame.at[item, year])
        operating, _, op_source = read(("OperatingIncomeLoss",))
        expenses, _, expense_source = read(("OperatingExpenses",))
        if not math.isfinite(expenses) and all(math.isfinite(x) for x in (v("Revenue"), v("Cost of goods sold"), operating)):
            expenses = v("Revenue") - v("Cost of goods sold") - operating
            expense_source = "Revenue − reported COGS − operating income. " + op_source
        put("Operating expenses (reported)", expenses, "Matched / derived — review",
            "Reported operating expenses including embedded D&A, or revenue − reported COGS − operating income. " + expense_source)
        pretax, _, pretax_source = read(("IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"))
        derive("Other non-operating income / (expense)", pretax - operating + v("Interest expense"),
               [pretax, operating, v("Interest expense")],
               "Pretax income − operating income + book interest expense. " + pretax_source)
        ca, _, ca_source = read(("AssetsCurrent",), True)
        assets, _, asset_source = read(("Assets",), True)
        cl, _, cl_source = read(("LiabilitiesCurrent",), True)
        liabilities, _, liability_source = read(("Liabilities",), True)
        if not math.isfinite(liabilities) and all(math.isfinite(x) for x in (assets, v("Shareholders' equity"))):
            liabilities = assets - v("Shareholders' equity")
            liability_source = "Total assets − consolidated equity. " + asset_source
        current_components = [v(k) for k in ("Cash and equivalents", "Short-term investments", "Accounts receivable", "Inventory")]
        derive("Other current assets", ca - sum(current_components), [ca, *current_components],
               "Total current assets − cash − short-term investments − receivables − inventory. " + ca_source)
        derive("Other noncurrent assets", assets - ca - v("Net property, plant and equipment"),
               [assets, ca, v("Net property, plant and equipment")],
               "Total assets − current assets − net PP&E (includes goodwill/intangibles and other noncurrent assets). " + asset_source)
        current_debts = [v(k) for k in ("Accounts payable and accruals", "Short-term debt", "Current portion of long-term debt")]
        derive("Other current liabilities", cl - sum(current_debts), [cl, *current_debts],
               "Total current liabilities − mapped payables/accruals − short-term debt − current long-term debt. " + cl_source)
        derive("Other noncurrent liabilities", liabilities - cl - v("Long-term debt (excluding current portion)"),
               [liabilities, cl, v("Long-term debt (excluding current portion)")],
               "Total liabilities − current liabilities − noncurrent interest-bearing debt. " + liability_source)
        previous_date = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        put("Beginning cash", *read(("CashAndCashEquivalentsAtCarryingValue",), True, previous_date))
        investing, _, inv_source = read(("NetCashProvidedByUsedInInvestingActivities",))
        derive("Other investing cash flow", investing + v("Capital expenditures"),
               [investing, v("Capital expenditures")], "Net investing cash flow + positive capex. " + inv_source)
        financing, _, fin_source = read(("NetCashProvidedByUsedInFinancingActivities",))
        derive("Other financing cash flow", financing - v("Existing debt proceeds") + v("Existing principal repayments") + v("Dividends"),
               [financing, v("Existing debt proceeds"), v("Existing principal repayments"), v("Dividends")],
               "Net financing cash flow − mapped debt proceeds + mapped principal repayments + dividends. Includes buybacks/equity and unmapped financing flows; review debt completeness. " + fin_source)
        put("FX effect on cash", *read(("EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "EffectOfExchangeRateOnCashAndCashEquivalents")))
        for item in frame.index:
            if item not in records:
                put(item, float("nan"), "Missing", "Manual review required; no mapping defined.")
        audit.extend(records.values())
    return frame.reset_index(drop=True), pd.DataFrame(audit)


def fact_browser(report):
    return pd.DataFrame([{"Concept": f.concept, "Value (USD)": f.value,
        "Start": f.start, "End": f.end, "Scope": "Segment/dimension — excluded" if f.dimensional else "Consolidated",
        "Tag type": "US-GAAP" if f.standard else "Custom — manual mapping",
        "File": f.source, "Context": f.context, "Fact ID": f.fact_id, "Source label": f.label}
        for f in report["facts"]])
