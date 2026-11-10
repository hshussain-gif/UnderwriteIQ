"""Synthetic Inline XBRL documents exercise the local importer without a network."""
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import math
import unittest
from unittest.mock import patch

import pandas as pd

from app import (load_baseline_data, validate_financial_data, financials, consistency_issues,
                 source_reconciliations, Facility, evaluate_scenario)
from edgar_import import (ALIASES, parse_filing, merge_filings, build_import_matrix,
                          select_fact, parse_number)


def fixture(company='Test Manufacturing', cik='123456', omit=None, extra=''):
    baseline = load_baseline_data().set_index('Line item')
    parts = ['''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
        xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:us-gaap="http://fasb.org/us-gaap/2025"
        xmlns:dei="http://xbrl.sec.gov/dei/2025" xmlns:custom="http://test.example/2025"
        xmlns:iso4217="http://www.xbrl.org/2003/iso4217"><body>''',
        f'<ix:nonNumeric name="dei:DocumentType">10-K</ix:nonNumeric>',
        f'<ix:nonNumeric name="dei:DocumentPeriodEndDate">2025-12-31</ix:nonNumeric>',
        f'<ix:nonNumeric name="dei:EntityRegistrantName">{company}</ix:nonNumeric>',
        f'<ix:nonNumeric name="dei:EntityCentralIndexKey">{cik}</ix:nonNumeric>',
        '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>']
    def context(identifier, start, end):
        period = (f'<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>'
                  if start else f'<xbrli:instant>{end}</xbrli:instant>')
        parts.append(f'<xbrli:context id="{identifier}"><xbrli:entity><xbrli:identifier>{cik}</xbrli:identifier></xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>')
    def fact(concept, value, context):
        if concept == omit:
            return
        sign = ' sign="-"' if value < 0 else ''
        parts.append(f'<table><tr><td>{concept}</td><td><ix:nonFraction id="{concept}-{context}" name="us-gaap:{concept}" contextRef="{context}" unitRef="usd" scale="6"{sign}>{abs(value) / 1e6:.8f}</ix:nonFraction></td></tr></table>')
    context('i2022', '', '2022-12-31')
    fact('CashAndCashEquivalentsAtCarryingValue', 1_500_000, 'i2022')
    for index, year in enumerate(('Year 1', 'Year 2', 'Year 3')):
        y = 2023 + index
        context(f'd{y}', f'{y}-01-01', f'{y}-12-31')
        context(f'i{y}', '', f'{y}-12-31')
        history = financials(load_baseline_data(), year)
        values = baseline[year].to_dict()
        for item, tags in ALIASES.items():
            instant = item in __import__('edgar_import').INSTANT_FIELDS
            fact(tags[0], values[item], f'{"i" if instant else "d"}{y}')
        operating = history['ebitda'] - values['Depreciation and amortization']
        extras = {
            'OperatingIncomeLoss': operating,
            'OperatingExpenses': values['Operating expenses (reported)'],
            'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest': history['pretax'],
            'ProfitLoss': history['pretax'] - values['Income tax expense'],
            'NetCashProvidedByUsedInInvestingActivities': -values['Capital expenditures'],
            'NetCashProvidedByUsedInFinancingActivities': values['Existing debt proceeds'] - values['Existing principal repayments'] - values['Dividends'],
            'EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents': 0,
        }
        ca = sum(values[k] for k in ('Cash and equivalents', 'Short-term investments', 'Accounts receivable', 'Inventory', 'Other current assets'))
        cl = sum(values[k] for k in ('Accounts payable and accruals', 'Short-term debt', 'Current portion of long-term debt', 'Other current liabilities'))
        instants = {'AssetsCurrent': ca, 'Assets': ca + values['Net property, plant and equipment'],
                    'LiabilitiesCurrent': cl, 'Liabilities': cl + values['Long-term debt (excluding current portion)'] + values['Other noncurrent liabilities']}
        for tag, value in extras.items():
            fact(tag, value, f'd{y}')
        for tag, value in instants.items():
            fact(tag, value, f'i{y}')
    parts.extend([extra, '</body></html>'])
    return ''.join(parts).encode()


class EdgarImportTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        self.report = merge_filings([('manufacturing-2025.htm', self.data)])

    def test_scaled_values_and_complete_pipeline(self):
        frame, audit = build_import_matrix(self.report, load_baseline_data())
        reviewed = validate_financial_data(frame)
        self.assertEqual(reviewed.set_index('Line item').at['Revenue', 'Year 3'], 50_000_000)
        self.assertEqual(consistency_issues(reviewed), [])
        self.assertTrue((source_reconciliations(self.report, reviewed)['Difference USD'] == 0).all())
        result = evaluate_scenario(reviewed, Facility())
        self.assertEqual(result['ebitda'], 9_000_000)
        self.assertIn('manufacturing-2025.htm', audit.iloc[0]['Source / derivation'])
        self.assertAlmostEqual(result['dscr'], 2.5427054155)

    def test_missing_values_never_use_sample_defaults(self):
        report = merge_filings([('missing.htm', fixture(omit='InterestExpenseNonOperating'))])
        frame, audit = build_import_matrix(report, load_baseline_data())
        items = frame.set_index('Line item')
        self.assertTrue(math.isnan(items.at['Interest expense', 'Year 3']))
        self.assertEqual(items.at['Operating expenses (reported)', 'Year 3'], 10_200_000)
        with self.assertRaises(ValueError):
            validate_financial_data(frame)

    def test_conflicts_dimensions_and_custom_tags(self):
        extra = '''<ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" contextRef="d2025" unitRef="usd" scale="6">99</ix:nonFraction>
        <ix:nonFraction name="custom:UsefulBorrowings" contextRef="d2025" unitRef="usd">100</ix:nonFraction>'''
        report = merge_filings([('conflict.htm', fixture(extra=extra))])
        value, status, source = select_fact(report, ALIASES['Revenue'], '2025-01-01', '2025-12-31')
        self.assertTrue(math.isnan(value))
        self.assertEqual(status, 'Ambiguous')
        fact = self.report['facts'][0]
        altered = dict(self.report, facts=[replace(fact, dimensional=True)])
        self.assertTrue(math.isnan(select_fact(altered, (fact.local,), fact.start, fact.end, not bool(fact.start))[0]))

    def test_invalid_file_forms_entities_and_periods(self):
        for name, data in [('file.pdf', self.data), ('fake.htm', b'%PDF-1.7'), ('viewer.html', b'<html><body>viewer</body></html>')]:
            with self.assertRaises(ValueError):
                parse_filing(name, data)
        with self.assertRaises(ValueError):
            merge_filings([('one.htm', self.data), ('two.htm', fixture(cik='999999'))])
        quarter = self.data.replace(b'>10-K<', b'>10-Q<')
        with self.assertRaises(ValueError):
            parse_filing('quarter.htm', quarter)
        with self.assertRaises(ValueError):
            merge_filings([('too-few.htm', self.data.replace(b'RevenueFromContractWithCustomerExcludingAssessedTax', b'UnknownRevenue'))])

    def test_newer_comparatives_are_preferred(self):
        old = self.data.replace(b'>2025-12-31</ix:nonNumeric>', b'>2024-12-31</ix:nonNumeric>')
        old = old.replace(b'>45.00000000</ix:nonFraction>', b'>44.00000000</ix:nonFraction>')
        report = merge_filings([('old.htm', old), ('new.htm', self.data)])
        self.assertEqual(select_fact(report, ALIASES['Revenue'], '2024-01-01', '2024-12-31')[0], 45_000_000)

    def test_transformations_nil_currency_and_annual_scope(self):
        from lxml import etree
        def node(text):
            return etree.fromstring(text.encode(), etree.HTMLParser()).find('.//body')[0]
        self.assertEqual(parse_number(node('<ix:nonFraction scale="3" sign="-" format="ixt:num-dot-decimal">1,234</ix:nonFraction>')), -1_234_000)
        self.assertEqual(parse_number(node('<ix:nonFraction format="ixt:zerodash">—</ix:nonFraction>')), 0)
        self.assertIsNone(parse_number(node('<ix:nonFraction xsi:nil="true">0</ix:nonFraction>')))
        with self.assertRaises(ValueError):
            parse_number(node('<ix:nonFraction format="ixt:unknown">1</ix:nonFraction>'))
        wrong_currency = self.data.replace(b'iso4217:USD', b'iso4217:EUR')
        with self.assertRaises(ValueError):
            parse_filing('euros.htm', wrong_currency)
        quarterly = self.data.replace(b'-01-01</xbrli:startDate>', b'-10-01</xbrli:startDate>')
        with self.assertRaises(ValueError):
            merge_filings([('quarter-duration.htm', quarterly)])

    def test_da_addback_preserves_gaap_gross_margin(self):
        frame, _ = build_import_matrix(self.report, load_baseline_data())
        history = financials(validate_financial_data(frame), 'Year 3')
        self.assertEqual(history['ebitda'], 9_000_000)
        self.assertEqual(history['ratios']['Gross Margin'], .35)

    def test_upload_ui_blocks_until_review_and_missing_fills(self):
        from streamlit.testing.v1 import AppTest
        class Uploaded(BytesIO):
            name = 'manufacturing-2025.htm'
        app_path = str(Path(__file__).with_name('app.py'))
        with patch('streamlit.file_uploader', return_value=[Uploaded(self.data)]):
            app = AppTest.from_file(app_path).run(timeout=30)
            app.radio(key='input_mode').set_value('Upload EDGAR 10-K files').run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(len(app.metric), 0)
            approval = next(item for item in app.checkbox if item.label.startswith('I reviewed all three years'))
            approval.check().run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            self.assertGreater(len(app.metric), 0)
            self.assertTrue(any('50,000,000' in item.value for item in app.markdown))


def rendered_fixture():
    """Small regression fixture for the table format in the supplied Veeva reports."""
    def row(concept, label, values, dimension=False):
        cells = ''.join(f'<td class="nump">{value}</td>' for value in values)
        return f'''<tr><td><a onclick="Show.showAR(this, 'defref_{concept}', window);">{label}</a></td>{cells}</tr>'''
    cover = '<table class="report">' + ''.join(row('dei_' + tag, tag, [value]) for tag, value in [
        ('DocumentType', '10-K'), ('DocumentPeriodEndDate', 'Jan. 31, 2025'),
        ('EntityRegistrantName', 'Rendered Company'), ('EntityCentralIndexKey', '000123456')]) + '</table>'
    header = '<tr><th rowspan="2">CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME - USD ($) $ in Thousands</th><th colspan="3">12 Months Ended</th></tr><tr><th>Jan. 31, 2025</th><th>Jan. 31, 2024</th><th>Jan. 31, 2023</th></tr>'
    income = '<table class="report">' + header
    income += row('us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax', 'Total revenue', ['50,000', '45,000', '40,000'])
    income += row('us-gaap_EarningsPerShareBasic', 'Basic earnings per share', ['1.23', '1.11', '1.00'])
    income += row('srt_ProductOrServiceAxis=us-gaap_SubscriptionAndCirculationMember', 'Subscription segment', ['', '', ''])
    income += row('us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax', 'Segment revenue', ['30,000', '25,000', '20,000'])
    income += '</table>'
    cash = '<table class="report">' + header.replace('COMPREHENSIVE INCOME', 'CASH FLOWS')
    cash += row('us-gaap_PaymentsToAcquirePropertyPlantAndEquipment', 'Capital expenditures', ['(1,234)', '0', '—'])
    cash += row('us-gaap_NetCashProvidedByUsedInInvestingActivities', 'Net investing flow', ['(1,234)', '(50)', '0'])
    cash += '</table>'
    return ('<html><body>' + cover + income + cash + '</body></html>').encode()


class RenderedReportTests(unittest.TestCase):
    def test_annual_currency_scaling_and_segment_exclusion(self):
        report = merge_filings([('rendered.html', rendered_fixture())])
        self.assertEqual(report['company'], 'Rendered Company')
        self.assertEqual(report['periods'][-1], ('2024-02-01', '2025-01-31'))
        self.assertEqual(select_fact(report, ALIASES['Revenue'], '2024-02-01', '2025-01-31')[0], 50_000_000)
        self.assertTrue(any(f.dimensional for f in report['facts']))
        self.assertFalse(any('PerShare' in f.concept for f in report['facts']))
        self.assertIn('Rendered', report['source_formats'][0])
        self.assertTrue(any('not original Inline XBRL' in warning for warning in report['warnings']))

    def test_cash_outflows_and_zero_rendering(self):
        report = merge_filings([('rendered.html', rendered_fixture())])
        facts = [f for f in report['facts'] if f.local == 'PaymentsToAcquirePropertyPlantAndEquipment']
        self.assertEqual([f.value for f in facts], [1_234_000, 0, 0])
        net = [f for f in report['facts'] if f.local == 'NetCashProvidedByUsedInInvestingActivities']
        self.assertEqual(net[0].value, -1_234_000)

    def test_reject_nonannual_and_missing_metadata(self):
        for data in (rendered_fixture().replace(b'12 Months Ended', b'3 Months Ended'),
                     rendered_fixture().replace(b'>10-K<', b'>10-Q<')):
            with self.assertRaises(ValueError):
                merge_filings([('bad-rendered.html', data)])

    def test_manual_inputs_recalculate_dependent_rows(self):
        report = merge_filings([('missing.htm', fixture(omit='InterestExpenseNonOperating'))])
        schema = load_baseline_data()
        frame, audit = build_import_matrix(report, schema, {('Interest expense', year): 0 for year in ('Year 1', 'Year 2', 'Year 3')})
        items = frame.set_index('Line item')
        self.assertEqual(items.at['Interest expense', 'Year 3'], 0)
        self.assertEqual(items.at['Other non-operating income / (expense)', 'Year 3'], -900_000)
        row = audit[(audit['Line item'] == 'Interest expense') & (audit['Year'] == 'Year 3')].iloc[0]
        self.assertTrue(math.isnan(row['Imported value']))
        self.assertEqual(row['Reviewed value'], 0)
        self.assertIn('User supplied', row['Status'])
