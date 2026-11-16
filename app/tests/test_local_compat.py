import os,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
TEMP=tempfile.TemporaryDirectory()
os.environ.update(V16_DATA_DIR=TEMP.name,V16_LOCAL_NO_VALIDATION='1',V17_TENANT_BACKEND='0',V17_QUEUE_WORKERS='1',V16_ERP_DIRECT_ENABLED='0')
import v16_web_app as web
from task_queue_store import PersistentTaskQueue
from auto_fill_runtime import RenderCheckpoint,FillCheckpointError,digest

class LocalCompatibility(unittest.TestCase):
 def test_local_queue_claim_is_fifo(self):
  with tempfile.TemporaryDirectory() as d:
   q=PersistentTaskQueue(Path(d)/'q.db')
   q.enqueue_legacy_internal('first',{},task_id='a')
   q.enqueue_legacy_internal('second',{},task_id='b')
   self.assertEqual(web._claim_next_queue_item(q)['task_id'],'a')
   self.assertEqual(web._claim_next_queue_item(q)['task_id'],'b')
 def test_local_does_not_enable_server_cleanup_or_auth(self):
  self.assertFalse(web.TENANT_BACKEND_MODE)
  self.assertFalse(web.ERP_DIRECT_ENABLED)
  self.assertFalse(web.CUSTOMER_BILLING_REQUIRED)
  self.assertEqual(web.strict_erp_configuration_error(),'')
  store=object()
  self.assertEqual(web.best_effort_expire_task_outputs(store)['expired_count'],0)
  self.assertFalse(web.run_daily_completed_output_cleanup_if_due(store)['due'])
 def test_local_download_lease_retains_path_guard(self):
  with tempfile.TemporaryDirectory() as d, patch.object(web,"BASE_DIR",Path(d).resolve()):
   root=web._validated_delivery_lease_root()
   self.assertEqual(root.parent,Path(d).resolve())
   keys=web._mark_output_download_tasks(["a"*32])
   web._release_output_download_tasks(keys)
   root.rmdir() if not list(root.iterdir()) else None
  with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside, patch.object(web,"BASE_DIR",Path(d).resolve()):
   (Path(d)/web.BUNDLE_DELIVERY_LEASE_DIR_NAME).symlink_to(outside,target_is_directory=True)
   with self.assertRaises(OSError):web._validated_delivery_lease_root()
 def test_local_page_and_csrf(self):
  with patch.object(web,'ensure_task_queue_worker',return_value=False):
   c=web.app.test_client();r=c.get('/')
   self.assertEqual(r.status_code,200)
   self.assertIn('开头视频',r.get_data(as_text=True))
   self.assertNotIn('退出登录</button>',r.get_data(as_text=True))
   self.assertEqual(c.post('/task-drafts/create',headers={'Accept':'application/json','X-Requested-With':'fetch'}).status_code,403)
 def test_macos_encoder_and_queue_configuration(self):
  self.assertEqual(web.TASK_QUEUE_WORKER_COUNT,1)
  if sys.platform=='darwin':self.assertEqual(web.WINDOWS_VIDEO_ENCODER_POLICY,'auto')

class RetryPolicy(unittest.TestCase):
 def checkpoint(self,d):
  root=Path(d);return RenderCheckpoint(root/'checkpoint.json','a'*32,'local',5,'signature',root,lambda s:None)
 def add(self,c,n):
  candidate=c.root/f'candidate{n}.mp4';final=c.root/f'final{n}.mp4'
  candidate.write_bytes(f'unique test video {n}'.encode())
  paths=[Path(x['path']) for x in c.data['files']]+[final]
  c.publish(candidate,final,c.root/'no-report',c.root/f'report{n}',{'accepted_paths':paths,'pair_results':[]})
 def retry(self,c):
  return c.retry(RuntimeError('injected render failure'),{'accepted_paths':[Path(x['path']) for x in c.data['files']],'pair_results':[]},lambda:None)
 def test_initial_0_1_2_never_retry(self):
  for n in (0,1,2):
   with self.subTest(n=n),tempfile.TemporaryDirectory() as d:
    c=self.checkpoint(d)
    try:
     for i in range(n):self.add(c,i)
     self.assertFalse(self.retry(c));self.assertEqual(c.round,0)
    finally:c.close()
 def test_three_plus_one_plus_one_preserves_files_and_restart(self):
  with tempfile.TemporaryDirectory() as d:
   c=self.checkpoint(d)
   for i in range(3):self.add(c,i)
   hashes=[digest(x['path']) for x in c.data['files']]
   self.assertTrue(self.retry(c));self.assertEqual(c.similarity_start,3)
   self.add(c,3);self.assertTrue(self.retry(c));c.close()
   c=self.checkpoint(d)
   try:
    self.assertEqual(c.data['initial_pass_completed'],3)
    self.assertEqual(c.similarity_start,4)
    self.add(c,4);c.rendered();self.assertEqual(c.public()['completed_count'],5)
    self.assertEqual(hashes,[digest(x['path']) for x in c.data['files'][:3]])
   finally:c.close()
 def test_four_plus_one_and_duplicate_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   c=self.checkpoint(d)
   try:
    for i in range(4):self.add(c,i)
    self.assertTrue(self.retry(c))
    p=c.root/'duplicate.mp4';p.write_bytes(Path(c.data['files'][0]['path']).read_bytes())
    with self.assertRaisesRegex(FillCheckpointError,'复制'):c.publish(p,c.root/'duplicate-final.mp4',c.root/'missing',c.root/'report',{})
    self.add(c,4);c.rendered();self.assertEqual(c.public()['state'],'completed')
   finally:c.close()
 def test_retry_limit_and_incomplete_not_completed(self):
  with tempfile.TemporaryDirectory() as d:
   c=self.checkpoint(d)
   try:
    for i in range(3):self.add(c,i)
    for _ in range(3):self.assertTrue(self.retry(c))
    self.assertFalse(self.retry(c))
    with self.assertRaises(FillCheckpointError):c.rendered()
   finally:c.close()
if __name__=='__main__':unittest.main()
