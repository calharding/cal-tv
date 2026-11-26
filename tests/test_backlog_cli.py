# Check the offline/live boundary and exact job binding with injected adapters.
import unittest,tempfile,types,json
from pathlib import Path
from unittest.mock import patch
from media_automation import backlog_cli as cli
from media_automation.core import Store,ReviewRequired
class Launcher(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.r=Path(self.t.name).resolve();self.store=Store(self.r/'store')
  req={'kind':'movie','title':'Fixture','year':2000,'provider_id':'tmdb:fixture'};job=self.store.create(req);self.store.change(job['id'],'AUTHORIZED',approval='synthetic')
  source=self.r/'source';source.mkdir();(source/'film.mkv').write_bytes(b'fixture')
  self.doc={'schema_version':1,'config':{'backlog_shared_store_root':str(self.store.root),'media_root':str(self.r/'media'),'complete_root':str(source),'backlog_journals':{'root':str(self.store.root/'jobs.sqlite3')}},'job_id':job['id'],'request':req,'approval_reference':'synthetic','source':str(source),'plan':{'provider_id':'tmdb:fixture'}}
  self.path=self.r/'scope.json';self.path.write_text(json.dumps(self.doc))
 def tearDown(self):self.store.db.close();self.t.cleanup()
 def test_existing_store_does_not_migrate_or_chmod(self):
  dbpath=self.store.root/'jobs.sqlite3';before=dbpath.stat();schema=self.store.db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall()
  with patch('os.chmod',side_effect=AssertionError('no chmod')),patch('pathlib.Path.mkdir',side_effect=AssertionError('no mkdir')):
   attached=cli.ExistingStore(self.store.root)
   self.assertEqual(attached.db.total_changes,0);attached.db.close()
  after=dbpath.stat();self.assertEqual((before.st_mode,before.st_mtime_ns),(after.st_mode,after.st_mtime_ns));self.assertEqual(schema,self.store.db.execute('SELECT sql FROM sqlite_master ORDER BY name').fetchall())
 def test_offline_no_services(self):
  doc=cli.load_scope(str(self.path));out=cli.execute_scope(doc,'validate',services_factory=lambda *a:self.fail('network must remain off'));self.assertFalse(out['activated'])
 def test_execute_disabled(self):
  with self.assertRaises(ReviewRequired):cli.execute_scope(self.doc,'execute',services_factory=lambda *a:self.fail('must gate first'))
 def test_wrong_approval(self):
  self.doc['approval_reference']='other'
  with self.assertRaises(ReviewRequired):cli.execute_scope(self.doc,'check',services_factory=lambda *a:self.fail('must gate first'))
 def test_execution_calls_coordinator_only(self):
  self.doc['config'].update(backlog_fast_enabled=True,manual_service_writes_enabled=True)
  service=object()
  with patch.object(cli,'run',return_value={'status':'fixture'}) as run:
   self.assertEqual(cli.execute_scope(self.doc,'execute',services_factory=lambda *a:(service,None)),{'status':'fixture'})
   self.assertIs(run.call_args.args[-1],service)
 def test_preview_read_only_call_path(self):
  services=types.SimpleNamespace(_item=lambda p:{'tmdbId':1});jelly=object()
  with patch.object(cli,'load_policy',return_value=({},'digest')),patch.object(cli,'reconcile',return_value={'safe':True}) as reconcile,patch.object(cli,'ExistingStore',side_effect=AssertionError('must not instantiate writable Store')):
   result=cli.execute_scope(self.doc,'check',services_factory=lambda *a:(services,jelly))
   self.assertEqual(result['status'],'READ_ONLY_PREVIEW');self.assertEqual(reconcile.call_count,1)
if __name__=='__main__':unittest.main()
