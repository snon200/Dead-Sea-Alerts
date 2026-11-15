"""Tests for the lightweight v2 Stage 3 detector and zone export.

Run from repository root:
    python -m unittest discover -s tests -p test_subsidence_detection_v2.py -v
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np

# Running from repo root/test folder: import the adjacent src directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import subsidence_detection_v2 as detector


class DetectionV2Tests(unittest.TestCase):
    def test_exact_risk_bins(self):
        for speed, acceleration, expected in [
            (9.9, True, 'low'), (10, None, 'watch'),
            (39.9, True, 'watch'), (40, False, 'watch'),
            (40, None, 'watch'), (40, True, 'elevated'),
            (100, True, 'elevated'), (100, False, 'watch'),
            (100.0001, False, 'high'), (None, True, None),
        ]:
            with self.subTest(rate=speed, acceleration=acceleration):
                self.assertEqual(detector.classify_rate(speed, acceleration), expected)

    def test_last_three_intervals_acceleration(self):
        dates = np.arange(4, dtype=float) * 12
        d = np.array([0, -20, -60, -120], dtype=float)[:, None, None] * np.ones((4, 3, 3))
        q = np.ones_like(d) * .95
        m = detector.temporal_metrics(d, q, dates, np.ones((3, 3), bool), 3, .35)
        self.assertTrue(m['accelerating_last_3'])
        self.assertGreater(m['subsidence_mm_year'], 100.)
        d[-1] = -61  # Latest subsidence rate nearly zero (not accelerating)
        m = detector.temporal_metrics(d, q, dates, np.ones((3, 3), bool), 3, .35)
        self.assertFalse(m['accelerating_last_3'])
        d[-1] = -120
        d[1] = np.nan
        m = detector.temporal_metrics(d, q, dates, np.ones((3, 3), bool), 3, .35)
        self.assertIsNone(m['accelerating_last_3'])

    def test_end_to_end_processed_run(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            p = base/'data'/'processed'/'run_v2_test'
            p.mkdir(parents=True)
            out_parent = base/'data'/'detection'
            n, h, w = 5, 48, 48
            d = np.zeros((n,h,w), np.float32)
            for k, value in enumerate([0, -5, -12, -23, -40]):
                d[k, 16:32, 16:32] = value
            q = np.ones_like(d) * .9
            np.save(p/'displacement.npy', d)
            np.save(p/'coherence.npy',q)
            time0 = datetime(2026,6,21,2,59,41,tzinfo=timezone.utc)
            tms = [time0+timedelta(days=12*k) for k in range(n)]
            dates = [t.date().isoformat() for t in tms]
            meta = {
                'stage':'geophysical_processing_cumulative_stack',
                'array_shape':[n,h,w],
                'dates':dates,
                'files':{'displacement':'displacement.npy','coherence':'coherence.npy'},
                'grid':{'crs':'EPSG:32636','transform_affine':[80,0,730000,0,-80,3488000],
                        'width':w,'height':h},
                'displacement':{'component':'los','positive_los_direction':'toward',
                                'phase_sign':-1,'phase_sign_verified_for_provisional_products':False,
                                'reference_bbox_wsen':[35.37,31.44,35.38,31.45]},
                'acquisition_family':{'track':100,'frame':17,'direction':'A'},
                'pairs':[{'reference_time_utc':a.isoformat(), 'secondary_time_utc':b.isoformat(),
                          'granule_id':'TEST'+str(i)} for i,(a,b) in enumerate(zip(tms,tms[1:]))],
            }
            (p/'metadata.json').write_text(json.dumps(meta),encoding='utf8')
            cfg = detector.DetectionConfig(coherence_min=.35, local_sigma_m=100,
                     background_sigma_m=900, minimum_filter_support=.2,
                     minimum_movement_mm=3, minimum_rate_mm_day=0,
                     minimum_snr=2.5, noise_floor_mm=1,
                     minimum_pixels=4)
            result = detector.main_process(p, out_parent, cfg)
            out = out_parent / p.name
            self.assertTrue((out/'anomalies.geojson').exists())
            self.assertEqual(np.load(out/'velocity.npy').shape, (4,h,w))
            self.assertEqual(np.load(out/'acceleration.npy').shape, (3,h,w))
            self.assertEqual(np.load(out/'candidate_mask.npy').shape, (5,h,w))
            geo = json.loads((out/'anomalies.geojson').read_text())
            self.assertGreater(len(geo['features']),0, 'Injected patch must be detected')
            latest = [f for f in geo['features'] if f['properties']['last_observed']==dates[-1]]
            self.assertTrue(latest)
            feature=max(latest,key=lambda f:f['properties']['pixel_count'])
            props=feature['properties']
            self.assertEqual(props['risk_level'],'high')
            self.assertTrue(props['accelerating_last_3_intervals'])
            self.assertEqual(len(props['recent_3_interval_rates_mm_per_year']),3)
            self.assertEqual(feature['geometry']['type'],'Polygon')
            self.assertEqual(result['stage'],'subsidence_detection')


if __name__=='__main__':
    unittest.main()
