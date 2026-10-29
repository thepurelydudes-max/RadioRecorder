"""Regression tests for compact audio name and driver display in v0.5.9."""
import unittest
from device_names import friendly_device_name, settings_device_choices, selection_label


class CompactNamesTest(unittest.TestCase):
    def test_redundant_alias_dropped_and_host_shortened(self):
        name = 'ESI MAYA CH1/2 (ESI MAYA CH1/2 MME) — Windows WASAPI (#18)'
        ident = 'portaudio:' + name
        self.assertEqual(settings_device_choices([(name, ident)]),
                         [('ESI MAYA CH1/2 — WASAPI', ident)])
        self.assertEqual(friendly_device_name(name), 'ESI MAYA CH1/2')

    def test_all_host_types_and_real_parentheses(self):
        samples = [
            ('Микрофон (Realtek(R) Audio) — Windows WASAPI (#12)', 'Микрофон (Realtek(R) Audio) — WASAPI'),
            ('ESI MAYA CH1/2 (ESI MAYA CH1/2) — MME (#4)', 'ESI MAYA CH1/2 — MME'),
            ('ESI MAYA CH1/2 (ESI MAYA CH1/2 ASIO) — ASIO (#7)', 'ESI MAYA CH1/2 — ASIO'),
            ('Line In — Windows DirectSound (#8)', 'Line In — DirectSound'),
            ('Line In — Windows WDM-KS (#5)', 'Line In — WDM-KS'),
        ]
        for src, want in samples:
            with self.subTest(src=src):
                self.assertEqual(settings_device_choices([(src, 'portaudio:' + src)])[0][0], want)

    def test_different_apis_same_device_keep_distinct_exact_ids(self):
        api_names = ['Windows WASAPI (#15)', 'MME (#2)', 'Windows DirectSound (#5)', 'ASIO (#30)']
        choices = [(f'ESI MAYA CH1/2 (ESI MAYA CH1/2 MME) — {api}',
                    f'portaudio:ESI MAYA CH1/2 (ESI MAYA CH1/2 MME) — {api}')
                   for api in api_names]
        pretty = settings_device_choices(choices)
        self.assertEqual([x[0] for x in pretty], [
            'ESI MAYA CH1/2 — WASAPI', 'ESI MAYA CH1/2 — MME',
            'ESI MAYA CH1/2 — DirectSound', 'ESI MAYA CH1/2 — ASIO'])
        self.assertEqual([x[1] for x in pretty], [x[1] for x in choices])
        for (label, ident) in pretty:
            self.assertEqual(selection_label(ident, pretty, choices), label)

    def test_directshow_and_unrelated_alias_not_removed(self):
        choices = [('ESI MAYA CH1/2 (Other Sound Card)', 'dshow:1'),
                   ('ESI MAYA CH1/2 (ESI MAYA CH1/2)', 'dshow:2')]
        self.assertEqual(settings_device_choices(choices, mark_directshow=True), [
            ('ESI MAYA CH1/2 (Other Sound Card) — DirectShow', 'dshow:1'),
            ('ESI MAYA CH1/2 — DirectShow', 'dshow:2')])

    def test_true_duplicate_labels_remain_independently_selectable(self):
        choices = [('ESI — Windows WASAPI (#2)', 'portaudio:ESI — Windows WASAPI (#2)'),
                   ('ESI — Windows WASAPI (#4)', 'portaudio:ESI — Windows WASAPI (#4)')]
        result = settings_device_choices(choices)
        self.assertEqual([x[0] for x in result], ['ESI — WASAPI', 'ESI — WASAPI (2)'])
        self.assertEqual(selection_label(choices[1][1], result, choices), 'ESI — WASAPI (2)')

if __name__ == '__main__':
    unittest.main()
