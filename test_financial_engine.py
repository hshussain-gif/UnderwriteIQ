import unittest
from dataclasses import replace
import math
import pandas as pd
from app import (load_baseline_data, validate_financial_data, financials, consistency_issues,
                 Facility, facility_schedule, annual_schedule, evaluate_scenario, assess_credit_risk)


class FinancialEngineTests(unittest.TestCase):
    def setUp(self):
        self.frame = validate_financial_data(load_baseline_data())

    def test_schema_independent_of_order(self):
        reordered = self.frame.sample(frac=1, random_state=4)
        self.assertEqual(financials(reordered, 'Year 3'), financials(self.frame, 'Year 3'))
        self.assertEqual(consistency_issues(self.frame), [])
        latest = financials(self.frame, 'Year 3')
        self.assertEqual(latest['ebitda'], 9_000_000)
        self.assertAlmostEqual(latest['ratios']['EBITDA Margin'], .18)
        self.assertEqual(latest['ratios']['DSCR'], 3.75)

    def test_invalid_schema_and_cells(self):
        for frame in (self.frame.drop(columns='Year 2'), pd.concat([self.frame, self.frame.iloc[:1]]), self.frame.iloc[1:]):
            with self.assertRaises(ValueError):
                validate_financial_data(frame)
        for value in (float('nan'), float('inf'), -1, 'bad'):
            invalid = self.frame.copy()
            invalid['Year 3'] = invalid['Year 3'].astype(object)
            invalid.loc[0, 'Year 3'] = value
            with self.assertRaises(ValueError):
                validate_financial_data(invalid)

    def test_amortization_conservation_and_annuity(self):
        for frequency in (4, 12):
            for rate in (0, 7.5):
                schedule = facility_schedule(Facility(annual_rate=rate, payments_per_year=frequency))
                self.assertAlmostEqual(schedule['Principal'].sum(), 8_000_000)
                self.assertEqual(schedule.iloc[-1]['Closing balance'], 0)
                self.assertTrue((schedule['Closing balance'] >= 0).all())
                self.assertTrue((schedule['Commitment fee'] == 0).all())
                if rate:
                    r = rate / 100 / frequency
                    expected = 8_000_000 * r / (1 - (1 + r) ** (-10 * frequency))
                    self.assertAlmostEqual(schedule.iloc[0]['Debt service'], expected, places=2)
                else:
                    self.assertEqual(schedule['Interest'].sum(), 0)

    def test_io_original_maturity_and_step_up(self):
        for frequency in (4, 12):
            schedule = facility_schedule(Facility(kind='Interest-Only (I/O) Period', io_months=12, payments_per_year=frequency))
            self.assertEqual(schedule.iloc[:frequency]['Principal'].sum(), 0)
            self.assertAlmostEqual(schedule.iloc[:frequency]['Interest'].sum(), 600_000)
            self.assertEqual(schedule.iloc[-1]['Closing balance'], 0)
            self.assertAlmostEqual(schedule['Principal'].sum(), 8_000_000)
            annual = annual_schedule(schedule)
            self.assertGreater(annual.iloc[1]['Debt service'], annual.iloc[0]['Debt service'])
        with self.assertRaises(ValueError):
            facility_schedule(Facility(kind='Interest-Only (I/O) Period', io_months=1, payments_per_year=4))
        with self.assertRaises(ValueError):
            facility_schedule(Facility(kind='Interest-Only (I/O) Period', io_months=120))

    def test_revolver_interest_fee_and_drawn_leverage(self):
        facility = Facility(kind='Revolver', utilization=60, commitment_fee=.5)
        schedule = facility_schedule(facility)
        self.assertEqual(schedule['Principal'].sum(), 0)
        self.assertEqual(schedule.iloc[-1]['Closing balance'], 4_800_000)
        first = annual_schedule(schedule).iloc[0]
        self.assertAlmostEqual(first['Debt service'], 4_800_000 * .075 + 3_200_000 * .005, places=1)
        scenario = evaluate_scenario(self.frame, facility)
        self.assertEqual(scenario['debt'], 16_800_000)
        self.assertAlmostEqual(scenario['dscr'], 9_000_000 / (2_400_000 + first['Debt service']))
        for utilization in (0, 100):
            case = evaluate_scenario(self.frame, replace(facility, utilization=utilization))
            self.assertEqual(case['debt'], 12_000_000 + 8_000_000 * utilization / 100)

    def test_stress_and_rating_endpoints(self):
        scenario = evaluate_scenario(self.frame, Facility(), 10, 200, 100)
        self.assertEqual(scenario['income']['Revenue'], 45_000_000)
        self.assertAlmostEqual(scenario['ebitda'], 7_200_000)
        self.assertEqual(scenario['facility'].annual_rate, 8.5)
        self.assertEqual(scenario['risk']['grade'], 2)
        for dscr, leverage, grade in ((2.01, 2.49, 1), (2, 2.5, 2), (1.5, 3.5, 2), (1.25, 4.5, 3), (1, 5.5, 4), (.99, 2, 5), (3, 5.51, 5)):
            self.assertEqual(assess_credit_risk(dscr, leverage, 1)['grade'], grade)
        loss = evaluate_scenario(self.frame, Facility(), 50, 3000, 500)
        self.assertLess(loss['ebitda'], 0)
        self.assertEqual(loss['risk']['grade'], 5)
        self.assertTrue(math.isnan(loss['leverage']))
