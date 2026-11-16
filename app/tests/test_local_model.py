import os,sys,json,tempfile,unittest,subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import local_model_config as model

FAKE='fixture-local-qwen-not-a-real-secret'
class BackendModelConfiguration(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.key=self.root/'key'
  self.env=patch.dict(os.environ,{'LOCAL8018_QWEN_KEY_FILE':str(self.key),'V16_QWEN_API_KEY':'','DASHSCOPE_API_KEY':''})
  self.env.start();self.addCleanup(self.env.stop)
 def save(self,value=FAKE):
  self.key.write_text(value);self.key.chmod(0o600)
 def test_missing_key_never_claims_ready(self):
  self.assertFalse(model.status()['configured'])
  with self.assertRaises(model.ModelConfigurationError):model.load_key()
 def test_owner_only_file_and_redacted_status(self):
  self.save();self.assertEqual(model.load_key(),FAKE)
  self.assertTrue(model.status()['configured']);self.assertNotIn(FAKE,json.dumps(model.status()))
 def test_world_readable_rejected(self):
  self.save();self.key.chmod(0o644)
  with self.assertRaises(model.ModelConfigurationError):model.load_key()
 def test_symlink_rejected(self):
  other=self.root/'other';other.write_text(FAKE);other.chmod(0o600);self.key.symlink_to(other)
  with self.assertRaises(model.ModelConfigurationError):model.load_key()
 def test_empty_or_corrupt_key_rejected(self):
  for value in ['', 'x y', 'x'*513]:
   self.save(value)
   with self.assertRaises(model.ModelConfigurationError):model.load_key()
 def test_refresh_and_cold_restart_reload(self):
  self.save();self.assertEqual(model.renderer_environment()['V16_QWEN_API_KEY'],FAKE)
  self.save('fixture-rotated-key')
  code='import local_model_config as m; assert m.load_key()=="fixture-rotated-key"; print("cold-load-ok")'
  env=dict(os.environ,PYTHONPATH=str(Path(model.__file__).parent))
  self.assertEqual(subprocess.check_output([sys.executable,'-c',code],env=env,text=True).strip(),'cold-load-ok')
 def test_browser_override_ignored_and_key_never_rendered(self):
  self.save()
  import v16_web_app as web
  with web.app.test_request_context('/run',method='POST',data={'vision_api_key':'browser-injected-key'}):
   self.assertEqual(web.parse_external_api_keys(),('qwen',FAKE))
  with patch.object(web,'ensure_task_queue_worker',return_value=False):
   page=web.app.test_client().get('/').get_data(as_text=True)
   self.assertNotIn('id="vision-api-key"',page)
   self.assertNotIn(FAKE,page)
   self.assertIn('程序自动读取授权',page)
 def test_child_process_gets_key_without_queue(self):
  self.save();env=dict(os.environ,**model.renderer_environment())
  code='import os; assert os.environ["V16_QWEN_API_KEY"]=="'+FAKE+'"; print("child-key-ok")'
  self.assertEqual(subprocess.check_output([sys.executable,'-c',code],env=env,text=True).strip(),'child-key-ok')
if __name__=='__main__':unittest.main()
