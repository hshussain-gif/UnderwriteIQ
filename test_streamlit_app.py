import unittest
from pathlib import Path
from streamlit.testing.v1 import AppTest


class StreamlitTests(unittest.TestCase):
    def test_live_facilities_stress_and_challenges(self):
        app = AppTest.from_file(str(Path(__file__).with_name('app.py'))).run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        for kind in ('Interest-Only (I/O) Period', 'Revolver', 'Fully Amortizing'):
            app.selectbox(key='facility_type').set_value(kind).run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            app.selectbox(key='frequency').set_value('Quarterly').run(timeout=30)
            self.assertEqual(len(app.exception), 0)
            for challenge in 'ABC':
                app.selectbox(key='challenge').set_value(challenge).run(timeout=30)
                self.assertEqual(len(app.exception), 0)
            app.selectbox(key='frequency').set_value('Monthly').run(timeout=30)
        app.slider(key='haircut').set_value(50)
        app.slider(key='compression').set_value(500)
        app.select_slider(key='shock').set_value(500)
        app.run(timeout=30)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(any('CRITICAL VULNERABILITY' in block.value for block in app.markdown))
