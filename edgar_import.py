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
    "Depreciation and amortization": ("DepreciationDepletionAndAmortization", "OtherDepreciationAndAmortization"),
    "Interest expense": ("InterestExpenseNonOperating", "InterestAndDebtExpense", "InterestExpense"),
    "Income tax expense": ("IncomeTaxExpenseBenefit",),
    "Cash and equivalents": ("CashAndCashEquivalentsAtCarryingValue",),
    "Short-term investments": ("ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
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
    source_format: str = "Inline XBRL"

    @property
    def standard(self):
        return "fasb.org/us-gaap/" in self.namespace or self.namespace == "rendered-us-gaap-concept"

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


def rendered_date(text):
    """Dates on concept-labelled investor-relations reports, never filename guesses."""
    text = re.sub(r"\s+", " ", text.replace(".", "")).strip()
    for pattern in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized financial column date: {text}")


def parse_rendered_filing(filename, root):
    """Narrow adapter for concept-labelled 'table.report' EDGAR/IR exports.

    These exports lack ix:nonFraction and contextRef. Only the consolidated
    primary statements with explicit USD units, scales, and annual/date column
    headers are eligible. Footnote/segment/parenthetical tables are excluded.
    Provenance explicitly identifies inferred annual starts and rendered sources.
    """
    tables = root.xpath('//table[contains(concat(" ", normalize-space(@class), " "), " report ")]')
    if not tables:
        raise ValueError("File has neither original Inline XBRL tags nor supported concept-labelled annual financial tables. Download the original SEC HTML rather than a viewer/search page.")
    metadata = {}
    def report_rows(table):
        return table.xpath('./tr|./thead/tr|./tbody/tr|./tfoot/tr')
    for table in tables:
        for row in report_rows(table):
            anchors = row.xpath('./td//a[@onclick]')
            cells = row.xpath('./td')
            if not anchors or len(cells) < 2:
                continue
            match = re.search(r"['\"]defref_dei_([A-Za-z0-9]+)['\"]", anchors[0].get('onclick', ''))
            if match and match.group(1) in {'DocumentType', 'DocumentPeriodEndDate', 'EntityRegistrantName', 'EntityCentralIndexKey'}:
                metadata[match.group(1)] = ' '.join(cells[1].itertext()).strip()
    if metadata.get('DocumentType', '').upper() not in {'10-K', '10-K/A'}:
        raise ValueError("Rendered report does not identify itself as a 10-K/10-K/A. Quarterly reports and saved viewer shells are not supported.")
    try:
        filing_end = rendered_date(metadata.get('DocumentPeriodEndDate', ''))
    except ValueError as error:
        raise ValueError("Rendered report has no readable annual period-end date.") from error
    entity = metadata.get('EntityCentralIndexKey', '').strip().lstrip('0')
    if not entity or not entity.isdigit():
        raise ValueError("Rendered report is missing a usable CIK; cannot identify the reporting company safely.")
    eligible, annual_ends, warnings = [], set(), []
    for table_index, table in enumerate(tables):
        rows = report_rows(table)
        first = [cell for row in rows for cell in row.xpath('./th')]
        if not first:
            continue
        title = re.sub(r'\s+', ' ', ' '.join(first[0].itertext())).strip()
        upper = title.upper()
        balance = upper.startswith('CONSOLIDATED BALANCE SHEET')
        duration = bool(re.match(r'^CONSOLIDATED STATEMENTS? OF (COMPREHENSIVE INCOME|INCOME|OPERATIONS|CASH FLOWS?)\b', upper))
        if not (balance or duration) or 'PARENTHETICAL' in upper:
            continue
        if not re.search(r'\bUSD\b', title):
            warnings.append(f'{filename}: skipped non-USD statement {title}')
            continue
        scale_match = re.search(r'\$\s+in\s+(Thousands|Millions|Billions)\b', title, re.I)
        scale = {'thousands': 1000, 'millions': 1000000, 'billions': 1000000000}[scale_match.group(1).lower()] if scale_match else 1
        if duration and '12 Months Ended' not in ' '.join(' '.join(cell.itertext()) for row in rows[:2] for cell in row.xpath('./th')):
            warnings.append(f'{filename}: skipped statement without explicit 12-month columns: {title}')
            continue
        headers = None
        for row in rows[:3]:
            candidates = []
            for cell in row.xpath('./th'):
                try:
                    candidates.append(rendered_date(' '.join(cell.itertext())))
                except ValueError:
                    pass
            if candidates:
                headers = candidates
                break
        if not headers or len(set(headers)) != len(headers) or any(end > filing_end for end in headers):
            warnings.append(f'{filename}: skipped ambiguous financial date headers: {title}')
            continue
        if duration:
            annual_ends.update(headers)
        eligible.append((table_index, table, title, balance, headers, scale))
    facts = []
    for table_index, table, title, balance, headers, scale in eligible:
        dimension_scope = ''
        for row_index, row in enumerate(report_rows(table)):
            anchors = row.xpath('./td//a[@onclick]')
            if not anchors:
                continue
            onclick = anchors[0].get('onclick', '')
            # Renderer exports put dimensional sections inside primary statements.
            # Once a member section begins, conservatively exclude the remaining
            # section rows from consolidated mapping rather than treating totals
            # for subscription/services segments as company-wide revenue.
            if re.search(r'defref_[^\'\"]*Axis=', onclick):
                dimension_scope = re.sub(r'\s+', ' ', ' '.join(anchors[0].itertext())).strip()
                continue
            concept_match = re.search(r"['\"]defref_([A-Za-z0-9-]+)_([A-Za-z0-9]+)['\"]", onclick)
            if not concept_match:
                continue
            prefix, local = concept_match.groups()
            if (local.endswith(('Abstract', 'Axis', 'Member', 'Domain'))
                    or re.search(r'PerShare|NumberOf.*Shares|SharesOutstanding|StockShares|Percentage|Percent|Ratio', local)):
                continue
            cells = row.xpath('./td|./th')[-len(headers):]
            if len(cells) != len(headers):
                continue
            for column, (cell, end) in enumerate(zip(cells, headers)):
                if 'num' not in cell.get('class', ''):
                    continue
                # Numeric renderer classes plus explicit table units supply value semantics.
                text = re.sub(r'\s+', '', ''.join(cell.itertext())).replace('$', '').replace(',', '').replace('\u2212', '-')
                if not text:
                    continue
                if text in {'-', '\u2014', '\u2013'}:
                    value = 0.0
                else:
                    negative = text.startswith('(') and text.endswith(')')
                    try:
                        value = float(Decimal(text.strip('()')) * scale)
                    except InvalidOperation:
                        warnings.append(f'{filename}: skipped unrecognized numeric cell {local} {text[:30]}')
                        continue
                    if negative:
                        value = -abs(value)
                if not math.isfinite(value):
                    continue
                if balance:
                    start = ''
                else:
                    previous = [d for d in annual_ends if 350 <= (end - d).days <= 380]
                    if previous:
                        start = (max(previous) + timedelta(days=1)).isoformat()
                    else:
                        # Calendar-year anniversary is an explicit approximation only for
                        # the earliest 12-month column; reviewers see this in provenance.
                        previous_day = min(end.day, 28) if end.month == 2 else end.day
                        start = (end.replace(year=end.year - 1, day=previous_day) + timedelta(days=1)).isoformat()
                context = f'rendered-table-{table_index}-column-{column}'
                label = re.sub(r'\s+', ' ', ' '.join(anchors[0].itertext())).strip()
                evidence = f'{label} | {title} | displayed USD x {scale}'
                if not balance and local.startswith(('Payments', 'Repayments')) and value < 0:
                    value = abs(value)
                    evidence += ' | displayed cash outflow normalized to positive payment amount'
                if dimension_scope:
                    evidence += f' | segment/member: {dimension_scope}'
                if not balance:
                    evidence += ' | 12 months ended; start inferred from prior year-end/calendar anniversary'
                facts.append(Fact(f'{prefix}:{local}', 'rendered-us-gaap-concept' if prefix == 'us-gaap' else f'rendered-custom:{prefix}',
                    value, entity, start, end.isoformat(), filename, context, f'table-{table_index}-row-{row_index}-column-{column}',
                    evidence, filing_end.isoformat(), bool(dimension_scope), 'Rendered concept-labelled financial tables'))
    if not facts:
        raise ValueError('No usable consolidated annual USD financial tables were found in the rendered HTML export.')
    warnings.insert(0, f'{filename}: rendered financial-table export detected, not original Inline XBRL. Primary statements only; annual starts are inferred and footnote facts may require manual review.')
    return {'facts': facts, 'warnings': warnings, 'entity': entity,
            'company': metadata.get('EntityRegistrantName', 'Unspecified registrant'),
            'filing_end': filing_end.isoformat(), 'source': filename,
            'source_format': 'Rendered concept-labelled financial tables'}


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
    # Saved browser pages can be UTF-8 without a charset declaration. lxml's
    # legacy default would otherwise corrupt em dashes and nonbreaking spaces.
    declared_encoding = re.search(br'(?:charset\s*=\s*[\"\x27]?|encoding\s*=\s*[\"\x27])([A-Za-z0-9_-]+)', data[:4096], re.I)
    encoding = None
    if not declared_encoding:
        try:
            data.decode('utf-8')
            encoding = 'utf-8'
        except UnicodeDecodeError:
            encoding = 'windows-1252'
    parser = etree.HTMLParser(no_network=True, remove_comments=True, huge_tree=False, encoding=encoding)
    try:
        root = etree.fromstring(data, parser)
    except (etree.ParserError, etree.XMLSyntaxError) as error:
        raise ValueError("The uploaded file could not be parsed as HTML.") from error
    if root is None:
        raise ValueError("The uploaded file is empty HTML.")
    if not any(local_name(node) == 'nonnumeric' for node in root.iter()):
        return parse_rendered_filing(filename, root)
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
            "filing_end": filing_end, "source": filename, "source_format": "Inline XBRL"}


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
    latest_filing = max(filings, key=lambda filing: filing['filing_end'])
    return {"facts": facts, "periods": periods, "company": latest_filing["company"],
            "entity": filings[0]["entity"], "sources": [f["source"] for f in filings],
            "source_formats": sorted({f.get('source_format', 'Inline XBRL') for f in filings}),
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
        return fact.value, "Matched — review scope" if scope_note else "Matched", f"{fact.source_format} | {fact.source} | {fact.concept} | {fact.context} | {fact.fact_id} | {period_label} | USD | {fact.label}{scope_note}"
    return float("nan"), "Missing", "No supported consolidated USD tag for this field/period. Review the fact browser or source filing; do not assume zero."


def build_import_matrix(report, schema, overrides=None):
    """Start completely blank. Never fill any imported cell from demo defaults.

    Reported COGS/operating expenses retain embedded operating D&A.
    Derived residuals preserve total current/noncurrent assets/liabilities and
    other investing/financing/FX cash flows instead of hiding them in a plug.
    """
    overrides = overrides or {}
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
            reviewed = overrides.get((item, year), value)
            frame.at[item, year] = reviewed
            if (item, year) in overrides:
                status = "User supplied / overridden — verify source"
            records[item] = {"Year": year, "Period": f"{start} → {end}", "Line item": item,
                             "Imported value": value, "Reviewed value": reviewed,
                             "Status": status, "Source / derivation": source}
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
        "File": f.source, "Source format": f.source_format, "Context": f.context, "Fact ID": f.fact_id, "Source label": f.label}
        for f in report["facts"]])
