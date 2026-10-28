from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
TOOLS=Path(__file__).resolve().parents[1]/"tools"
sys.path.insert(0,str(TOOLS))
import verify_ble_native as qa
import desktop_sync
TARGET="11111111-1111-4111-8111-111111111111"
SECRET="private-title-or-prompt"

class BleNativeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.state=self.root/"state.json"; self.config=self.root/"config.toml"
        self.state.write_text('{}'); self.config.write_text('')
    def tearDown(self): self.directory.cleanup()
    def mapping(self,plan="fast",layout=None):
        if layout is not None:self.state.write_text(json.dumps({desktop_sync.LAYOUT_KEY:layout,"title":SECRET}))
        return qa.mapping_summary(self.state,self.config,plan)
    def test_defaults_are_verified(self):
        for plan in qa.PLANS:
            result=self.mapping(plan)
            self.assertEqual(result['encoder'],'composer-navigation')
            self.assertEqual(result['actions'][0],'fast')
            self.assertEqual(result['joystick_up'],'plan')
    def test_fast_custom_or_send_refused(self):
        for action in ({"type":"composer-text","text":SECRET},{"type":"command","commandId":"composer.submit"}):
            layout=deepcopy(desktop_sync.DEFAULT_LAYOUT);layout['slots']['ACT06']['action']=action
            with self.assertRaises(RuntimeError):self.mapping('fast',layout)
    def test_dial_custom_refused(self):
        layout=deepcopy(desktop_sync.DEFAULT_LAYOUT);layout['encoderMode']='custom'
        with self.assertRaises(RuntimeError):self.mapping('dial',layout)
    def test_plan_requires_exact_up(self):
        layout=deepcopy(desktop_sync.DEFAULT_LAYOUT);layout['analogStick']=deepcopy(qa.DEFAULT_STICK)
        layout['analogStick']['up']={'type':'command','commandId':'approval.approve'}
        with self.assertRaises(RuntimeError):self.mapping('plan',layout)
    def test_report_mapping_has_no_private_content(self):
        layout=deepcopy(desktop_sync.DEFAULT_LAYOUT);layout['ignored_private_note']=SECRET
        self.assertNotIn(SECRET,json.dumps(self.mapping('fast',layout)))
    def test_lock_state_fails_closed(self):
        user={'kCGSSessionUserIDKey':501,'kCGSSessionOnConsoleKey':True}
        self.assertFalse(qa.unlocked_from_registry({'IOConsoleUsers':[user]},501))
        self.assertFalse(qa.unlocked_from_registry({'IOConsoleLocked':True,'IOConsoleUsers':[user]},501))
        self.assertTrue(qa.unlocked_from_registry({'IOConsoleLocked':False,'IOConsoleUsers':[user]},501))
        self.assertFalse(qa.unlocked_from_registry({'IOConsoleLocked':False,'IOConsoleUsers':[user]},502))
        user['CGSSessionScreenIsLocked']=True
        self.assertFalse(qa.unlocked_from_registry({'IOConsoleLocked':False,'IOConsoleUsers':[user]},501))
    def test_cache_target_freshness_privacy(self):
        cache=self.root/'cache.json'
        data={'v':1,'source':'desktop-ipc','scope':'explicit-target','host_id':'local','connection':'live',
              'thread_id':TARGET,'observed_at':100,'tier':'standard','mode':'default','title':SECRET}
        cache.write_text(json.dumps(data))
        result=qa.cache_summary(cache,TARGET,now=101)
        self.assertNotIn(SECRET,json.dumps(result));self.assertNotIn(TARGET,json.dumps(result))
        with self.assertRaises(RuntimeError):qa.cache_summary(cache,TARGET,now=107)
        with self.assertRaises(RuntimeError):qa.cache_summary(cache,'wrong',now=101)
    def test_topology_only_matching_devices(self):
        one={'VendorID':0x303A,'ProductID':0x8360,'PrimaryUsagePage':0xFF00,'Transport':'Bluetooth Low Energy'}
        self.assertEqual(qa.topology_from_registry([one,{**one,'VendorID':1}]),['bluetooth'])
        self.assertEqual(qa.topology_from_registry({'IORegistryEntryChildren':[{**one,'Transport':'USB'}]}),['usb'])
    def test_topology_mac_keyboard_primary_includes_native_vendor_collection(self):
        bt={'VendorID':12346,'ProductID':33632,'PrimaryUsagePage':1,'PrimaryUsage':6,
            'DeviceUsagePairs':[{'DeviceUsage':6,'DeviceUsagePage':1},
                                {'DeviceUsage':1,'DeviceUsagePage':65280}],
            'Transport':'Bluetooth Low Energy'}
        usb={**bt,'PrimaryUsagePage':65280,'PrimaryUsage':1,'Transport':'USB'}
        self.assertEqual(qa.topology_from_registry(bt),['bluetooth'])
        self.assertEqual(qa.topology_from_registry(usb),['usb'])
        self.assertEqual(qa.topology_from_registry({'IORegistryEntryChildren':[bt,usb]}),['bluetooth','usb'])
        for field,value in (('VendorID',1),('ProductID',0x1001)):
            self.assertEqual(qa.topology_from_registry({**bt,field:value}),[])
    def test_topology_rejects_missing_or_malformed_vendor_usage(self):
        keyboard={'VendorID':0x303A,'ProductID':0x8360,'PrimaryUsagePage':1,'Transport':'Bluetooth Low Energy'}
        for pairs in (None,{},'65280',[None,'65280',{}],
                      [{'DeviceUsage':1,'DeviceUsagePage':'65280'}],
                      [{'DeviceUsage':6,'DeviceUsagePage':1}]):
            with self.subTest(pairs=pairs):
                self.assertEqual(qa.topology_from_registry({**keyboard,'DeviceUsagePairs':pairs}),[])
        self.assertEqual(qa.topology_from_registry({**keyboard,'PrimaryUsagePage':'65280'}),[])
    def test_topology_unknown_transport_cannot_prove_bluetooth_only(self):
        device={'VendorID':0x303A,'ProductID':0x8360,'PrimaryUsagePage':0xFF00}
        for transport in (None,'','wireless','not-bluetooth','USB over Bluetooth',False):
            with self.subTest(transport=transport):
                found=qa.topology_from_registry({**device,'Transport':transport})
                self.assertEqual(found,['unknown'])
                observations=[{'unlocked':True,'mapping_unchanged':True,
                               'transports':found,'cache':{'tier':tier}}
                              for tier in ('standard','fast','standard')]
                self.assertFalse(qa.effect_summary(observations,{'tier':'standard'},'fast')['confirmed'])
        self.assertEqual(qa.topology_from_registry([{**device,'Transport':'Bluetooth'},device]),['bluetooth','unknown'])
    def test_execution_requires_isolated_bluetooth_change_and_restore(self):
        base={'tier':'standard','mode':'default'}
        obs=[{'unlocked':True,'mapping_unchanged':True,'transports':['bluetooth'],'cache':{'tier':v}} for v in ['standard','fast','standard']]
        self.assertTrue(qa.effect_summary(obs,base,'fast')['confirmed'])
        self.assertFalse(qa.effect_summary(obs[:2],base,'fast')['confirmed'])
        for item in obs:item['transports']=['usb','bluetooth']
        self.assertFalse(qa.effect_summary(obs,base,'fast')['confirmed'])
        self.assertFalse(qa.effect_summary(obs,base,'dial')['confirmed'])
    def test_unknown_and_ultrafast_are_not_opposite_fast_states(self):
        def sequence(values):
            return [{'unlocked':True,'mapping_unchanged':True,'transports':['bluetooth'],'cache':{'tier':v}} for v in values]
        for invalid in ('unknown','ultrafast',None,'bogus'):
            self.assertFalse(qa.effect_summary(sequence(['fast',invalid,'fast']),{'tier':'fast'},'fast')['confirmed'])
        for baseline in ('unknown','ultrafast',None):
            self.assertFalse(qa.effect_summary(sequence([baseline,'fast',baseline]),{'tier':baseline},'fast')['confirmed'])
        self.assertTrue(qa.effect_summary(sequence(['fast','default','fast']),{'tier':'fast'},'fast')['confirmed'])
    def test_plan_requires_explicit_opposite_mode(self):
        def sequence(values):
            return [{'unlocked':True,'mapping_unchanged':True,'transports':['bluetooth'],'cache':{'mode':v}} for v in values]
        self.assertTrue(qa.effect_summary(sequence(['default','plan','default']),{'mode':'default'},'plan')['confirmed'])
        self.assertTrue(qa.effect_summary(sequence(['plan','default','plan']),{'mode':'plan'},'plan')['confirmed'])
        for invalid in ('unknown',None,'fast'):
            self.assertFalse(qa.effect_summary(sequence(['default',invalid,'default']),{'mode':'default'},'plan')['confirmed'])
    def test_changed_or_unknown_mapping_and_errors_cannot_prove_execution(self):
        base={'unlocked':True,'mapping_unchanged':True,'transports':['bluetooth']}
        for modifier in ({'mapping_unchanged':False},{'mapping_unchanged':None},{'error':'read-failed'},{'error':None}):
            observations=[{**base,'cache':{'tier':'standard'}},
                          {**base,**modifier,'cache':{'tier':'fast'}},
                          {**base,'cache':{'tier':'standard'}}]
            self.assertFalse(qa.effect_summary(observations,{'tier':'standard'},'fast')['confirmed'])
        observations=[{**base,'cache':{'tier':'fast'}},
                      {**base,'mapping_unchanged':False,'cache':{'tier':'standard'}}]
        self.assertFalse(qa.effect_summary(observations,{'tier':'standard'},'fast')['confirmed'])
    def test_interrupted_observations_cannot_join_change_and_restore(self):
        base={'unlocked':True,'mapping_unchanged':True,'transports':['bluetooth']}
        for interruption in ({'unlocked':False},{'unlocked':None},
                             {'mapping_unchanged':False},{'mapping_unchanged':None},
                             {'transports':['usb','bluetooth']},{'transports':['usb']},
                             {'transports':[]},{'transports':['unknown']},
                             {'error':'read-failed'},{'error':None},{'cache':None}):
            observations=[{**base,'cache':{'tier':'fast'}},
                          {**base,'cache':{'tier':'fast'},**interruption},
                          {**base,'cache':{'tier':'standard'}}]
            with self.subTest(interruption=interruption):
                result=qa.effect_summary(observations,{'tier':'standard'},'fast')
                self.assertFalse(result['confirmed'])
                self.assertFalse(result['uninterrupted_ble_interval'])
    def test_expected_usb_removal_and_recovery_do_not_break_isolated_interval(self):
        base={'unlocked':True,'mapping_unchanged':True}
        observations=[{**base,'transports':['usb','bluetooth'],'cache':{'tier':'standard'}},
                      {**base,'transports':[],'cache':{'tier':'standard'}},
                      *[{**base,'transports':['bluetooth'],'cache':{'tier':tier}}
                        for tier in ('standard','fast','standard')],
                      {**base,'transports':['usb','bluetooth'],'cache':{'tier':'standard'}}]
        self.assertTrue(qa.effect_summary(observations,{'tier':'standard'},'fast')['confirmed'])
    def test_observed_cycle_requires_verified_native_transport_for_attribution(self):
        observations=[{'unlocked':True,'mapping_unchanged':True,
                       'transports':['bluetooth'],'cache':{'tier':tier},'elapsed_ms':elapsed}
                      for tier,elapsed in (('standard',100),('fast',1600),('standard',4700))]
        for verified in (False,None,1):
            with self.subTest(verified=verified):
                effect=qa.attributed_effect(observations,{'tier':'standard'},'fast',verified,not_before_ms=1000)
                self.assertTrue(effect['state_cycle_observed'])
                self.assertFalse(effect['confirmed'])
                self.assertFalse(effect['transport_attribution_verified'])
        self.assertTrue(qa.attributed_effect(observations,{'tier':'standard'},'fast',True,not_before_ms=1000)['confirmed'])
        self.assertFalse(qa.attributed_effect(observations,{'tier':'standard'},'dial',True,not_before_ms=1000)['confirmed'])
        for boundary in (None,True,-1,float('nan'),float('inf')):
            with self.subTest(boundary=boundary):
                self.assertFalse(qa.attributed_effect(observations,{'tier':'standard'},'fast',True,
                                                    not_before_ms=boundary)['confirmed'])
    def test_state_cycle_before_native_delay_cannot_prove_execution(self):
        base={'unlocked':True,'mapping_unchanged':True,'transports':['bluetooth']}
        for readings in ((('standard',100),('fast',200),('standard',300),('standard',16000)),
                         (('standard',100),('fast',200),('fast',16000),('standard',19000))):
            observations=[{**base,'cache':{'tier':tier},'elapsed_ms':elapsed} for tier,elapsed in readings]
            with self.subTest(readings=readings):
                self.assertFalse(qa.attributed_effect(observations,{'tier':'standard'},'fast',True,
                                                    not_before_ms=15000)['confirmed'])
        observations=[{**base,'cache':{'tier':tier},'elapsed_ms':elapsed}
                      for tier,elapsed in (('standard',14000),('fast',16000),('standard',19000))]
        self.assertTrue(qa.attributed_effect(observations,{'tier':'standard'},'fast',True,
                                           not_before_ms=15000)['confirmed'])
    def test_slow_os_queries_timestamp_the_actual_cache_read(self):
        clock=[114.969]
        mapping={'fingerprint':'checked'}
        cache_read_at=[]
        args=SimpleNamespace(state=self.state,config=self.config,plan='fast',cache=self.root/'cache',thread_id=TARGET)
        def slow_topology():
            clock[0]+=.65
            return ['bluetooth']
        def read_cache(*_args):
            cache_read_at.append(clock[0])
            return {'tier':'fast'}
        def slow_mapping(*_args):
            clock[0]+=5
            return mapping
        with patch.object(qa.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(qa,'mac_unlocked',return_value=True), \
             patch.object(qa,'topology',side_effect=slow_topology), \
             patch.object(qa,'mapping_summary',side_effect=slow_mapping), \
             patch.object(qa,'cache_summary',side_effect=read_cache):
            observation=qa.sample_observation(args,mapping,100)
        self.assertAlmostEqual(observation['sample_started_elapsed_ms'],14969)
        self.assertAlmostEqual(observation['elapsed_ms'],15619)
        self.assertAlmostEqual(observation['elapsed_ms'],(cache_read_at[0]-100)*1000)
        base={'unlocked':True,'mapping_unchanged':True,'transports':['bluetooth']}
        observations=[{**base,'cache':{'tier':'standard'},'elapsed_ms':14000},observation,
                      {**base,'cache':{'tier':'standard'},'elapsed_ms':19000}]
        self.assertTrue(qa.attributed_effect(observations,{'tier':'standard'},'fast',True,
                                           not_before_ms=15331.8)['confirmed'])
        # Slow mapping validation after reading the cache cannot relabel an
        # early manual change as a post-emission state observation.
        clock[0]=114.969
        with patch.object(qa.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(qa,'mac_unlocked',return_value=True), \
             patch.object(qa,'topology',return_value=['bluetooth']), \
             patch.object(qa,'mapping_summary',side_effect=slow_mapping), \
             patch.object(qa,'cache_summary',side_effect=read_cache):
            early=qa.sample_observation(args,mapping,100)
        self.assertAlmostEqual(early['elapsed_ms'],14969)
        observations[1]=early
        self.assertFalse(qa.attributed_effect(observations,{'tier':'standard'},'fast',True,
                                            not_before_ms=15331.8)['confirmed'])
    def test_argument_default_is_dry_run(self):
        args=qa.arguments(['--plan','fast','--thread-id',TARGET])
        self.assertFalse(args.execute);self.assertFalse(args.confirm_target_active)
        with self.assertRaises(RuntimeError):qa.arguments(['--plan','fast','--thread-id',TARGET,'--pause-seconds','10'])
    def test_main_dry_run_never_executes(self):
        output=self.root/'out.json'
        with patch.object(qa,'preflight',return_value={'ready':False,'errors':['locked']}), patch.object(qa,'execute') as run:
            self.assertEqual(qa.main(['--plan','fast','--thread-id',TARGET,'--output',str(output)]),0)
            run.assert_not_called()
        self.assertEqual(json.loads(output.read_text())['errors'],['locked'])
    def test_main_cannot_claim_execution_without_completed_native_transport(self):
        output=self.root/'out.json'
        report={'ready':True,'errors':[],'transport_verified':False,'desktop_effect':{'confirmed':True}}
        with patch.object(qa,'preflight',return_value=report),patch.object(qa,'execute'),patch('builtins.print') as printed:
            code=qa.main(['--plan','fast','--thread-id',TARGET,'--execute','--output',str(output)])
        self.assertEqual(code,1)
        self.assertFalse(json.loads(printed.call_args.args[0])['desktop_execution_confirmed'])
    def test_device_projection_redacts_host_state(self):
        data={'firmware':'test','host_feedback':{'chat':{'title':SECRET,'thread_id':TARGET}},'bluetooth':{'session':1,'name':SECRET}}
        text=json.dumps(qa.device_projection(data));self.assertNotIn(SECRET,text);self.assertNotIn(TARGET,text)

    def test_prearm_accepts_authenticated_session_before_hid_subscription(self):
        before={'firmware':'tab5-micro-0.12.0','active_transport':'usb','input_capture':False,
                'lighting_preview':False,'bluetooth':{'session':7,'enabled':True,'connected':True,
                    'authenticated':True,'bonded':True,'ready':False,'inputs_submitted':10},
                'host_feedback':{'config_fresh':True,'chat':{'fresh':True,'thread_id':TARGET},'actions':[2]*7}}
        args=SimpleNamespace(plan='fast',thread_id=TARGET)
        qa.check_device(before,args)
        before['bluetooth']['ready']=True
        qa.check_device(before,args)

    def test_prearm_never_uses_bond_or_ready_as_current_authentication(self):
        before={'firmware':'tab5-micro-0.12.0','active_transport':'usb','input_capture':False,
                'lighting_preview':False,'bluetooth':{'session':7,'enabled':True,'connected':True,
                    'authenticated':True,'bonded':True,'ready':True,'inputs_submitted':10},
                'host_feedback':{'config_fresh':True,'chat':{'fresh':True,'thread_id':TARGET},'actions':[2]*7}}
        args=SimpleNamespace(plan='fast',thread_id=TARGET)
        for field in ('enabled','connected','authenticated','bonded'):
            with self.subTest(field=field):
                changed=deepcopy(before);changed['bluetooth'][field]=False
                with self.assertRaises(RuntimeError):qa.check_device(changed,args)
                del changed['bluetooth'][field]
                with self.assertRaises(RuntimeError):qa.check_device(changed,args)
        for session in (None,False,True,0,-1,'7'):
            changed=deepcopy(before);changed['bluetooth']['session']=session
            with self.subTest(session=session),self.assertRaises(RuntimeError):qa.check_device(changed,args)

    def test_locked_execute_never_opens_serial(self):
        import tab5_transport
        args=SimpleNamespace()
        with patch.object(tab5_transport,'open_serial') as opened:
            with self.assertRaises(RuntimeError):qa.execute(args,{'ready':False})
            opened.assert_not_called()
    def test_lost_arm_ack_cancels_original_session_without_pause(self):
        import tab5_transport
        args=qa.arguments(['--plan','fast','--thread-id',TARGET,'--execute','--confirm-target-active'])
        mapping={'fingerprint':'checked'}
        before={'firmware':'tab5-micro-0.12.0','active_transport':'usb','input_capture':False,
                'lighting_preview':False,'bluetooth':{'session':7,'enabled':True,'connected':True,
                    'authenticated':True,'bonded':True,'ready':True,'inputs_submitted':10},
                'host_feedback':{'config_fresh':True,'chat':{'fresh':True,'thread_id':TARGET},'actions':[2]*7}}
        calls=[]; status_calls=0
        class FakeSession:
            def __init__(self,*args):pass
            def request(self,command,**kwargs):
                nonlocal status_calls
                calls.append(command)
                if command=='STATUS':return before
                if command=='BLE_TEST STATUS':
                    status_calls+=1
                    return {'state':'idle' if status_calls==1 else 'armed' if status_calls==2 else 'aborted','queued_steps':0}
                if command.startswith('BLE_TEST ARM'):raise TimeoutError('lost ack')
                return None
        connection=SimpleNamespace(port='fake')
        manager=MagicMock();manager.__enter__.return_value=connection
        report={'ready':True,'checks':{'mapping':mapping,'target_cache':{'tier':'standard','mode':'default'}}}
        with patch.object(tab5_transport,'open_serial',return_value=manager) as opened, \
             patch.object(tab5_transport,'application_ports',return_value=[SimpleNamespace(device='fake',serial_number='TAB5-0123456789AB')]), \
             patch.object(qa,'DeviceSession',FakeSession),patch.object(qa,'mac_unlocked',return_value=True), \
             patch.object(qa,'mapping_summary',return_value=mapping),patch.object(qa,'cache_summary',return_value={}):
            with self.assertRaises(TimeoutError):qa.execute(args,report)
        self.assertIn('BLE_TEST CANCEL',calls)
        self.assertFalse(any(command.startswith('USB_PAUSE') for command in calls))
        self.assertEqual(opened.call_args.kwargs['serial_number'],'TAB5-0123456789AB')
        self.assertEqual(report['schedule']['state'],'aborted')

if __name__=='__main__': unittest.main()
