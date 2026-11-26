# Verify that inheriting service references cannot activate an import.
import copy,unittest,sys,importlib.util
from pathlib import Path
from media_automation.scope_config import scoped_config
from media_automation.core import ReviewRequired
class ScopedConfigTests(unittest.TestCase):
 def setUp(self):
  self.profile={k:'/fixture/'+k for k in ('media_root','complete_root','quality_policy_path','quality_policy_sha256','storage_guard_script','storage_guard_config')}
  self.profile.update(targeted_refresh_permitted=False,jellyfin_enabled=False)
  self.live=self.profile|{'quality_policy_version':'1.2.0','targeted_refresh_permitted':True,'jellyfin_enabled':True,'jellyfin_url':'https://jellyfin.example.invalid','jellyfin_key_file':'/fixture/key','secret_token':'synthetic-placeholder'}
 def test_inherit_without_activate_or_mutate(self):
  before=copy.deepcopy((self.profile,self.live));c=scoped_config(self.profile,self.live,'/review/run')
  self.assertTrue(c['targeted_refresh_permitted']);self.assertTrue(c['jellyfin_enabled']);self.assertEqual(c['quality_policy_version'],'1.2.0');self.assertFalse(c['backlog_fast_enabled']);self.assertFalse(c['manual_service_writes_enabled']);self.assertNotIn('secret_token',c);self.assertEqual((self.profile,self.live),before)
 def test_preserve_denial(self):
  self.live['targeted_refresh_permitted']=False;self.assertFalse(scoped_config(self.profile,self.live,'/review/run')['targeted_refresh_permitted'])
 def test_missing_permission_not_granted(self):
  del self.live['targeted_refresh_permitted'];self.assertFalse(scoped_config(self.profile,self.live,'/review/run')['targeted_refresh_permitted'])
 def test_truthy_string_rejected(self):
  self.live['targeted_refresh_permitted']='true'
  with self.assertRaises(ReviewRequired):scoped_config(self.profile,self.live,'/review/run')
 def test_changed_policy_rejected(self):
  self.live['quality_policy_sha256']='changed'
  with self.assertRaises(ReviewRequired):scoped_config(self.profile,self.live,'/review/run')
 def test_existing_enabled_profile_cannot_activate(self):
  self.profile.update(backlog_fast_enabled=True,manual_service_writes_enabled=True,prepared_scope_enabled=True)
  c=scoped_config(self.profile,self.live,'/review/run');self.assertFalse(c['backlog_fast_enabled']);self.assertFalse(c['manual_service_writes_enabled']);self.assertFalse(c['prepared_scope_enabled'])
if __name__=='__main__':unittest.main()
