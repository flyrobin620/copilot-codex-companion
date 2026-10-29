import copy
import io
import json
import socket
import sys
import time
import tomllib
import unittest
import subprocess
from pathlib import Path
from unittest import mock
import manage
import auth_bridge
from proxy import Normalizer
from catalog import adapt


class AuthBridgeTests(unittest.TestCase):
    def test_hostname_optional_and_replaces_existing_flags(self):
        original = ['auth', 'token', '--hostname', 'old.example.test']
        self.assertEqual(auth_bridge.auth_args(original), original)
        for args in (['auth', 'token'], original,
                     ['auth', 'token', '--hostname=old.example.test'],
                     ['auth', 'token', '--hostname', 'old.example.test', '--hostname=other.example.test']):
            self.assertEqual(auth_bridge.auth_args(args, 'enterprise.example.test'),
                             ['auth', 'token', '--hostname', 'enterprise.example.test'])
        self.assertEqual(original[-1], 'old.example.test')
        self.assertEqual(auth_bridge.auth_args(['auth', '--', '--hostname=literal'], 'host.test'),
                         ['auth', '--hostname', 'host.test', '--', '--hostname=literal'])
        for value in ('', ' ', '--restart'):
            with self.assertRaises(ValueError): auth_bridge.auth_args(original, value)

    def test_restart_refuses_unknown_service(self):
        with mock.patch.object(auth_bridge, 'health', return_value={'installation': 'other'}), \
             mock.patch.object(auth_bridge, 'start_bridge') as start, \
             mock.patch.object(auth_bridge.urllib.request, 'build_opener') as opener:
            with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
                auth_bridge.restart_bridge(Path('.'), {'installation': 'ours'})
            start.assert_not_called()
            opener.assert_not_called()

    def test_restart_stopped_service_and_unverifiable_service(self):
        settings = {'installation': 'ours'}
        with mock.patch.object(auth_bridge, 'health', return_value=None), \
             mock.patch.object(auth_bridge, 'start_bridge') as start:
            auth_bridge.restart_bridge(Path('.'), settings)
            start.assert_called_once_with(Path('.'), settings)
        with mock.patch.object(auth_bridge, 'health', side_effect=TimeoutError), \
             mock.patch.object(auth_bridge, 'start_bridge') as start:
            with self.assertRaisesRegex(RuntimeError, 'Cannot verify'):
                auth_bridge.restart_bridge(Path('.'), settings)
            start.assert_not_called()

    def test_restart_shutdown_timeout_does_not_launch(self):
        settings = {'installation': 'ours', 'port': 43129, 'control': 'test-control'}
        opener = mock.MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(b'{"stopping":true}')
        with mock.patch.object(auth_bridge, 'health', return_value=settings), \
             mock.patch.object(auth_bridge.urllib.request, 'build_opener', return_value=opener), \
             mock.patch.object(auth_bridge.socket, 'create_connection', return_value=mock.MagicMock()), \
             mock.patch.object(auth_bridge.time, 'monotonic', side_effect=[0, 11]), \
             mock.patch.object(auth_bridge, 'start_bridge') as start:
            with self.assertRaisesRegex(RuntimeError, 'did not stop in time'):
                auth_bridge.restart_bridge(Path('.'), settings)
            start.assert_not_called()
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_header('X-compat-control'), 'test-control')


class CatalogTests(unittest.TestCase):
    def fixture(self):
        defaults={'models':[{'slug':'known','context_window':272000,'max_context_window':872000,
            'effective_context_window_percent':95,'model_messages':{'instructions_template':'KEEP EXACTLY'},
            'base_instructions':'KEEP BASE','supported_reasoning_levels':[{'effort':'low'},{'effort':'ultra'}],
            'default_reasoning_level':'ultra','input_modalities':['text','image'],'service_tiers':[{'id':'priority'}]}]}
        upstream={'data':[{'id':'known','policy':{'state':'enabled'},'supported_endpoints':['/responses'],
            'capabilities':{'limits':{'max_context_window_tokens':1050000,'max_output_tokens':128000,'max_prompt_tokens':1050000},
                'supports':{'streaming':True,'tool_calls':True,'vision':True,'reasoning_effort':['low']}}}]}
        return upstream,defaults

    def test_limits_reserve_output_and_preserve_instructions(self):
        upstream,defaults=self.fixture(); before=copy.deepcopy(defaults)
        model=adapt(upstream,defaults)['models'][0]
        self.assertEqual(model['context_window'],922000)
        self.assertEqual(model['max_context_window'],922000)
        self.assertEqual(model['effective_context_window_percent'],95)
        self.assertEqual(model['base_instructions'],'KEEP BASE')
        self.assertEqual(model['model_messages'],before['models'][0]['model_messages'])
        self.assertEqual(model['supported_reasoning_levels'],[{'effort':'low'}])
        self.assertEqual(model['default_reasoning_level'],'low')
        self.assertEqual(model['service_tiers'],[])
        self.assertEqual(defaults,before)

    def test_unknown_disabled_and_invalid_limits_fail_closed(self):
        for change in ['unknown','disabled','invalid']:
            upstream,defaults=self.fixture();remote=upstream['data'][0]
            if change=='unknown': remote['id']='different-model'
            elif change=='disabled': remote['policy']['state']='disabled'
            else: remote['capabilities']['limits']['max_output_tokens']=2000000
            with self.assertRaises(RuntimeError): adapt(upstream,defaults)


class NormalizerTests(unittest.TestCase):
    def test_rotating_ids_and_payloads(self):
        n=Normalizer()
        n.normalize({'output_index':0,'item':{'id':'first'}})
        n.normalize({'output_index':1,'item':{'id':'tool-first','call_id':'call-1'}})
        event={'output_index':1,'item':{'id':'tool-last','call_id':'call-1','arguments':'{"id":"preserve"}'}}
        result=n.normalize(copy.deepcopy(event))
        self.assertEqual(result['item']['id'],'tool-first')
        self.assertEqual(result['item']['call_id'],'call-1')
        self.assertEqual(result['item']['arguments'],event['item']['arguments'])
        self.assertEqual(n.normalize({'output_index':0,'item_id':'rotated','delta':'same'})['item_id'],'first')
        result=n.normalize({'response':{'output':[{'id':'rotated-again'},{'id':'tool-rotated-again'}]}})
        self.assertEqual([i['id'] for i in result['response']['output']],['first','tool-first'])
        self.assertEqual(Normalizer().ids,{})


class InstallationTests(unittest.TestCase):
    def setUp(self):
        patch=mock.patch.object(manage,'catalog_check',return_value={'slug':'synthetic-model','context_window':922000})
        patch.start(); self.addCleanup(patch.stop)
        import uuid
        self.root=Path(__file__).resolve().parent.parent/'distribution-tests'/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.config=self.root/'profile with spaces 配置.toml'
        self.dest=self.root/'runtime with spaces 中文'
        self.original='''# Keep this comment
model = "synthetic-model"
model_provider = "enterprise"
[features]
some_existing_flag = true
[model_providers.enterprise]
name = "Existing provider"
base_url = "https://api.enterprise.githubcopilot.com"
wire_api = "responses"
http_headers = { Editor-Version = "existing-header" }
[model_providers.enterprise.auth]
command = '''+json.dumps(sys.executable)+'''
args = ["-c", "print('SYNTHETIC_TEST_CREDENTIAL')"]
refresh_interval_ms = 300000
'''
        self.config.write_text(self.original,encoding='utf-8')
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            self.port=s.getsockname()[1]

    def tearDown(self):
        if (self.dest/'settings.json').exists():
            s=manage.settings_at(self.dest)
            try: manage.local_json(s['changed']['base_url']+'/shutdown',b'',{'X-Compat-Control':s['control']})
            except Exception: pass

    def test_install_repeat_check_restore_preserves_other_edits(self):
        manage.install(self.config,self.dest,self.port)
        cfg=tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(cfg['model'],'synthetic-model')
        self.assertEqual(cfg['model_providers']['enterprise']['http_headers'],{'Editor-Version':'existing-header'})
        self.assertIn('# Keep this comment',self.config.read_text(encoding='utf-8'))
        before=self.config.read_bytes()
        manage.install(self.config,self.dest,self.port)
        self.assertEqual(before,self.config.read_bytes())
        manage.check(self.dest)
        s=manage.settings_at(self.dest)
        # The control endpoint must reject callers without its local management key.
        with self.assertRaises(Exception): manage.local_json(s['changed']['base_url']+'/shutdown',b'')
        self.config.write_text(self.config.read_text(encoding='utf-8').replace('some_existing_flag = true','some_existing_flag = false'),encoding='utf-8')
        manage.uninstall(self.dest)
        expected=tomllib.loads(self.original)
        expected['features']['some_existing_flag']=False
        self.assertEqual(tomllib.loads(self.config.read_text(encoding='utf-8')),expected)
        self.assertTrue((self.dest/'config.before.toml').exists())

    def test_occupied_port_leaves_config_unchanged(self):
        with socket.socket() as s:
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'): s.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            s.bind(('127.0.0.1',self.port)); s.listen()
            with self.assertRaises(RuntimeError): manage.install(self.config,self.dest,self.port)
        self.assertEqual(self.config.read_text(encoding='utf-8'),self.original)
        self.assertFalse(self.dest.exists())

    def test_changed_connection_refuses_restore(self):
        manage.install(self.config,self.dest,self.port)
        changed=self.config.read_text(encoding='utf-8').replace(f'http://127.0.0.1:{self.port}','https://changed.example.test')
        self.config.write_text(changed,encoding='utf-8')
        with self.assertRaises(RuntimeError): manage.uninstall(self.dest)
        self.assertEqual(self.config.read_text(encoding='utf-8'),changed)

    def test_failed_auth_leaves_config_unchanged(self):
        with mock.patch.object(manage,'credential',side_effect=RuntimeError('Test auth failed')):
            with self.assertRaises(RuntimeError): manage.install(self.config,self.dest,self.port)
        self.assertEqual(self.config.read_text(encoding='utf-8'),self.original)
        self.assertFalse(self.dest.exists())

    def test_automatic_context_removal_and_restore(self):
        original='model_context_window = 872000\nmodel_auto_compact_token_limit = 800000\n'+self.original
        self.config.write_text(original,encoding='utf-8')
        manage.install(self.config,self.dest,self.port,automatic_context=True)
        installed=tomllib.loads(self.config.read_text(encoding='utf-8'))
        self.assertNotIn('model_context_window',installed)
        self.assertNotIn('model_auto_compact_token_limit',installed)
        manage.uninstall(self.dest)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding='utf-8')),tomllib.loads(original))

    def test_hostname_install_override_and_restore(self):
        helper_args = ['-c', "import sys; assert sys.argv[1:] == ['--hostname', 'enterprise.example.test']; print('SYNTHETIC_TEST_CREDENTIAL')",
                       '--hostname', 'original.example.test']
        original = manage.field(self.original, 'model_providers.enterprise.auth', 'args', helper_args)
        self.config.write_text(original, encoding='utf-8')
        manage.install(self.config, self.dest, self.port, hostname='enterprise.example.test')
        settings = manage.settings_at(self.dest)
        self.assertEqual(settings['auth_args'][-2:], ['--hostname', 'enterprise.example.test'])
        manage.check(self.dest)
        manage.uninstall(self.dest)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding='utf-8')), tomllib.loads(original))

    def test_restart_and_direct_hostname_argument(self):
        manage.install(self.config, self.dest, self.port)
        settings = manage.settings_at(self.dest)
        self.assertTrue((self.dest/'restart.cmd').is_file())
        config_before = self.config.read_bytes()
        for command in ([sys.executable, str(self.dest/'manage.py'), 'restart'],
                        [sys.executable, str(self.dest/'auth_bridge.py'), '--restart']):
            result = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertNotIn(b'SYNTHETIC_TEST_CREDENTIAL', result.stdout + result.stderr)
            self.assertTrue(auth_bridge.healthy(settings))
            if '--restart' in command:
                self.assertEqual(result.stdout, b'')
        manage.local_json(settings['changed']['base_url']+'/shutdown', b'', {'X-Compat-Control': settings['control']})
        time.sleep(0.6)
        auth_bridge.restart_bridge(self.dest, settings)
        self.assertTrue(auth_bridge.healthy(settings))
        self.assertEqual(self.config.read_bytes(), config_before)
        settings['auth_args'] = ['-c', "import sys; assert sys.argv[1:] == ['--hostname', 'custom.example.test']; print('SYNTHETIC_TEST_CREDENTIAL')",
                                 '--hostname=previous.example.test']
        (self.dest/'settings.json').write_text(json.dumps(settings), encoding='utf-8')
        result = subprocess.run([sys.executable, str(self.dest/'auth_bridge.py'), '--hostname', 'custom.example.test'],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout.strip(), b'SYNTHETIC_TEST_CREDENTIAL')
        self.assertEqual(result.stderr, b'')
        self.assertEqual(manage.settings_at(self.dest)['auth_args'], settings['auth_args'])

    def test_update_legacy_install_and_switch_hostname_in_place(self):
        manage.install(self.config, self.dest, self.port, automatic_context=True)
        old = manage.settings_at(self.dest)
        old['version'] = '1.1.0'
        (self.dest/'settings.json').write_text(json.dumps(old), encoding='utf-8')
        # A 1.1.0 install has neither launcher and its helper has no CLI parser.
        (self.dest/'restart.cmd').unlink()
        (self.dest/'update.cmd').unlink()
        legacy_helper = b"import json, pathlib, subprocess, sys\ns = json.loads(pathlib.Path(__file__).with_name('settings.json').read_text())\nsys.stdout.buffer.write(subprocess.check_output([s['auth_command'], *s['auth_args']]))\n"
        (self.dest/'auth_bridge.py').write_bytes(legacy_helper)
        config_before = self.config.read_bytes()
        uninstall_backup = (self.dest/'config.before.toml').read_bytes()
        manage.update(self.dest, hostname='enterprise.example.test')
        updated = manage.settings_at(self.dest)
        expected = copy.deepcopy(old)
        expected.update(version=manage.VERSION, auth_args=auth_bridge.auth_args(old['auth_args'], 'enterprise.example.test'))
        self.assertEqual(updated, expected)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual((self.dest/'config.before.toml').read_bytes(), uninstall_backup)
        backup = next(self.dest.glob('update-backup-*'))
        self.assertEqual((backup/'auth_bridge.py').read_bytes(), legacy_helper)
        self.assertEqual(json.loads((backup/'settings.json').read_text()), old)
        self.assertTrue((self.dest/'update.cmd').is_file())
        self.assertTrue(auth_bridge.healthy(updated))
        # The installed package can be reused to change hostname, then updated
        # without an override while retaining that choice.
        manage.update(self.dest, hostname='second.example.test', source=self.dest)
        manage.update(self.dest, source=self.dest)
        self.assertEqual(manage.settings_at(self.dest)['auth_args'][-2:], ['--hostname', 'second.example.test'])
        manage.uninstall(self.dest)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding='utf-8')), tomllib.loads(self.original))

    def test_update_verification_failure_restores_old_installation(self):
        manage.install(self.config, self.dest, self.port)
        # Include files absent from an old distribution in the rollback check.
        (self.dest/'update.cmd').unlink()
        names = (*manage.FILES, 'settings.json', 'catalog-defaults.json', 'config.before.toml')
        before = {name: (self.dest/name).read_bytes() if (self.dest/name).exists() else None for name in names}
        config_before = self.config.read_bytes()
        with mock.patch.object(manage, 'check', side_effect=RuntimeError('Synthetic catalog failure')):
            with self.assertRaisesRegex(RuntimeError, 'previous files and settings restored'):
                manage.update(self.dest, hostname='rejected.example.test')
        for name, data in before.items():
            self.assertEqual((self.dest/name).read_bytes() if (self.dest/name).exists() else None, data)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertFalse((self.dest/'update.lock').exists())
        self.assertTrue(auth_bridge.healthy(manage.settings_at(self.dest)))

    def test_update_failed_auth_does_not_stop_or_modify_installation(self):
        manage.install(self.config, self.dest, self.port)
        before = (self.dest/'settings.json').read_bytes()
        with mock.patch.object(manage, 'credential', side_effect=RuntimeError('Synthetic auth failure')), \
             mock.patch.object(manage, 'stop_bridge') as stop:
            with self.assertRaisesRegex(RuntimeError, 'auth failure'):
                manage.update(self.dest, hostname='unavailable.example.test')
            stop.assert_not_called()
        self.assertEqual((self.dest/'settings.json').read_bytes(), before)
        self.assertEqual(list(self.dest.glob('update-backup-*')), [])
        self.assertFalse((self.dest/'update.lock').exists())

    def test_update_refuses_modified_connection(self):
        manage.install(self.config, self.dest, self.port)
        self.config.write_text(self.config.read_text(encoding='utf-8').replace(f'http://127.0.0.1:{self.port}', 'https://changed.example.test'), encoding='utf-8')
        before = (self.dest/'settings.json').read_bytes()
        with mock.patch.object(manage, 'stop_bridge') as stop:
            with self.assertRaisesRegex(RuntimeError, 'connection settings changed'):
                manage.update(self.dest)
            stop.assert_not_called()
        self.assertEqual((self.dest/'settings.json').read_bytes(), before)

    def test_update_incomplete_package_and_concurrent_update(self):
        manage.install(self.config, self.dest, self.port)
        empty_source = self.root/'incomplete-package'
        empty_source.mkdir()
        with mock.patch.object(manage, 'stop_bridge') as stop:
            with self.assertRaises(FileNotFoundError):
                manage.update(self.dest, source=empty_source)
            stop.assert_not_called()
        self.assertFalse((self.dest/'update.lock').exists())
        (self.dest/'update.lock').write_text('another update', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'update lock exists'):
            manage.update(self.dest)
        self.assertEqual((self.dest/'update.lock').read_text(), 'another update')


if __name__=='__main__': unittest.main()
