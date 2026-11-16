import unittest,sys,os,subprocess,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mac_performance as m
class Performance(unittest.TestCase):
 def test_m4max_profile_with_memory_headroom(self):
  p=m.profile({'chip':'Apple M4 Max','performance_cores':10,'memory_gib':36})
  self.assertEqual((p['render_workers'],p['filter_threads'],p['queue_workers']),(4,2,1))
 def test_smaller_mac_compatible(self):
  for h in [{'chip':'Apple M2','performance_cores':4,'memory_gib':16},{'chip':'Apple M4 Max','performance_cores':10,'memory_gib':24}]:self.assertEqual(m.profile(h)['render_workers'],2)
 def test_filter_flags_idempotent_and_input_order_preserved(self):
  cmd=['ffmpeg','-y','-i','a.mp4','-c:v','h264_videotoolbox','-q:v','65','out.mp4']
  out=m.ffmpeg_command(cmd,2)
  self.assertEqual(m.ffmpeg_command(out,2),out)
  self.assertEqual(out[5:],cmd[1:])
  self.assertEqual(m.ffmpeg_command(cmd,0),cmd)
 def test_renderer_obeys_four_lanes(self):
  env=dict(os.environ,V16_RENDER_WORKERS='4',V16_FILTER_THREADS='2')
  text=subprocess.check_output([sys.executable,'-c','import run_v16_enhanced as r; assert r.RENDER_WORKERS==4; print("four-lanes-ok")'],cwd=Path(m.__file__).parent,env=env,text=True)
  self.assertIn('four-lanes-ok',text)
 def test_four_source_lanes_preserve_order_without_nested_fanout(self):
  import run_v16_enhanced as r
  from unittest.mock import patch
  import threading,time
  guard=threading.Lock();active=0;peak=0
  def render(path,prefix,**kw):
   nonlocal active,peak
   self.assertEqual(kw['render_workers'],1)
   with guard:active+=1;peak=max(peak,active)
   time.sleep(.03)
   with guard:active-=1
   return [path]
  with patch.object(r,'RENDER_WORKERS',4),patch.object(r,'split_dedup_video',side_effect=render):
   self.assertEqual(r.split_dedup_sources_parallel(list(range(8)),'test'),list(range(8)))
  self.assertEqual(peak,4)
 def test_safe_hardware_detection(self):
  p=m.profile();self.assertEqual(p['chip'],'Apple M4 Max');self.assertEqual(p['render_workers'],4)
if __name__=='__main__':unittest.main()
