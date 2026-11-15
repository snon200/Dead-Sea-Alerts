from __future__ import annotations
import json
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from subsidence_detection_v2 import DetectionConfig, main_process
from zone_service import build_zones, export_cache

class ForwardingTests(unittest.TestCase):
    def test_risk_in_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'data'; proc=root/'processed'/'run_v2_test'; proc.mkdir(parents=True)
            n,h,w=5,48,48
            d=np.zeros((n,h,w),np.float32)
            for i,v in enumerate([0,-5,-12,-23,-40]): d[i,16:32,16:32]=v
            np.save(proc/'displacement.npy',d)
            np.save(proc/'coherence.npy',np.ones_like(d,dtype=np.float32)*.95)
            t0=datetime(2026,6,21,2,59,41,tzinfo=timezone.utc)
            times=[t0+timedelta(days=12*i) for i in range(n)]
            meta={
                'stage':'geophysical_processing_cumulative_stack',
                'array_shape':list(d.shape), 'dates':[t.date().isoformat() for t in times],
                'grid':{'crs':'EPSG:32636','transform_affine':[80,0,730000,0,-80,3488000], 'width':w,'height':h},
                'displacement':{'component':'los','positive_los_direction':'toward','phase_sign':-1,'reference_bbox_wsen':[35.37,31.44,35.38,31.45], 'phase_sign_verified_for_provisional_products':False},
                'files':{'displacement':'displacement.npy','coherence':'coherence.npy'},
                'acquisition_family':{'track':100,'frame':17,'direction':'A','polarization':'HH'},
                'pairs':[{'reference_time_utc':a.isoformat(),'secondary_time_utc':b.isoformat(),'granule_id':'s'+str(i)} for i,(a,b) in enumerate(zip(times,times[1:]))]
            }
            (proc/'metadata.json').write_text(json.dumps(meta))
            cfg=DetectionConfig(coherence_min=.35,minimum_movement_mm=3,minimum_rate_mm_day=0,minimum_snr=2.5,noise_floor_mm=1,minimum_filter_support=.2)
            main_process(proc,root/'detection',cfg)
            zones=build_zones(run_name='run_v2_test',data_root=root)
            self.assertTrue(zones)
            self.assertTrue(any(z['risk_level']=='high' for z in zones), zones)
            path=export_cache(run_name='run_v2_test',data_root=root,output=root/'api'/'zones.json')
            payload=json.loads(path.read_text())
            self.assertEqual(payload['schema_version'],'1.0')
            self.assertTrue(any(z['risk_level']=='high' for z in payload['zones']))

if __name__=='__main__': unittest.main()
