import unittest
from dataclasses import replace
import math
from app import (load_baseline_data, evaluate_scenario, Facility, debt_capacity,
                 facility_schedule, annual_schedule, credit_memo, analyst_answer, financials)


class CommitteeTests(unittest.TestCase):
    def setUp(self):
        self.frame = load_baseline_data()
        self.facility = Facility()
        self.base = evaluate_scenario(self.frame, self.facility)

    def test_capacity_constraints_all_facilities(self):
        for facility in (self.facility, replace(self.facility, kind='Interest-Only (I/O) Period', io_months=12),
                         replace(self.facility, kind='Revolver')):
            scenario = evaluate_scenario(self.frame, facility)
            capacity = debt_capacity(self.frame, scenario)
            schedule = facility_schedule(replace(facility, amount=capacity['maximum']))
            worst = 9_000_000 / (2_400_000 + annual_schedule(schedule)['Debt service'].max())
            self.assertGreaterEqual(worst, 1.25)
            self.assertLessEqual((12_000_000 + schedule.iloc[0]['Opening balance']) / 9_000_000, 4.5)
            next_schedule = facility_schedule(replace(facility, amount=round(capacity['maximum'] + .01, 2)))
            self.assertTrue(annual_schedule(next_schedule)['Debt service'].max() > 9_000_000 / 1.25 - 2_400_000
                            or next_schedule.iloc[0]['Opening balance'] > 4.5 * 9_000_000 - 12_000_000)

    def test_zero_utilization_and_infeasible_earnings(self):
        facility = replace(self.facility, kind='Revolver', utilization=0, commitment_fee=0)
        self.assertTrue(math.isinf(debt_capacity(self.frame, evaluate_scenario(self.frame, facility))['maximum']))
        case = evaluate_scenario(self.frame, self.facility, 50, 3000)
        self.assertEqual(debt_capacity(self.frame, case)['maximum'], 0)

    def test_memo_branches(self):
        cap = debt_capacity(self.frame, self.base)
        memo = credit_memo(self.frame, self.base, self.base, cap, 0, 0, 0)
        for section in ('Executive Summary', 'Financial Performance Analysis', 'Stress Test Vulnerabilities', 'Credit Committee Recommendation'):
            self.assertIn('## ' + section, memo)
        self.assertIn('RESTRUCTURE / DEFER', memo)
        case = evaluate_scenario(self.frame, self.facility, 50, 500, 500)
        self.assertIn('CRITICAL VULNERABILITY', credit_memo(self.frame, self.base, case, debt_capacity(self.frame, case), 50, 500, 500))
        bad = self.frame.copy()
        bad.loc[bad['Line item'] == 'Cash and equivalents', 'Year 3'] += 10
        self.assertIn('DEFER — reconcile', credit_memo(bad, self.base, self.base, cap, 0, 0, 0))

    def test_dialogue_recomputes_and_discloses_structures(self):
        for kind in ('Fully Amortizing', 'Interest-Only (I/O) Period', 'Revolver'):
            facility = replace(self.facility, kind=kind, io_months=12)
            scenario = evaluate_scenario(self.frame, facility)
            capacity = debt_capacity(self.frame, scenario)
            for challenge in 'ABC':
                answer = analyst_answer(challenge, self.frame, facility, scenario, capacity, 0, 0)
                self.assertGreater(len(answer), 100)
                if challenge == 'C' and kind == 'Revolver':
                    self.assertIn('zero scheduled principal', answer)
                if challenge == 'C' and kind != 'Revolver':
                    self.assertIn('worst annual scheduled DSCR', answer)
