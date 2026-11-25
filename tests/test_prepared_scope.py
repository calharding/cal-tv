import copy,tempfile,unittest,sys,importlib.util
from pathlib import Path
from media_automation.prepared_scope import validate_prepared_scope
from media_automation.importer import identity_seal
from media_automation.core import ReviewRequired
class PreparedScopeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.source=self.root/'one';self.source.mkdir();self.original=self.root/'original.mkv';self.prepared=self.source/'prepared.mkv';self.original.write_bytes(b'fixture');self.prepared.write_bytes(b'fixture')
  self.scope=dict(schema_version=1,provider_id='tmdb:fixture',season=4,episode=13,maximum_height=480,bytes=7,approval_reference='fixture-specific-approval',original_path=str(self.original),prepared_path=str(self.prepared),original_identity_seal=identity_seal(self.original),prepared_identity_seal=identity_seal(self.prepared))
  self.config={'prepared_scope_enabled':True,'prepared_source_scopes':[self.scope]}
  self.req=dict(kind='season',provider_id='tmdb:fixture',season=4,episodes=[13],max_height=480,size_exception_approval='fixture-specific-approval',episode_size_exception=dict(schema_version=1,provider_id='tmdb:fixture',season=4,episode=13,maximum_bytes=7,maximum_height=720,approval_reference='fixture-specific-approval'))
 def tearDown(self):self.tmp.cleanup()
 def test_exact_and_no_mutation(self):
  before=copy.deepcopy((self.config,self.req));self.assertEqual(validate_prepared_scope(self.config,self.req,self.source),self.scope);self.assertEqual((self.config,self.req),before)
 def test_unlisted_root_not_granted(self):self.assertIsNone(validate_prepared_scope(self.config,self.req,self.root))
 def test_inactive(self):
  self.config['prepared_scope_enabled']=False
  with self.assertRaises(ReviewRequired):validate_prepared_scope(self.config,self.req,self.source)
 def test_height_not_relaxed_by_720_enum(self):
  self.req['max_height']=720
  with self.assertRaises(ReviewRequired):validate_prepared_scope(self.config,self.req,self.source)
 def test_wrong_episode(self):
  self.req['episodes']=[12]
  with self.assertRaises(ReviewRequired):validate_prepared_scope(self.config,self.req,self.source)
 def test_larger_cap(self):
  self.req['episode_size_exception']['maximum_bytes']=8
  with self.assertRaises(ReviewRequired):validate_prepared_scope(self.config,self.req,self.source)
 def test_changed_prepared(self):
  self.prepared.write_bytes(b'changed-size')
  with self.assertRaises(ReviewRequired):validate_prepared_scope(self.config,self.req,self.source)
 def test_changed_original(self):
  self.original.write_bytes(b'changed-size')
  with self.assertRaises(ReviewRequired):validate_prepared_scope(self.config,self.req,self.source)
 def test_symlink(self):
  self.prepared.unlink();self.prepared.symlink_to(self.original)
  with self.assertRaises(ReviewRequired):validate_prepared_scope(self.config,self.req,self.source)
 def test_duplicate_scope(self):
  self.config['prepared_source_scopes'].append(copy.deepcopy(self.scope))
  with self.assertRaises(ReviewRequired):validate_prepared_scope(self.config,self.req,self.source)
if __name__=='__main__':unittest.main()

class RemuxScopeTests(unittest.TestCase):
 def setUp(self):
  from unittest.mock import patch
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.src=self.root/'original.mkv';self.src.write_bytes(b'original');self.folder=self.root/'prepared';self.folder.mkdir();self.dst=self.folder/'episode.mkv';self.dst.write_bytes(b'remuxchangedbytes')
  self.req=dict(kind='season',provider_id='tmdb:fixture',season=1,episodes=[2],max_height=480)
  self.scope=dict(schema_version=2,mode='light_stream_copy',provider_id='tmdb:fixture',season=1,episode=2,maximum_height=480,actual_height=480,output_cap_bytes=20,duration_seconds=1200,policy_sha256='fixture',original_path=str(self.src),prepared_path=str(self.dst),original_identity_seal=identity_seal(self.src),prepared_identity_seal=identity_seal(self.dst),approval_reference='explicit-preparation',preparation_approval_reference='explicit-preparation',structural_check_passed=True,full_av_integrity_proved=False)
  self.cfg=dict(prepared_scope_enabled=True,prepared_source_scopes=[self.scope]);self.policy={'maximum_resolution':{'height':1080},'television':{'resolution_caps':{'720p_or_lower':{'over_15_up_to_30_minutes_bytes':20}}}};self.patch=patch('media_automation.quality_policy.load_policy',return_value=(self.policy,'fixture'));self.patch.start()
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def validate(self):return validate_prepared_scope(self.cfg,self.req,self.folder)
 def test_distinct_sizes_admitted(self):self.assertEqual(self.validate(),self.scope);self.assertNotEqual(self.src.stat().st_size,self.dst.stat().st_size)
 def test_output_cap_not_expanded(self):
  self.scope['output_cap_bytes']=21
  with self.assertRaises(ReviewRequired):self.validate()
 def test_output_exceeds_cap(self):
  self.scope['output_cap_bytes']=10
  with self.assertRaises(ReviewRequired):self.validate()
 def test_original_seal_changed(self):
  self.src.write_bytes(b'changed')
  with self.assertRaises(ReviewRequired):self.validate()
 def test_prepared_seal_changed(self):
  self.dst.write_bytes(b'changed')
  with self.assertRaises(ReviewRequired):self.validate()
 def test_policy_changed(self):
  self.scope['policy_sha256']='different'
  with self.assertRaises(ReviewRequired):self.validate()
 def test_truthful_proof(self):
  self.scope['full_av_integrity_proved']=True
  with self.assertRaises(ReviewRequired):self.validate()
 def test_wrong_episode(self):
  self.req['episodes']=[3]
  with self.assertRaises(ReviewRequired):self.validate()
 def test_explicit_exception(self):
  self.scope['output_cap_bytes']=30;self.req['size_exception_approval']='user-specific';self.req['episode_size_exception']=dict(schema_version=1,provider_id='tmdb:fixture',season=1,episode=2,maximum_bytes=30,maximum_height=720,approval_reference='user-specific');self.validate()
 def test_builder_binds_limit(self):
  from media_automation.prepared_scope import remux_scope_from_result
  result=dict(status='PREPARED',mode='STREAM_COPY',maximum_bytes=20,path=str(self.dst),source_identity_seal=identity_seal(self.src),prepared_identity_seal=identity_seal(self.dst),all_tracks_preserved_structurally=True,full_av_integrity_proved=False,probe={'format':{'duration':'1200'},'streams':[{'codec_type':'video','height':480}]})
  self.cfg['prepared_source_scopes']=[remux_scope_from_result(self.req,self.src,result,output_cap_bytes=20,approval_reference='explicit',policy_sha256='fixture')];self.validate()
  with self.assertRaises(ReviewRequired):remux_scope_from_result(self.req,self.src,result,output_cap_bytes=30,approval_reference='explicit',policy_sha256='fixture')
