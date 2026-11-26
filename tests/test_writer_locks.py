# Check that contention on any writer role unwinds locks already held.
import unittest,tempfile,fcntl
from pathlib import Path
from media_automation.core import Store,ReviewRequired
from media_automation.backlog_once import writer_locks
class WriterLocks(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.store=Store(self.root/'store')
  self.paths={'root':self.store.root/'worker.lock','manual':self.root/'manual.lock','batch':self.root/'batch.lock','films':self.root/'films.lock'}
  for p in self.paths.values():p.touch()
  self.cfg={'backlog_writer_locks':{k:str(v) for k,v in self.paths.items()}}
 def tearDown(self):self.store.db.close();self.tmp.cleanup()
 def assert_free(self,except_path=None):
  for p in self.paths.values():
   if p==except_path:continue
   with p.open('r+') as f:
    fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(f,fcntl.LOCK_UN)
 def test_each_contention_releases_partial(self):
  for p in self.paths.values():
   with self.subTest(path=p),p.open('r+') as held:
    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with self.assertRaises(ReviewRequired):
     with writer_locks(self.store,self.cfg):self.fail('must not enter')
    self.assert_free(except_path=p)
   self.assert_free()
 def test_success_and_exception_release(self):
  with self.assertRaises(KeyboardInterrupt):
   with writer_locks(self.store,self.cfg):
    for p in self.paths.values():
     with p.open('r+') as f:
      with self.assertRaises(BlockingIOError):fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    raise KeyboardInterrupt()
  self.assert_free()
  with writer_locks(self.store,self.cfg):pass
  self.assert_free()
 def test_missing_role_rejected(self):
  self.cfg['backlog_writer_locks'].pop('films')
  with self.assertRaises(ReviewRequired):
   with writer_locks(self.store,self.cfg):pass
  self.assert_free()
 def test_alias_rejected(self):
  self.cfg['backlog_writer_locks']['films']=str(self.paths['root'])
  with self.assertRaises(ReviewRequired):
   with writer_locks(self.store,self.cfg):pass
  self.assert_free()
if __name__=='__main__':unittest.main()
