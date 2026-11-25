import unittest,tempfile,json,hashlib,fcntl,sqlite3
from pathlib import Path
from media_automation.backlog_queue import run,validate,request_stop
from media_automation.core import ReviewRequired
class Queue(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.items=[]
  for n in range(3):
   doc={'schema_version':1,'config':{'backlog_shared_store_root':str(self.root/'store'),'backlog_horizon_policy':'fresh-under-locks','backlog_fast_enabled':True,'manual_service_writes_enabled':True},'job_id':str(n),'approval_reference':'fixture','request':{'kind':'movie','title':'Fixture'+str(n),'year':2000,'provider_id':'tmdb:fixture'+str(n+1)},'source':str(self.root/'source'),'plan':{}}
   p=self.root/(str(n)+'.json');p.write_text(json.dumps(doc));self.items.append({'scope_path':str(p),'job_id':str(n),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
  self.manifest={'schema_version':1,'queue_execution_enabled':True,'queue_root':str(self.root/'queue'),'items':self.items};self.calls=[]
 def tearDown(self):self.tmp.cleanup()
 def ready(self,doc):self.calls.append(doc['job_id']);return {'state':'READY','result':{'fixture':True}}
 def test_ordered_idempotent(self):
  self.assertEqual(run(self.manifest,executor=self.ready)['completed'],3);self.assertEqual(self.calls,['0','1','2'])
  self.assertEqual(run(self.manifest,executor=self.ready)['status'],'COMPLETE');self.assertEqual(self.calls,['0','1','2'])
 def test_stop_after_current(self):
  def execute(doc):request_stop(self.manifest);return self.ready(doc)
  self.assertEqual(run(self.manifest,executor=execute)['status'],'STOPPED');self.assertEqual(self.calls,['0'])
  self.assertEqual(run(self.manifest,executor=self.ready)['status'],'STOPPED');self.assertEqual(self.calls,['0'])
 def test_stop_before_start_and_repeated(self):
  request_stop(self.manifest);request_stop(self.manifest)
  self.assertEqual(run(self.manifest,executor=self.ready)['status'],'STOPPED');self.assertEqual(self.calls,[])
 def test_stop_during_scan_wait_does_not_resume_or_start_next(self):
  def waiting(doc):
   self.calls.append(doc['job_id']);request_stop(self.manifest);return {'state':'WAITING'}
  self.assertEqual(run(self.manifest,executor=waiting)['status'],'WAITING')
  self.assertEqual(run(self.manifest,executor=self.ready)['status'],'STOPPED')
  self.assertEqual(self.calls,['0'])
 def test_wait_no_next_then_resume(self):
  first=[True]
  def execute(doc):
   if first[0]:first[0]=False;self.calls.append('waiting:'+doc['job_id']);return {'state':'WAITING'}
   return self.ready(doc)
  self.assertEqual(run(self.manifest,executor=execute)['status'],'WAITING');self.assertEqual(self.calls,['waiting:0'])
  self.assertEqual(run(self.manifest,executor=execute)['status'],'COMPLETE');self.assertEqual(self.calls,['waiting:0','0','1','2'])
 def test_failure_blocks_no_retry(self):
  def failure(doc):self.calls.append(doc['job_id']);raise ReviewRequired('uncertain')
  with self.assertRaises(ReviewRequired):run(self.manifest,executor=failure)
  self.assertEqual(run(self.manifest,executor=self.ready)['status'],'BLOCKED');self.assertEqual(self.calls,['0'])
 def test_pinned_horizon_refused_before_queue(self):
  p=Path(self.items[0]['scope_path']);doc=json.loads(p.read_text());doc['config']['backlog_expected_horizons']={}
  p.write_text(json.dumps(doc));self.items[0]['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
  with self.assertRaises(ReviewRequired):run(self.manifest,executor=self.ready)
  self.assertEqual(self.calls,[])
 def test_changed_scope_rejected(self):
  Path(self.items[1]['scope_path']).write_text('{}')
  with self.assertRaises(ReviewRequired):run(self.manifest,executor=self.ready)
  self.assertEqual(self.calls,[])
 def test_disabled_no_action(self):
  self.manifest['queue_execution_enabled']=False
  with self.assertRaises(ReviewRequired):run(self.manifest,executor=self.ready)
  self.assertEqual(self.calls,[])
 def test_crash_after_effect_uses_idempotent_coordinator(self):
  effects=set()
  def execute(doc):effects.add(doc['job_id']);return self.ready(doc)
  def crash(phase,index):
   if phase=='EXECUTED' and index==0:raise KeyboardInterrupt()
  with self.assertRaises(KeyboardInterrupt):run(self.manifest,executor=execute,checkpoint=crash)
  self.assertEqual(run(self.manifest,executor=execute)['status'],'COMPLETE');self.assertEqual(effects,{'0','1','2'});self.assertEqual(self.calls,['0','0','1','2'])
 def test_unknown_persisted_state_rejected(self):
  run(self.manifest,executor=lambda doc:{'state':'WAITING'})
  db=sqlite3.connect(Path(self.manifest['queue_root'])/'queue.sqlite3')
  try:
   db.execute("UPDATE items SET state='UNKNOWN_NEW_STATE' WHERE position=0");db.commit()
  finally:db.close()
  with self.assertRaises(ReviewRequired):run(self.manifest,executor=self.ready)
  self.assertEqual(self.calls,[])
 def test_queue_lock_contention(self):
  root=Path(self.manifest['queue_root']);root.mkdir()
  with (root/'queue.lock').open('a') as held:
   fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaises(ReviewRequired):run(self.manifest,executor=self.ready)
  self.assertEqual(self.calls,[])
 def test_stop_still_works_with_changed_scope(self):
  Path(self.items[0]['scope_path']).write_text('{}');self.assertEqual(request_stop(self.manifest)['status'],'STOP_REQUESTED')
if __name__=='__main__':unittest.main()
