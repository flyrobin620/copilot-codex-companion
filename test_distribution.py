import copy
import io
import json
import os
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
import proxy
from proxy import Normalizer
from catalog import adapt
from upstream import DEFAULT_UPSTREAM, select_upstream, settings_upstream, validate_upstream


class AuthBridgeTests(unittest.TestCase):
    def test_direct_hostname_change_cannot_mix_tokens_and_api_hosts(self):
        root = mock.MagicMock()
        (root/'settings.json').read_text.return_value = json.dumps({
            'auth_args': ['auth', 'token', '--hostname', 'github.com'],
            'upstream_url': DEFAULT_UPSTREAM,
        })
        with mock.patch.object(auth_bridge, 'ROOT', root), \
             mock.patch.object(auth_bridge, 'start_bridge') as start, \
             mock.patch.object(auth_bridge.subprocess, 'run') as credential:
            with self.assertRaisesRegex(RuntimeError, 'different Copilot API'):
                auth_bridge.main(['--hostname', 'microsoft.ghe.com'])
            start.assert_not_called()
            credential.assert_not_called()

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


class ConnectionDiagnosticsTests(unittest.TestCase):
    def test_connection_details_redact_url_credentials(self):
        details = manage.connection_details(Path('custom.toml'), 'enterprise', {
            'base_url': 'https://user:URL_SECRET@api.example.test/v1?token=QUERY_SECRET#FRAGMENT_SECRET',
            'wire_api': 'chat', 'auth': {'command': 'gh', 'args': ['SECRET_ARG']},
        })
        self.assertIn('custom.toml', details)
        self.assertIn('https://api.example.test/v1', details)
        self.assertIn("wire_api: 'chat'", details)
        for secret in ('URL_SECRET', 'QUERY_SECRET', 'FRAGMENT_SECRET', 'SECRET_ARG'):
            self.assertNotIn(secret, details)

    def test_connection_details_handle_missing_and_invalid_urls(self):
        for value, expected in ((None, '<missing>'), ('not a url', '<invalid URL>'), ('https://host:bad', '<invalid URL>')):
            self.assertIn(expected, manage.connection_details(Path('config.toml'), 'enterprise', {'base_url': value}))


class UpstreamTests(unittest.TestCase):
    def test_ghe_hostname_selects_enterprise_endpoint(self):
        for args in (['auth', 'token', '--hostname', 'microsoft.ghe.com'],
                     ['auth', 'token', '--hostname=Microsoft.GHE.COM']):
            self.assertEqual(select_upstream(args), 'https://copilot-api.microsoft.ghe.com')
        self.assertEqual(select_upstream([], hostname='another.ghe.com'), 'https://copilot-api.another.ghe.com')
        self.assertEqual(select_upstream([], 'https://copilot-api.microsoft.ghe.com', 'github.com'), DEFAULT_UPSTREAM)
        self.assertEqual(select_upstream([]), DEFAULT_UPSTREAM)
        self.assertEqual(select_upstream(['auth', '--', '--hostname=microsoft.ghe.com']), DEFAULT_UPSTREAM)

    def test_migration_uses_saved_host_and_respects_explicit_endpoint(self):
        legacy = {'old': {'base_url': DEFAULT_UPSTREAM}, 'auth_args': ['auth', 'token', '--hostname', 'microsoft.ghe.com']}
        self.assertEqual(settings_upstream(legacy), 'https://copilot-api.microsoft.ghe.com')
        self.assertEqual(settings_upstream({'old': {'base_url': DEFAULT_UPSTREAM}}), DEFAULT_UPSTREAM)
        self.assertEqual(settings_upstream({'upstream_url': 'https://copilot-api.another.ghe.com'}), 'https://copilot-api.another.ghe.com')

    def test_rejects_non_copilot_origins_and_embedded_credentials(self):
        for value in ('http://copilot-api.microsoft.ghe.com', 'https://copilot-api.microsoft.ghe.com.evil.test',
                      'https://user:secret@copilot-api.microsoft.ghe.com', 'https://copilot-api.microsoft.ghe.com/?token=secret',
                      'https://copilot-api.microsoft.ghe.com:8443', 'https://example.test', 'https://copilot-api.microsoft.ghe.com/path'):
            with self.assertRaises(RuntimeError):
                validate_upstream(value)
        self.assertEqual(validate_upstream('https://copilot-api.microsoft.ghe.com/'), 'https://copilot-api.microsoft.ghe.com')

    def test_proxy_routes_models_and_responses_to_same_saved_endpoint(self):
        endpoint = 'https://copilot-api.microsoft.ghe.com'
        handler = object.__new__(proxy.Handler)
        handler.headers = {'Authorization': 'Bearer SYNTHETIC_TEST_CREDENTIAL'}
        handler.path = '/models'
        handler.reply = mock.Mock()
        with mock.patch.object(proxy, 'SETTINGS', {'upstream_url': endpoint}), \
             mock.patch.object(proxy, 'ensure_defaults', return_value={'catalog': {}}), \
             mock.patch.object(proxy, 'adapt', return_value={'models': []}), \
             mock.patch.object(proxy.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"data":[]}')) as request:
            handler.serve_catalog()
            self.assertEqual(request.call_args.args[0].full_url, endpoint+'/models')
            handler.reply.assert_called_once_with(200, {'models': []})
        for route in ('/responses', '/responses/compact'):
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.headers = {'Content-Type': 'application/json'}
            response.read.side_effect = [b'{}', b'']
            handler.path = route
            handler.command = 'POST'
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()
            handler.wfile = io.BytesIO()
            with mock.patch.object(proxy, 'SETTINGS', {'upstream_url': endpoint}), \
                 mock.patch.object(proxy.urllib.request, 'urlopen', return_value=response) as request:
                handler.forward(b'{}')
                self.assertEqual(request.call_args.args[0].full_url, endpoint+route)
                self.assertEqual(handler.wfile.getvalue(), b'{}')

    def test_catalog_auth_error_names_endpoint_without_token(self):
        endpoint = 'https://copilot-api.microsoft.ghe.com'
        state = {'provider': 'enterprise', 'runtime': '.', 'changed': {'base_url': 'http://127.0.0.1:43129'}, 'upstream_url': endpoint}
        cfg = {'model_providers': {'enterprise': {}}}
        for status in (401, 403):
            body = io.BytesIO(b'SECRET_TOKEN')
            error = manage.urllib.error.HTTPError('http://localhost/models?token=SECRET_TOKEN', status, 'Unauthorized', {}, body)
            output = io.StringIO()
            with mock.patch.object(manage, 'ensure_defaults', return_value={'client_version': '0.1.0'}), \
                 mock.patch.object(manage, 'local_json', side_effect=error), mock.patch.object(sys, 'stdout', output):
                with self.assertRaisesRegex(RuntimeError, f'HTTP {status}') as exc:
                    manage.catalog_check(state, cfg, b'SECRET_TOKEN')
            self.assertIn(endpoint+'/models', str(exc.exception))
            self.assertNotIn('SECRET_TOKEN', str(exc.exception) + output.getvalue())
            self.assertTrue(body.closed)


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

    def orphaned_config_fixture(self):
        text = self.original.replace('some_existing_flag = true', 'some_existing_flag = false')
        text = manage.field(text, 'model_providers.enterprise', 'base_url', f'http://127.0.0.1:{self.port}')
        text = manage.field(text, 'model_providers.enterprise.auth', 'args', [str(self.dest/'auth_bridge.py')])
        self.config.write_text(text, encoding='utf-8-sig')
        return self.config.read_bytes()

    def test_force_config_repairs_deleted_installation_and_backs_up_exact_bytes(self):
        before = self.orphaned_config_fixture()
        with mock.patch.object(manage.shutil, 'which', return_value='C:\\Synthetic gh.exe'), \
             mock.patch.object(manage, 'credential', return_value=b'SYNTHETIC_TEST_CREDENTIAL') as credential, \
             mock.patch.object(manage, 'ensure_defaults'), \
             mock.patch.object(manage.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=b'SYNTHETIC_TEST_CREDENTIAL')), \
             mock.patch.object(manage, 'local_json', side_effect=lambda *a, **k: {'installation': manage.settings_at(self.dest)['installation']}):
            manage.install(self.config, self.dest, self.port, hostname='microsoft.ghe.com', force_config=True)
            credential.assert_called_once_with('C:\\Synthetic gh.exe', ['auth', 'token', '--hostname', 'microsoft.ghe.com'], 5000)
            state = manage.settings_at(self.dest)
            self.assertEqual(state['old']['base_url'], 'https://copilot-api.microsoft.ghe.com')
            self.assertEqual(state['upstream_url'], 'https://copilot-api.microsoft.ghe.com')
            self.assertEqual(state['old']['command'], 'gh')
            self.assertEqual(state['old']['args'], ['auth', 'token', '--hostname', 'microsoft.ghe.com'])
            backups = list(self.root.glob(self.config.name+'.before-force-*.bak'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), before)
            saved = tomllib.loads((self.dest/'config.before.toml').read_text(encoding='utf-8'))
            self.assertEqual(saved['model_providers']['enterprise']['base_url'], 'https://copilot-api.microsoft.ghe.com')
            self.assertFalse(saved['features']['some_existing_flag'])
            self.assertEqual(saved['model_providers']['enterprise']['http_headers'], {'Editor-Version': 'existing-header'})
            manage.uninstall(self.dest)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding='utf-8')), saved)

    def test_orphaned_config_is_not_overwritten_without_force(self):
        before = self.orphaned_config_fixture()
        with self.assertRaisesRegex(RuntimeError, '--force-config'):
            manage.install(self.config, self.dest, self.port, hostname='microsoft.ghe.com')
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.dest.exists())

    def test_force_auth_failure_leaves_original_config_untouched(self):
        before = self.orphaned_config_fixture()
        with mock.patch.object(manage.shutil, 'which', return_value='C:\\Synthetic gh.exe'), \
             mock.patch.object(manage, 'credential', side_effect=RuntimeError('Synthetic auth failure')):
            with self.assertRaisesRegex(RuntimeError, 'auth failure'):
                manage.install(self.config, self.dest, self.port, hostname='microsoft.ghe.com', force_config=True)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.dest.exists())

    def test_force_builds_missing_auth_table_without_default_hostname(self):
        original = self.original.split('[model_providers.enterprise.auth]')[0]
        restored = manage.force_copilot_connection(original, tomllib.loads(original), None)
        p = tomllib.loads(restored)['model_providers']['enterprise']
        self.assertEqual(p['auth'], {'command': 'gh', 'args': ['auth', 'token']})
        self.assertEqual(p['base_url'], manage.UPSTREAM)

    def test_force_alias_is_forwarded_and_rejected_for_other_actions(self):
        for flag in ('--force-config', '--force'):
            with mock.patch.object(sys, 'argv', ['manage.py', 'install', flag, '--hostname', 'microsoft.ghe.com']), \
                 mock.patch.object(manage, 'install') as install, mock.patch.object(sys, 'stdout', io.StringIO()):
                self.assertEqual(manage.main(), 0)
                self.assertTrue(install.call_args.kwargs['force_config'])
                self.assertEqual(install.call_args.args[5], 'microsoft.ghe.com')
        with mock.patch.object(sys, 'argv', ['manage.py', 'update', '--force']), mock.patch.object(sys, 'stderr', io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                manage.main()
            self.assertEqual(exc.exception.code, 2)

    def test_force_can_retry_after_catalog_failure_without_deleting_folder(self):
        before = self.orphaned_config_fixture()
        with mock.patch.object(manage.shutil, 'which', return_value='C:\\Synthetic gh.exe'), \
             mock.patch.object(manage, 'credential', return_value=b'SYNTHETIC_TEST_CREDENTIAL'), \
             mock.patch.object(manage, 'ensure_defaults', side_effect=RuntimeError('Synthetic catalog failure')), \
             mock.patch.object(manage, 'local_json', return_value={}):
            for attempt in range(2):
                with self.assertRaisesRegex(RuntimeError, 'catalog failure'):
                    manage.install(self.config, self.dest, self.port, hostname='microsoft.ghe.com', force_config=True)
                self.assertFalse((self.dest/'settings.json').exists())
                self.assertTrue((self.dest/'settings.failed.json').is_file())
                self.assertEqual(self.config.read_bytes(), before)

    def uninstalled_fixture(self):
        self.dest.mkdir()
        # Match the user's legacy 1.0 directory: no settings.json or catalog.py.
        state = {'version': '1.0.0', 'installation': 'legacy-test', 'config': str(self.config), 'port': self.port}
        (self.dest/'settings.uninstalled.legacy-test.json').write_text(json.dumps(state), encoding='utf-8')
        for name in ('auth_bridge.py', 'check.cmd', 'config.before.toml', 'install.cmd', 'manage.py', 'proxy.py', 'README.md', 'uninstall.cmd'):
            (self.dest/name).write_bytes(('Previous contents: ' + name).encode())
        (self.dest/'personal-notes.txt').write_text('Keep this unrelated file.', encoding='utf-8')
        return {p.name: p.read_bytes() for p in self.dest.iterdir()}

    def stale_uninstalled_fixture(self, original_auth=False):
        self.uninstalled_fixture()
        marker = self.dest/'settings.uninstalled.legacy-test.json'
        state = json.loads(marker.read_text(encoding='utf-8'))
        cfg = tomllib.loads(self.original)
        p = cfg['model_providers']['enterprise']
        a = p['auth']
        state.update(provider='enterprise', auth_command=a['command'], auth_args=a['args'], auth_timeout_ms=5000,
                     changed={'base_url': f'http://127.0.0.1:{self.port}', 'command': sys.executable,
                              'args': [str(self.dest/'auth_bridge.py')], 'timeout_ms': 15000},
                     old={'base_url': p['base_url'], 'command': a['command'], 'args': a['args'], 'timeout_ms': None},
                     old_present={'args': True, 'timeout_ms': False})
        marker.write_text(json.dumps(state), encoding='utf-8')
        text = self.original.replace('some_existing_flag = true', 'some_existing_flag = false')
        for key, value in state['changed'].items():
            if original_auth and key != 'base_url':
                continue
            section = 'model_providers.enterprise' + ('' if key == 'base_url' else '.auth')
            text = manage.field(text, section, key, value)
        self.config.write_text(text, encoding='utf-8')
        return state

    def test_reinstall_recovers_stale_bridge_and_uninstall_uses_upstream(self):
        record = self.stale_uninstalled_fixture()
        expected = tomllib.loads(self.original)
        expected['features']['some_existing_flag'] = False
        manage.update(self.dest, hostname='microsoft.ghe.com')
        state = manage.settings_at(self.dest)
        self.assertEqual(state['old'], record['old'])
        self.assertEqual(state['auth_args'][-2:], ['--hostname', 'microsoft.ghe.com'])
        self.assertNotIn(str(self.dest/'auth_bridge.py'), state['auth_args'])
        self.assertEqual(tomllib.loads((self.dest/'config.before.toml').read_text(encoding='utf-8')), expected)
        self.assertTrue(auth_bridge.healthy(state))
        manage.uninstall(self.dest)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding='utf-8')), expected)

    def test_recovery_handles_already_restored_auth_fields(self):
        record = self.stale_uninstalled_fixture(original_auth=True)
        text = self.config.read_text(encoding='utf-8')
        restored = manage.recover_uninstalled_connection(text, tomllib.loads(text), record)
        expected = tomllib.loads(self.original)
        expected['features']['some_existing_flag'] = False
        self.assertEqual(tomllib.loads(restored), expected)
        self.assertEqual(self.config.read_text(encoding='utf-8'), text)

    def test_recovery_auth_failure_does_not_change_config_or_old_files(self):
        self.stale_uninstalled_fixture()
        before = self.config.read_bytes()
        files = {p.name: p.read_bytes() for p in self.dest.iterdir()}
        with mock.patch.object(manage, 'credential', side_effect=RuntimeError('Synthetic auth failure')):
            with self.assertRaisesRegex(RuntimeError, 'auth failure'):
                manage.update(self.dest, hostname='microsoft.ghe.com')
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual({p.name: p.read_bytes() for p in self.dest.iterdir()}, files)

    def test_recovery_refuses_modified_bridge_auth_args(self):
        self.stale_uninstalled_fixture()
        text = manage.field(self.config.read_text(encoding='utf-8'), 'model_providers.enterprise.auth', 'args', ['newer-helper.py'])
        self.config.write_text(text, encoding='utf-8')
        with mock.patch.object(manage, 'credential') as credential:
            with self.assertRaisesRegex(RuntimeError, 'modified connection field'):
                manage.update(self.dest)
            credential.assert_not_called()
        self.assertEqual(self.config.read_text(encoding='utf-8'), text)

    def test_install_reuses_uninstalled_directory_and_preserves_backups(self):
        previous = self.uninstalled_fixture()
        manage.install(self.config, self.dest, self.port, hostname='microsoft.ghe.com')
        backup = next(self.dest.glob('reinstall-backup-*'))
        self.assertEqual((backup/'config.before.toml').read_bytes(), previous['config.before.toml'])
        self.assertEqual((backup/'auth_bridge.py').read_bytes(), previous['auth_bridge.py'])
        for name in ('settings.uninstalled.legacy-test.json', 'personal-notes.txt'):
            self.assertEqual((self.dest/name).read_bytes(), previous[name])
        self.assertEqual(manage.settings_at(self.dest)['auth_args'][-2:], ['--hostname', 'microsoft.ghe.com'])
        self.assertTrue(auth_bridge.healthy(manage.settings_at(self.dest)))
        manage.uninstall(self.dest)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding='utf-8')), tomllib.loads(self.original))

    def test_update_reinstalls_legacy_uninstalled_directory(self):
        self.uninstalled_fixture()
        manage.update(self.dest, hostname='microsoft.ghe.com')
        state = manage.settings_at(self.dest)
        self.assertEqual(state['config'], str(self.config))
        self.assertEqual(state['port'], self.port)
        self.assertEqual(state['version'], manage.VERSION)
        self.assertEqual(state['auth_args'][-2:], ['--hostname', 'microsoft.ghe.com'])

    def test_reinstall_auth_failure_leaves_old_files_untouched(self):
        previous = self.uninstalled_fixture()
        with mock.patch.object(manage, 'credential', side_effect=RuntimeError('Synthetic auth failure')):
            with self.assertRaisesRegex(RuntimeError, 'auth failure'):
                manage.install(self.config, self.dest, self.port)
        self.assertEqual({p.name: p.read_bytes() for p in self.dest.iterdir()}, previous)
        self.assertEqual(self.config.read_text(encoding='utf-8'), self.original)

    def test_reinstall_copy_failure_restores_old_files_and_can_retry(self):
        previous = self.uninstalled_fixture()
        with mock.patch.object(manage, 'ensure_defaults', side_effect=RuntimeError('Synthetic catalog failure')):
            with self.assertRaisesRegex(RuntimeError, 'catalog failure'):
                manage.install(self.config, self.dest, self.port)
        self.assertEqual({p.name: p.read_bytes() for p in self.dest.iterdir() if p.is_file()}, previous)
        self.assertFalse((self.dest/'settings.json').exists())
        self.assertEqual(self.config.read_text(encoding='utf-8'), self.original)
        manage.install(self.config, self.dest, self.port)
        self.assertTrue(auth_bridge.healthy(manage.settings_at(self.dest)))

    def test_unrelated_or_invalid_uninstall_directory_is_not_overwritten(self):
        self.dest.mkdir()
        notes = self.dest/'notes.txt'
        notes.write_text('Keep me', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'no valid uninstall state'):
            manage.install(self.config, self.dest, self.port)
        marker = self.dest/'settings.uninstalled.old.json'
        for content, message in (('invalid json', 'Cannot read uninstall state'),
                                 (json.dumps({'installation': 'old', 'config': str(self.root/'different.toml')}), 'different config')):
            marker.write_text(content, encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, message):
                manage.install(self.config, self.dest, self.port)
            self.assertEqual(marker.read_text(encoding='utf-8'), content)
        self.assertEqual(notes.read_text(encoding='utf-8'), 'Keep me')
        self.assertFalse((self.dest/'settings.json').exists())

    def test_cli_shows_default_and_resolved_destination(self):
        env = dict(os.environ, LOCALAPPDATA=str(self.root))
        result = subprocess.run([sys.executable, str(Path(manage.__file__)), 'update', '--help'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn(''.join(str(self.root/'CodexCopilotCompat').split()), ''.join(result.stdout.split()))
        result = subprocess.run([sys.executable, str(Path(manage.__file__)), 'update', '--dest', str(self.root/'missing')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Installation directory: ' + str(self.root/'missing'), result.stdout)
        self.assertIn(str(self.root/'missing'), result.stderr)

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
        old.pop('upstream_url')
        (self.dest/'settings.json').write_text(json.dumps(old), encoding='utf-8')
        # A 1.1.0 install has neither launcher and its helper has no CLI parser.
        (self.dest/'restart.cmd').unlink()
        (self.dest/'update.cmd').unlink()
        (self.dest/'upstream.py').unlink()
        legacy_helper = b"import json, pathlib, subprocess, sys\ns = json.loads(pathlib.Path(__file__).with_name('settings.json').read_text())\nsys.stdout.buffer.write(subprocess.check_output([s['auth_command'], *s['auth_args']]))\n"
        (self.dest/'auth_bridge.py').write_bytes(legacy_helper)
        config_before = self.config.read_bytes()
        uninstall_backup = (self.dest/'config.before.toml').read_bytes()
        manage.update(self.dest, hostname='enterprise.example.test')
        updated = manage.settings_at(self.dest)
        expected = copy.deepcopy(old)
        expected.update(version=manage.VERSION, auth_args=auth_bridge.auth_args(old['auth_args'], 'enterprise.example.test'), upstream_url=DEFAULT_UPSTREAM)
        self.assertEqual(updated, expected)
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertEqual((self.dest/'config.before.toml').read_bytes(), uninstall_backup)
        backup = next(self.dest.glob('update-backup-*'))
        self.assertEqual((backup/'auth_bridge.py').read_bytes(), legacy_helper)
        self.assertEqual(json.loads((backup/'settings.json').read_text()), old)
        self.assertTrue((self.dest/'update.cmd').is_file())
        self.assertTrue(auth_bridge.healthy(updated))
        updated['version'] = '1.1.1'
        (self.dest/'settings.json').write_text(json.dumps(updated), encoding='utf-8')
        # The installed package can be reused to change hostname, then updated
        # without an override while retaining that choice.
        manage.update(self.dest, hostname='second.example.test', source=self.dest)
        manage.update(self.dest, source=self.dest)
        self.assertEqual(manage.settings_at(self.dest)['auth_args'][-2:], ['--hostname', 'second.example.test'])
        manage.uninstall(self.dest)
        self.assertEqual(tomllib.loads(self.config.read_text(encoding='utf-8')), tomllib.loads(self.original))

    def test_update_switches_credential_host_and_live_proxy_endpoint_together(self):
        manage.install(self.config, self.dest, self.port, hostname='microsoft.ghe.com')
        for hostname, endpoint in (('another.ghe.com', 'https://copilot-api.another.ghe.com'), ('github.com', DEFAULT_UPSTREAM)):
            manage.update(self.dest, hostname=hostname)
            state = manage.settings_at(self.dest)
            self.assertEqual(state['auth_args'][-2:], ['--hostname', hostname])
            self.assertEqual(state['upstream_url'], endpoint)
            health = manage.local_json(state['changed']['base_url']+'/health')
            self.assertEqual(health['upstream_url'], endpoint)

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
