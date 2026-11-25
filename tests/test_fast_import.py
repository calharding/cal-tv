import importlib.util,sys,tempfile,unittest,os,json,time
from pathlib import Path
from unittest.mock import patch
from media_automation import importer as imp
from media_automation.core import Store,ReviewRequired

def probe(p):
 return {'format':{'duration':'1200'},'streams':[{'codec_type':'video','index':0,'width':640,'height':480},{'codec_type':'audio','index':1,'tags':{'language':'eng'}}]}
class FastTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.src=self.root/'source';self.media=self.root/'media';self.src.mkdir();self.media.mkdir();self.file=self.src/'film.mkv';self.file.write_bytes(b'fixture'*100);self.file.chmod(0o640);self.store=Store(self.root/'db');self.job={'id':'test','request':{'kind':'movie','title':'Fixture','year':2000,'provider_id':'tmdb:fixture','audible_language_confirmed':True}}
 def tearDown(self):self.store.db.close();self.tmp.cleanup()
 def runjob(self,**kw):
  with patch.object(imp,'sha256',side_effect=AssertionError('unexpected full hash')),patch.object(imp.subprocess,'run',side_effect=AssertionError('unexpected decode')):
   return imp.import_job(self.store,self.job,self.src,self.media,probe_fn=probe,screening='light',**kw)
 def test_fast_and_recovery(self):
  st=self.file.stat();rows=self.runjob();dst=Path(rows[0]['destination']);self.assertTrue(os.path.samefile(self.file,dst));self.assertEqual(self.file.stat().st_mode,st.st_mode);self.assertEqual(self.file.stat().st_mtime_ns,st.st_mtime_ns);self.assertEqual(rows[0]['sha256'],'');self.assertFalse(rows[0]['probe']['decode_samples_passed']);self.assertTrue(self.runjob()[0]['recovered'])
 def test_collision(self):
  dst=self.media/'Movies/Fixture (2000)/Fixture (2000).mkv';dst.parent.mkdir(parents=True);dst.write_bytes(b'other')
  with self.assertRaises(ReviewRequired):self.runjob()
  self.assertEqual(dst.read_bytes(),b'other')
 def test_probe_mutation(self):
  def mutated(p):p.write_bytes(b'changed');return probe(p)
  with self.assertRaises(ReviewRequired):imp.import_job(self.store,self.job,self.src,self.media,probe_fn=mutated,screening='light')
 def test_uncertain_link_recovery(self):
  def uncertain(src,dst):os.link(src,dst);raise RuntimeError('lost after link')
  with self.assertRaises(RuntimeError):self.runjob(link_fn=uncertain)
  self.assertTrue(self.runjob()[0]['recovered'])
 def test_mutated_recovery(self):
  self.runjob();self.file.write_bytes(b'mutated linked media')
  with self.assertRaises(ReviewRequired):self.runjob()
 def test_wrong_job_recovery(self):
  self.runjob();self.job['id']='other'
  with self.assertRaises(ReviewRequired):self.runjob()
 def test_subtitle_default(self):
  def subs(p):r=probe(p);r['streams'].append({'codec_type':'subtitle','tags':{'language':'fra'},'disposition':{'default':1}});return r
  with self.assertRaises(ReviewRequired):imp.import_job(self.store,self.job,self.src,self.media,probe_fn=subs,screening='light')
 def test_symlink(self):
  target=self.root/'elsewhere';self.file.rename(target);self.file.symlink_to(target)
  with self.assertRaises(ReviewRequired):self.runjob()
 def test_unlinked_intent_changed(self):
  def fail(src,dst):raise RuntimeError('before link')
  with self.assertRaises(RuntimeError):self.runjob(link_fn=fail)
  self.file.write_bytes(b'changed')
  with self.assertRaises(ReviewRequired):self.runjob()
 def test_missing_linked_destination(self):
  row=self.runjob()[0];Path(row['destination']).unlink()
  with self.assertRaises(ReviewRequired):self.runjob()
 def test_foreign_default_audio(self):
  def foreign(p):
   r=probe(p);r['streams'][1]['disposition']={'default':0};r['streams'].append({'codec_type':'audio','index':2,'tags':{'language':'fra'},'disposition':{'default':1}});return r
  with self.assertRaises(ReviewRequired):imp.import_job(self.store,self.job,self.src,self.media,probe_fn=foreign,screening='light')
 def test_ambiguous_audio(self):
  def multi(p):r=probe(p);r['streams'].append({'codec_type':'audio','index':2,'tags':{'language':'eng'}});return r
  with self.assertRaises(ReviewRequired):imp.import_job(self.store,self.job,self.src,self.media,probe_fn=multi,screening='light')
 def test_subtitle_mutation_during_hash(self):
  sub=self.src/'film.srt';sub.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello\n')
  self.job['request']['subtitle_mappings']=[{'source':'film.srt','video':'film.mkv','language':'eng','review_approval':'fixture','usable':True,'matching':True}]
  def mutation(p):p.write_text('changed');return 'old-digest'
  with patch.object(imp,'sha256',side_effect=mutation),self.assertRaises(ReviewRequired):
   imp.import_job(self.store,self.job,self.src,self.media,probe_fn=probe,screening='light')
 def test_recovered_file_mutated_by_later_probe(self):
  self.file.rename(self.src/'Fixture S01E01.mkv');second=self.src/'Fixture S01E02.mkv';second.write_bytes(b'second')
  self.job['request'].update(kind='season',season=1,episodes=[1,2],episode_durations={'1':1200,'2':1200})
  self.runjob()
  def mutate_later(p):
   if p==second:(self.src/'Fixture S01E01.mkv').write_bytes(b'changed-after-first-probe')
   return probe(p)
  with self.assertRaises(ReviewRequired):imp.import_job(self.store,self.job,self.src,self.media,probe_fn=mutate_later,screening='light')
 def test_scaling(self):
  records=[]
  self.file.unlink()
  for n in (1,2,4,8):
   src=self.root/f'src{n}';src.mkdir();media=self.root/f'lib{n}';media.mkdir()
   for e in range(1,n+1):(src/f'Fixture S01E{e:02}.mkv').write_bytes(b'fixture'*100)
   req={'kind':'season','title':'Fixture','year':2000,'provider_id':'tmdb:fixture','season':1,'episodes':list(range(1,n+1)),'episode_durations':{str(e):1200 for e in range(1,n+1)},'audible_language_confirmed':True};calls=[]
   def count(p):calls.append(p);return probe(p)
   start=time.perf_counter()
   with patch.object(imp,'sha256',side_effect=AssertionError('full hash')),patch.object(imp.subprocess,'run',side_effect=AssertionError('decode')):
    rows=imp.import_job(self.store,{'id':f'scale{n}','request':req},src,media,probe_fn=count,screening='light')
   self.assertEqual(len(calls),n);self.assertEqual(len(rows),n);records.append({'items':n,'probe_calls':len(calls),'media_hash_calls':0,'decode_calls':0,'seconds':round(time.perf_counter()-start,4)})
  print('SCALING '+json.dumps(records))
if __name__=='__main__':unittest.main(verbosity=2)
