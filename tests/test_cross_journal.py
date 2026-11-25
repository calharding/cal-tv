import unittest,tempfile,types
from pathlib import Path
from media_automation.core import Store,ReviewRequired
from media_automation.importer import identity_seal
from media_automation.cross_journal import reconcile,_overlaps
class Reconcile(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();r=Path(self.tmp.name).resolve();self.r=r
  self.stores={role:Store(r/role) for role in ['root','manual','batch','films']}
  self.paths={k:str(v.root/'jobs.sqlite3') for k,v in self.stores.items()}
  src=r/'prepared'/'Film.mkv';src.parent.mkdir();src.write_bytes(b'fixture')
  self.scope={'request':dict(kind='movie',title='Film',year=2000,provider_id='tmdb:fixture'),'files':[dict(source=str(src),prepared_root=str(src.parent),identity_seal=identity_seal(src),destination=str(r/'media'/'Film.mkv'),alternate_destinations=[str(r/'media'/'Film.mp4')],episode=None)],'plan':{'files':[{'path':'/fixture/library/Film.mkv','episode_ids':[]}]},'admission_jobs':[]}
  self.services=types.SimpleNamespace(read_arr=lambda p:{'files':[]});self.jelly=types.SimpleNamespace(owned=lambda r:False)
 def tearDown(self):
  for s in self.stores.values():s.db.close()
  self.tmp.cleanup()
 def check(self):return reconcile(self.paths,self.scope,services=self.services,jellyfin=self.jelly)
 def test_staged_pair_overlap(self):
  a={'kind':'season','provider_id':'tmdb:fixture','season':1,'episodes':[9],'combined_episode_staging':{'start':9,'end':10}}
  b={'kind':'season','provider_id':'tmdb:fixture','season':1,'episodes':[10]}
  self.assertTrue(_overlaps(a,b))
  a.pop('season')
  with self.assertRaises(ReviewRequired):_overlaps(a,b)
 def test_alias_overlap(self):
  a={'kind':'movie','provider_id':'imdb:tt999999999999'};b={'kind':'movie','provider_id':'tmdb:fixture'}
  self.assertTrue(_overlaps(a,b,['tmdb:fixture','imdb:tt999999999999']))
 def test_clear(self):self.assertTrue(self.check()['safe'])
 def test_authorized_exact(self):
  s=self.stores['root'];job=s.create(self.scope['request']);s.change(job['id'],'AUTHORIZED',approval='fixture');self.scope['admission_jobs']=[dict(role='root',id=job['id'],state='AUTHORIZED')];self.assertTrue(self.check()['safe'])
 def test_blocked_default_and_explicit_exact_recovery(self):
  s=self.stores['root'];j=s.create(self.scope['request']);s.change(j['id'],'MANUAL_BLOCKED',approval='fixture')
  self.scope['admission_jobs']=[dict(role='root',id=j['id'],state='MANUAL_BLOCKED')]
  self.assertFalse(self.check()['safe'])
  raw=dict(s.db.execute('SELECT * FROM jobs WHERE id=?',(j['id'],)).fetchone())
  self.assertTrue(reconcile(self.paths,self.scope,services=self.services,jellyfin=self.jelly,recovery_snapshot=dict(role='root',job=raw))['safe'])
  s.change(j['id'],'MANUAL_BLOCKED',approval='changed')
  self.assertFalse(reconcile(self.paths,self.scope,services=self.services,jellyfin=self.jelly,recovery_snapshot=dict(role='root',job=raw))['safe'])
 def test_recovery_snapshot_does_not_allow_existing_effect(self):
  s=self.stores['root'];j=s.create(self.scope['request']);s.change(j['id'],'MANUAL_BLOCKED',approval='fixture')
  self.scope['admission_jobs']=[dict(role='root',id=j['id'],state='MANUAL_BLOCKED')]
  raw=dict(s.db.execute('SELECT * FROM jobs WHERE id=?',(j['id'],)).fetchone())
  with s.db:s.db.execute('INSERT INTO imports VALUES(?,?,?,?,?,?,?)',(j['id'],'other','other','',9,9,'INTENT'))
  self.assertFalse(reconcile(self.paths,self.scope,services=self.services,jellyfin=self.jelly,recovery_snapshot=dict(role='root',job=raw))['safe'])
 def test_ready_duplicate_other_store(self):
  s=self.stores['manual'];job=s.create(self.scope['request']);s.change(job['id'],'READY');self.assertFalse(self.check()['safe'])
 def test_unknown_job_state(self):
  s=self.stores['batch'];job=s.create(self.scope['request']);s.change(job['id'],'SUBMITTING');self.assertFalse(self.check()['safe'])
 def test_inode_overlap_other_path(self):
  f=self.scope['files'][0];seal=f['identity_seal'];s=self.stores['films'];job=s.create(self.scope['request'])
  with s.db:s.db.execute('INSERT INTO imports VALUES (?,?,?,?,?,?,?)',(job['id'],'/different','/alternate','',seal[0],seal[1],'INTENT'))
  self.assertFalse(self.check()['safe'])
 def test_existing_alternate(self):
  p=Path(self.scope['files'][0]['alternate_destinations'][0]);p.parent.mkdir();p.write_bytes(b'old');self.assertFalse(self.check()['safe'])
 def test_source_change(self):
  Path(self.scope['files'][0]['source']).write_bytes(b'changed')
  with self.assertRaises(ReviewRequired):self.check()
 def test_live_unknown(self):self.assertFalse(reconcile(self.paths,self.scope)['safe'])
 def test_live_arr_overlap(self):
  self.services.read_arr=lambda p:{'files':[{'path':'elsewhere','episode_ids':[]}]};self.assertFalse(self.check()['safe'])
 def test_read_only(self):
  before={k:list(s.db.execute('SELECT * FROM jobs')) for k,s in self.stores.items()};self.check();self.assertEqual(before,{k:list(s.db.execute('SELECT * FROM jobs')) for k,s in self.stores.items()})
if __name__=='__main__':unittest.main()
