import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cloud_recovery import CloudError,CloudRecovery,CredentialStore,EcsApi


class Clock:
    def __init__(self): self.value=1000000
    def time(self): return self.value
    def monotonic(self): return self.value


class Store:
    def __init__(self,exists=True): self.ready=exists;self.credentials={}
    def exists(self): return self.ready
    def load(self): return self.credentials
    def save(self,value): self.credentials=value;self.ready=True


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.clock=Clock()
        self.api=Mock()
        self.api.describe.return_value={'Status':'Running'}
        self.api.reboot.return_value={'RequestId':'test-request'}
        self.store=Store()
        self.healthy=Mock(return_value=False)
        self.probe=Mock(return_value='unresponsive')
        self.reconnect=Mock()
        self.config={'cloud_recovery':{'enabled':True,'region_id':'cn-hangzhou',
            'instance_id':'i-test','expected_ip':'YOUR_SERVER_IP','outage_seconds':20,
            'failure_attempts':3,'cooldown_seconds':3600,'max_reboots_24h':2,'startup_grace_seconds':600}}
        self.engine=self.new_engine()

    def new_engine(self):
        return CloudRecovery(self.config,self.root,self.healthy,self.probe,self.reconnect,
            store=self.store,clock=self.clock,api_factory=lambda *_:self.api)

    def elapse(self,seconds,engine=None):
        engine=engine or self.engine
        while seconds:
            step=min(seconds,5);seconds-=step;self.clock.value+=step;engine.check_once()

    def fault(self,engine=None):
        engine=engine or self.engine
        for _ in range(3):
            engine.note_failure('SSH timeout')
            self.elapse(5,engine)
        self.elapse(6,engine)

    def test_eligible_outage_sends_one_normal_reboot(self):
        self.engine.note_healthy();self.fault()
        self.api.reboot.assert_called_once_with('i-test')
        self.assertEqual(self.engine.ledger['reboots'][0]['outcome'],'accepted')
        self.elapse(1800)
        self.api.reboot.assert_called_once()

    def test_no_reboot_before_fault_threshold(self):
        self.engine.note_healthy();self.engine.note_failure('SSH timeout');self.elapse(19)
        self.api.reboot.assert_not_called()
        self.api.describe.assert_not_called()

    def test_no_reboot_without_prior_successful_connection(self):
        self.fault();self.api.describe.assert_not_called()

    def test_missing_credentials_blocks_all_cloud_calls(self):
        self.store.ready=False;self.engine.note_healthy();self.fault()
        self.api.describe.assert_not_called()
        self.assertEqual(self.engine.public_status()['phase'],'unconfigured')

    def test_authentication_error_never_causes_reboot(self):
        self.engine.note_healthy()
        for _ in range(8):
            self.engine.note_failure('SSH 密钥认证未通过');self.elapse(5)
        self.api.describe.assert_not_called()

    def test_healthy_independent_probe_reconnects_without_reboot(self):
        self.probe.return_value='healthy';self.engine.note_healthy();self.fault()
        self.reconnect.assert_called_once();self.api.reboot.assert_not_called()

    def test_local_probe_configuration_error_does_not_reboot(self):
        self.probe.return_value='configuration_error';self.engine.note_healthy();self.fault()
        self.api.reboot.assert_not_called()

    def test_api_network_failure_never_reboots(self):
        self.api.describe.side_effect=CloudError('NetworkUnavailable')
        self.engine.note_healthy();self.fault()
        self.api.reboot.assert_not_called()
        self.assertEqual(self.engine.failures,0)

    def test_starting_or_stopped_instance_is_not_rebooted(self):
        for status in ['Starting','Stopped','Stopping']:
            with self.subTest(status=status):
                self.api.describe.return_value={'Status':status}
                self.engine.note_healthy();self.fault()
                self.api.reboot.assert_not_called()
                self.assertGreater(self.engine.ledger['grace_until'],self.clock.time())

    def test_cooldown_and_count_survive_manager_restart(self):
        self.engine.note_healthy();self.fault()
        restarted=self.new_engine();restarted.note_healthy();self.fault(restarted)
        self.api.reboot.assert_called_once()
        self.assertEqual(restarted.public_status()['reboots_24h'],1)
        self.assertEqual(restarted.public_status()['phase'],'cooldown')

    def test_daily_limit_applies_after_cooldown(self):
        self.engine.note_healthy();self.fault()
        for _ in range(2):
            self.engine.note_healthy();self.elapse(3601);self.fault()
        self.assertEqual(self.api.reboot.call_count,2)
        self.assertEqual(self.engine.public_status()['phase'],'daily_limit')

    def test_ambiguous_reboot_response_does_not_retry_power_action(self):
        self.api.reboot.side_effect=CloudError('NetworkUnavailable')
        self.engine.note_healthy();self.fault();self.elapse(7200)
        self.api.reboot.assert_called_once()
        self.assertEqual(self.engine.ledger['reboots'][0]['outcome'],'unknown')

    def test_corrupt_ledger_fails_closed(self):
        (self.root/'cloud-recovery-state.json').write_text('{broken')
        engine=self.new_engine();engine.note_healthy();self.fault(engine)
        self.api.reboot.assert_not_called()
        self.assertEqual(engine.public_status()['phase'],'ledger_error')

    def test_failed_ledger_save_prevents_cloud_reboot(self):
        self.engine.note_healthy()
        self.engine._save_ledger=Mock(side_effect=OSError('disk write failure'))
        with self.assertRaises(OSError):self.fault()
        self.api.reboot.assert_not_called()

    def test_pause_and_close_stop_power_actions(self):
        self.engine.note_healthy();self.config['cloud_recovery']['enabled']=False;self.fault()
        self.api.describe.assert_not_called()
        self.config['cloud_recovery']['enabled']=True;self.engine.close();self.fault()
        self.api.describe.assert_not_called()

    def test_sleep_resume_does_not_trigger_immediate_reboot(self):
        self.engine.note_healthy()
        self.engine.note_failure('SSH timeout');self.engine.note_failure('SSH timeout');self.engine.note_failure('SSH timeout')
        self.clock.value+=3600;self.engine.check_once()
        self.api.describe.assert_not_called();self.assertIsNone(self.engine.fault_since)

    def test_configuration_only_dry_runs_never_reboots(self):
        self.store.ready=False
        self.engine.configure('testAccessKeyId','testAccessKeySecret')
        self.api.describe.assert_called_once();self.api.dry_run.assert_called_once_with('i-test')
        self.api.reboot.assert_not_called()
        self.assertTrue(self.store.ready)
        self.assertNotIn('testAccessKeySecret',json.dumps(self.engine.public_status()))


class ApiTests(unittest.TestCase):
    def test_instance_identity_and_ip_are_checked(self):
        api=EcsApi({'access_key_id':'dummy','access_key_secret':'dummy'},'cn-hangzhou')
        api.call=Mock(return_value={'Instances':{'Instance':[{'InstanceId':'i-target','RegionId':'cn-hangzhou','PublicIpAddress':{'IpAddress':['YOUR_SERVER_IP']}}]}})
        self.assertEqual(api.describe('i-target','YOUR_SERVER_IP')['InstanceId'],'i-target')
        for target,ip in [('i-other','YOUR_SERVER_IP'),('i-target','8.8.8.8')]:
            with self.assertRaises(CloudError):api.describe(target,ip)

    def test_dry_run_and_real_reboot_never_force(self):
        api=EcsApi({},'cn-hangzhou');api.call=Mock(side_effect=CloudError('DryRunOperation'))
        self.assertTrue(api.dry_run('i-target'))
        api.call.assert_called_with('RebootInstance',InstanceId='i-target',ForceStop='false',DryRun='true')
        api.call=Mock(return_value={'RequestId':'test'})
        api.reboot('i-target')
        api.call.assert_called_with('RebootInstance',InstanceId='i-target',ForceStop='false',DryRun='false')


@unittest.skipUnless(os.name=='nt','Windows DPAPI test')
class EncryptionTests(unittest.TestCase):
    def test_dpapi_round_trip_and_no_plaintext_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'credentials.json';store=CredentialStore(path,'i-test')
            credentials={'access_key_id':'testKeyId','access_key_secret':'very-private-test-secret'}
            store.save(credentials)
            self.assertNotIn('very-private-test-secret',path.read_text())
            self.assertNotIn('testKeyId',path.read_text())
            self.assertEqual(store.load(),credentials)
            with self.assertRaises(RuntimeError):CredentialStore(path,'i-different').load()


if __name__=='__main__':unittest.main(verbosity=2)
