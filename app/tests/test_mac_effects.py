import os,sys,unittest,tempfile,subprocess,shutil
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mac_performance as m
import run_v16_enhanced as r
class MacEffects(unittest.TestCase):
 def test_final_threads_are_separate_and_idempotent(self):
  cmd=['ffmpeg','-i','input','-filter_complex','null','output']
  with patch.dict(os.environ,{'V16_FINAL_FILTER_THREADS':'8'}),patch.object(m.platform,'system',return_value='Darwin'):
   out=m.final_render_command(cmd)
   self.assertEqual(out[:3],['ffmpeg','-filter_complex_threads','8'])
   self.assertEqual(m.final_render_command(out),out)
   self.assertEqual(m.ffmpeg_command(out,2).count('-filter_complex_threads'),1)
   self.assertEqual(m.final_render_command(['ffmpeg','-i','input','output']),['ffmpeg','-i','input','output'])
 def test_windows_and_optout_keep_original(self):
  with patch.object(m.platform,'system',return_value='Windows'),patch.dict(os.environ,{'V16_STATIC_CORNER_ALPHA':'1'}):
   self.assertFalse(m.static_corner_alpha_enabled())
   self.assertEqual(m.final_render_command(['ffmpeg']),['ffmpeg'])
  with patch.dict(os.environ,{'V16_STATIC_CORNER_ALPHA':'0'}):self.assertFalse(m.static_corner_alpha_enabled())
 @unittest.skipUnless(shutil.which('ffmpeg'),'ffmpeg required')
 def test_exact_moving_frames_with_looping_mask_and_odd_radii(self):
  for radius in [0,24,72,131,240]:
   hashes=[]
   for enabled in ['0','1']:
    with patch.dict(os.environ,{'V16_STATIC_CORNER_ALPHA':enabled}):
     parts,label=r.build_corner_mask_reveal_filter('[0:v]',1,mode=r._corner_mask_mode_for_radius(radius),radius_px=radius)
    parts.append(f'{label}format=yuv420p[out]')
    cmd=['ffmpeg','-v','error','-filter_complex_threads','8' if enabled=='1' else '2',
     '-f','lavfi','-i','testsrc2=size=720x1280:rate=30:duration=1',
     '-f','lavfi','-i','testsrc=size=720x1280:rate=30:duration=0.2',
     '-filter_complex',';'.join(parts),'-map','[out]','-t','1','-f','framemd5','-']
    p=subprocess.run(cmd,capture_output=True,text=True,timeout=45)
    self.assertEqual(p.returncode,0,p.stderr)
    hashes.append([x for x in p.stdout.splitlines() if not x.startswith('#')])
   self.assertEqual(hashes[0],hashes[1],f'radius {radius}')
   self.assertGreater(len(hashes[1]),0)
if __name__=='__main__':unittest.main()
