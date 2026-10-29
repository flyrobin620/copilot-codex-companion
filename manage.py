"""Windows per-user installer. Standard library only, Python 3.11+."""
import sys
if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11 or newer is required. No configuration was changed.')
import argparse
import contextlib
import copy
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from catalog import ensure_defaults
from auth_bridge import auth_args, restart_bridge, start_bridge, stop_bridge

VERSION = '1.1.2'
UPSTREAM = 'https://api.enterprise.githubcopilot.com'
FILES = ('proxy.py', 'catalog.py', 'auth_bridge.py', 'manage.py', 'install.cmd', 'check.cmd', 'restart.cmd', 'update.cmd', 'uninstall.cmd', 'README.md')
CONTEXT_KEYS = ('model_context_window','model_auto_compact_token_limit')


def top_field(text,key,value=None,remove=False):
    end=re.search(r'(?m)^\[',text)
    offset=end.start() if end else len(text)
    block=text[:offset]
    pattern=r'(?m)^'+re.escape(key)+r'[ \t]*=.*(?:\n|$)'
    count=len(re.findall(pattern,block))
    if count>1: raise RuntimeError('Duplicate top-level setting: '+key)
    replacement='' if remove else key+' = '+json.dumps(value)+'\n'
    if count: block=re.sub(pattern,lambda _:replacement,block)
    elif not remove: block+=replacement
    return block+text[offset:]


def field(text, section, key, value, remove=False):
    pattern = r'(?ms)(^\[' + re.escape(section) + r'\][^\S\n]*(?:#[^\n]*)?\n)(.*?)(?=^\[|\Z)'
    match = re.search(pattern, text)
    if not match:
        raise RuntimeError(f'Cannot safely edit [{section}]; use a separate standard TOML table for it.')
    block = match.group(2)
    key_pattern = r'(?m)^[ \t]*' + re.escape(key) + r'[ \t]*=.*(?:\n|$)'
    count = len(re.findall(key_pattern, block))
    if count > 1:
        raise RuntimeError('Duplicate configuration field: ' + key)
    replacement = '' if remove else key + ' = ' + json.dumps(value, ensure_ascii=False) + '\n'
    if count:
        block = re.sub(key_pattern, lambda _: replacement, block)
    elif not remove:
        block = block.rstrip('\n') + '\n' + replacement + '\n'
    return text[:match.start(2)] + block + text[match.end(2):]


def replace_config(path, previous, updated):
    tomllib.loads(updated)
    if path.read_text(encoding='utf-8-sig') != previous:
        raise RuntimeError('Configuration changed during installation; retry after other edits finish.')
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as f:
        f.write(updated)
        tmp = Path(f.name)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def credential(command, args, timeout):
    try:
        r = subprocess.run([command, *args], capture_output=True, timeout=timeout/1000)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError('Existing credential helper could not run or timed out.') from None
    if r.returncode or not r.stdout.strip():
        raise RuntimeError('The existing credential helper failed. Renew its login first; nothing was changed.')
    return r.stdout.strip()


def local_json(url, data=None, headers=None, timeout=3):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with opener.open(request, timeout=timeout) as r:
        return json.load(r)


def settings_at(dest):
    return json.loads((dest/'settings.json').read_text(encoding='utf-8'))


def uninstalled_state(dest):
    markers = sorted(dest.glob('settings.uninstalled.*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    if not markers:
        return None
    marker = markers[0]
    try:
        state = json.loads(marker.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or not isinstance(state.get('config'), str)
                or not state['config'] or not state.get('installation')
                or marker.name != f"settings.uninstalled.{state['installation']}.json"):
            raise ValueError('Invalid uninstall state')
        return state
    except (OSError, ValueError):
        raise RuntimeError(f'Cannot read uninstall state at {marker}; existing files were preserved.') from None


@contextlib.contextmanager
def preserve_uninstalled_files(dest, reinstall):
    if not reinstall:
        yield
        return
    names = (*FILES, 'settings.json', 'catalog-defaults.json', 'config.before.toml')
    previous = {name: (dest/name).read_bytes() if (dest/name).exists() else None for name in names}
    backup = Path(tempfile.mkdtemp(prefix='reinstall-backup-', dir=dest))
    for name, data in previous.items():
        if data is not None:
            (backup/name).write_bytes(data)
    print(f'Uninstalled installation detected. Previous files backed up to: {backup}')
    try:
        yield
    except Exception:
        for name, data in previous.items():
            if data is None:
                (dest/name).unlink(missing_ok=True)
            else:
                write_atomic(dest/name, data)
        print(f'Reinstall failed; previous files restored. Backup retained at {backup}', file=sys.stderr)
        raise


def catalog_check(state,cfg,token):
    p=cfg['model_providers'][state['provider']]
    headers=dict(p.get('http_headers',{}))
    headers.update({key:os.environ[env] for key,env in p.get('env_http_headers',{}).items() if env in os.environ})
    headers['Authorization']='Bearer '+token.decode().strip()
    saved=ensure_defaults(Path(state['runtime']),state.get('codex_command','codex'))
    catalog=local_json(state['changed']['base_url']+'/models?client_version='+saved['client_version'],headers=headers,timeout=40)
    selected=next((m for m in catalog['models'] if m['slug']==cfg['model']),None)
    if not selected:
        raise RuntimeError('The selected model is not enabled or lacks matching Codex defaults; limits were not changed.')
    return selected


def install(config, dest, port, automatic_context=False, codex_command='codex', hostname=None, source=None):
    if os.name != 'nt':
        raise RuntimeError('This package supports Windows only.')
    if (dest/'settings.json').exists():
        s = settings_at(dest)
        if s['config'] != str(config):
            raise RuntimeError('This installation belongs to a different config. Choose another --dest and --port.')
        print('Already installed. Use update.cmd to update or change --hostname, or check.cmd to verify.')
        return
    reinstall = None
    if dest.exists() and any(dest.iterdir()):
        reinstall = uninstalled_state(dest)
        if reinstall is None:
            raise RuntimeError(f'Destination {dest} is not empty and has no valid uninstall state. Choose another --dest; existing files will not be overwritten.')
        if Path(reinstall['config']).resolve() != config.resolve():
            raise RuntimeError('Uninstalled files belong to a different config. Use its --config or choose another --dest.')
    if not config.is_file():
        raise RuntimeError('No Codex config found. Configure a working Copilot Enterprise connection first.')
    original = config.read_text(encoding='utf-8-sig')
    cfg = tomllib.loads(original)
    provider = cfg.get('model_provider', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', provider):
        raise RuntimeError('A simple, top-level custom model_provider name is required.')
    p = cfg.get('model_providers', {}).get(provider, {})
    if cfg.get('model_catalog_json') or p.get('model_catalog_url'):
        raise RuntimeError('An explicit model catalog is configured. Resolve that override before installing this catalog adapter.')
    if p.get('base_url', '').rstrip('/') != UPSTREAM or p.get('wire_api', 'responses') != 'responses':
        raise RuntimeError('This package requires an existing Copilot Enterprise Responses connection.')
    if p.get('supports_websockets'):
        raise RuntimeError('This adapter supports HTTP/SSE only. WebSocket configuration was left unchanged.')
    a = p.get('auth', {})
    if not a.get('command') or p.get('env_key') or p.get('requires_openai_auth'):
        raise RuntimeError('A working command-backed provider auth helper is required.')
    command = shutil.which(a['command'])
    if not command:
        raise RuntimeError('Existing credential helper was not found: ' + a['command'])
    args = a.get('args', [])
    if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
        raise RuntimeError('Invalid credential-helper argument list.')
    args = auth_args(args, hostname)
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise RuntimeError('pythonw.exe is required; use a standard Windows Python installation.')
    if not 1024 <= port <= 65535:
        raise RuntimeError('Choose a port between 1024 and 65535.')
    with socket.socket() as sock:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            raise RuntimeError(f'Port {port} is occupied. Retry install.cmd --port 43130, for example.')
    timeout = int(a.get('timeout_ms', 5000))
    credential(command, args, timeout)
    changed = {'base_url': f'http://127.0.0.1:{port}', 'command': sys.executable,
               'args': [str(dest/'auth_bridge.py')], 'timeout_ms': max(15000, timeout+7000)}
    updated = original
    section = 'model_providers.' + provider
    for key, value in changed.items():
        updated = field(updated, section if key == 'base_url' else section+'.auth', key, value)
    expected = copy.deepcopy(cfg)
    expected['model_providers'][provider]['base_url'] = changed['base_url']
    expected['model_providers'][provider]['auth'].update({k:v for k,v in changed.items() if k != 'base_url'})
    removed_context={k:cfg[k] for k in CONTEXT_KEYS if automatic_context and k in cfg}
    for key in removed_context:
        updated=top_field(updated,key,remove=True)
        expected.pop(key)
    if tomllib.loads(updated) != expected:
        raise RuntimeError('Configuration validation failed; no configuration was changed.')
    source = source or Path(__file__).resolve().parent
    payload = {name: (source/name).read_bytes() for name in FILES}
    with preserve_uninstalled_files(dest, reinstall):
        dest.mkdir(parents=True, exist_ok=True)
        for name, data in payload.items():
            write_atomic(dest/name, data)
        ensure_defaults(dest,codex_command)
        state = {'version': VERSION, 'installation': secrets.token_hex(16), 'control': secrets.token_hex(32),
                 'config': str(config), 'runtime':str(dest), 'codex_command':codex_command,
                 'removed_context_overrides':removed_context, 'provider': provider, 'port': port, 'auth_command': command,
                 'auth_args': args, 'auth_timeout_ms': timeout, 'changed': changed,
                 'old': {'base_url':p['base_url'], 'command':a['command'], 'args':a.get('args'), 'timeout_ms':a.get('timeout_ms')},
                 'old_present': {k: k in a for k in ('args', 'timeout_ms')}}
        (dest/'config.before.toml').write_text(original, encoding='utf-8')
        (dest/'settings.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        try:
            # Start and verify the installed runtime before changing the active connection.
            r = subprocess.run([sys.executable, str(dest/'auth_bridge.py')], capture_output=True,
                               timeout=changed['timeout_ms']/1000)
            if r.returncode or not r.stdout.strip():
                raise RuntimeError('Installed helper did not start successfully; active configuration remains unchanged.')
            health = local_json(changed['base_url']+'/health')
            if health.get('installation') != state['installation']:
                raise RuntimeError('Unexpected service on local port; active configuration remains unchanged.')
            selected=catalog_check(state,cfg,r.stdout.strip())
            print('Catalog verified: '+selected['slug']+' input context='+str(selected['context_window']))
            replace_config(config, original, updated)
        except Exception:
            try:
                local_json(changed['base_url']+'/shutdown', b'', {'X-Compat-Control':state['control']})
            except Exception:
                pass
            (dest/'settings.json').rename(dest/'settings.failed.json')
            raise
        print(f'Installed {VERSION} in {dest}')
        print('Existing provider, model, headers and login were preserved. No credential was printed.')
        if removed_context:
            print('Manual context/compaction overrides removed after catalog validation; uninstall can restore them.')
        print('Finish active tasks, fully exit and reopen Codex, then test in a new chat.')


def write_atomic(path, data):
    with tempfile.NamedTemporaryFile('wb', dir=path.parent, delete=False) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def update(dest, hostname=None, source=None, config=None, port=None, automatic_context=False, codex_command=None):
    """Update an existing 1.1 installation, retaining its identity and uninstall state."""
    if os.name != 'nt':
        raise RuntimeError('This package supports Windows only.')
    dest = dest.resolve()
    if not (dest/'settings.json').is_file():
        previous = uninstalled_state(dest)
        if previous is not None:
            print('The previous installation was uninstalled; reinstalling from the current package.')
            return install(config or Path(previous['config']), dest,
                           port if port is not None else previous.get('port', 43129),
                           automatic_context, codex_command or previous.get('codex_command', 'codex'), hostname, source)
        raise RuntimeError(f'No active installation or uninstall state found at {dest}. Use install.cmd for a new installation.')
    lock = dest/'update.lock'
    try:
        handle = lock.open('x')
    except FileExistsError:
        raise RuntimeError('An update lock exists. Wait for the other update; if it was interrupted, inspect its backup before removing update.lock.') from None
    try:
        handle.close()
        _update(dest, hostname, (source or Path(__file__).resolve().parent).resolve())
    finally:
        lock.unlink(missing_ok=True)


def _update(dest, hostname, source):
    state = settings_at(dest)
    if state.get('version') not in ('1.1.0', '1.1.1', VERSION):
        raise RuntimeError('Only existing 1.1.0, 1.1.1 and 1.1.2 installations are supported.')
    if Path(state['runtime']).resolve() != dest:
        raise RuntimeError('Installation path mismatch. Use the original installation directory.')
    cfg = tomllib.loads(Path(state['config']).read_text(encoding='utf-8-sig'))
    provider = cfg.get('model_providers', {}).get(state['provider'], {})
    for key, value in state['changed'].items():
        actual = provider.get(key) if key == 'base_url' else provider.get('auth', {}).get(key)
        if actual != value:
            raise RuntimeError('Active connection settings changed; update was not attempted.')
    # Read the complete package before stopping the existing service, including when
    # running from the installed directory solely to switch hostname.
    payload = {name: (source/name).read_bytes() for name in FILES}
    updated = copy.deepcopy(state)
    updated['version'] = VERSION
    updated['auth_args'] = auth_args(state['auth_args'], hostname)
    credential(updated['auth_command'], updated['auth_args'], updated['auth_timeout_ms'])
    if not Path(sys.executable).with_name('pythonw.exe').is_file():
        raise RuntimeError('pythonw.exe is required; no installation files were changed.')
    names = (*FILES, 'settings.json', 'catalog-defaults.json')
    previous = {name: (dest/name).read_bytes() if (dest/name).exists() else None for name in names}
    backup = Path(tempfile.mkdtemp(prefix='update-backup-', dir=dest))
    for name, data in previous.items():
        if data is not None:
            (backup/name).write_bytes(data)
    (backup/'manifest.json').write_text(json.dumps({'absent': [name for name, data in previous.items() if data is None]}, indent=2), encoding='utf-8')
    print(f'Update backup: {backup}')
    stop_bridge(state)
    try:
        for name, data in payload.items():
            if previous[name] != data:
                write_atomic(dest/name, data)
        write_atomic(dest/'settings.json', json.dumps(updated, indent=2).encode('utf-8'))
        start_bridge(dest, updated)
        check(dest)
    except Exception as exc:
        try:
            stop_bridge(updated)
            for name, data in previous.items():
                if data is None:
                    (dest/name).unlink(missing_ok=True)
                else:
                    write_atomic(dest/name, data)
            start_bridge(dest, state)
        except Exception as recovery:
            raise RuntimeError(f'Update failed ({type(exc).__name__}); automatic recovery failed ({type(recovery).__name__}). Backup retained at {backup}.') from None
        raise RuntimeError(f'Update failed ({type(exc).__name__}); previous files and settings restored and bridge restarted. Backup: {backup}') from None
    print(f'Updated to {VERSION}; bridge restarted. Configuration and uninstall backup preserved.')
    print('Reopen Codex to refresh any cached credentials, especially after changing hostname.')


def check(dest, live=False):
    s = settings_at(dest)
    cfg = tomllib.loads(Path(s['config']).read_text(encoding='utf-8-sig'))
    p = cfg['model_providers'][s['provider']]
    if p.get('base_url') != s['changed']['base_url'] or p.get('auth', {}).get('args') != s['changed']['args']:
        raise RuntimeError('Active configuration does not point to this installation.')
    token = credential(s['changed']['command'], s['changed']['args'], s['changed']['timeout_ms'])
    health = local_json(s['changed']['base_url']+'/health')
    if health.get('installation') != s['installation']:
        raise RuntimeError('Local service identity mismatch.')
    print('PASS: configuration, local service and original credential helper.')
    selected=catalog_check(s,cfg,token)
    print('PASS: Codex model catalog; '+selected['slug']+' input context='+str(selected['context_window']))
    overrides={k:cfg[k] for k in CONTEXT_KEYS if k in cfg}
    if overrides:
        print('Manual overrides remain: '+json.dumps(overrides))
    if not live:
        print('Use check.cmd --live to send one synthetic API request and verify stream IDs.')
        return
    headers = dict(p.get('http_headers', {}))
    for key, env in p.get('env_http_headers', {}).items():
        if env in os.environ: headers[key] = os.environ[env]
    headers.update({'Authorization':'Bearer '+token.decode().strip(), 'Content-Type':'application/json', 'Accept':'text/event-stream'})
    body = {'model':cfg['model'], 'input':[{'role':'user','content':'Reply with exactly COMPAT_DISTRIBUTION_OK.'}], 'stream':True, 'store':False}
    req = urllib.request.Request(s['changed']['base_url']+'/responses', data=json.dumps(body).encode(), headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    ids = {}; complete = False; text = ''
    with opener.open(req, timeout=120) as response:
        for line in response:
            if not line.startswith(b'data:'): continue
            raw = line[5:].strip()
            if raw == b'[DONE]': break
            e = json.loads(raw)
            index = e.get('output_index')
            mid = e.get('item', {}).get('id', e.get('item_id'))
            if index is not None and mid: ids.setdefault(index, set()).add(mid)
            if e.get('type') == 'response.completed':
                complete = True
                for index, item in enumerate(e['response'].get('output', [])):
                    if item.get('id'): ids.setdefault(index, set()).add(item['id'])
                    if item.get('type') == 'message':
                        text += ''.join(c.get('text','') for c in item.get('content',[]))
    if not complete or not ids or any(len(values) != 1 for values in ids.values()):
        raise RuntimeError('Live stream validation failed.')
    if text.strip() != 'COMPAT_DISTRIBUTION_OK':
        raise RuntimeError('Stream was stable, but the synthetic response did not match the expected text.')
    print('PASS: live Copilot response completed; message IDs stayed consistent.')
    print('Desktop rendering still needs to be checked after restarting the app.')


def uninstall(dest):
    s = settings_at(dest)
    config = Path(s['config'])
    original = config.read_text(encoding='utf-8-sig')
    cfg = tomllib.loads(original)
    p = cfg['model_providers'][s['provider']]
    for key, value in s['changed'].items():
        actual = p.get(key) if key == 'base_url' else p.get('auth', {}).get(key)
        if actual != value:
            raise RuntimeError('Modified connection settings detected; refusing to overwrite newer changes.')
    section = 'model_providers.' + s['provider']
    updated = original
    expected = copy.deepcopy(cfg)
    for key, value in s['old'].items():
        remove = key in s['old_present'] and not s['old_present'][key]
        updated = field(updated, section if key == 'base_url' else section+'.auth', key, value, remove)
        target = expected['model_providers'][s['provider']] if key == 'base_url' else expected['model_providers'][s['provider']]['auth']
        if remove: target.pop(key, None)
        else: target[key] = value
    for key,value in s.get('removed_context_overrides',{}).items():
        if key not in cfg:
            updated=top_field(updated,key,value)
            expected[key]=value
        else:
            print('Preserving newer manual setting: '+key)
    if tomllib.loads(updated) != expected:
        raise RuntimeError('Restore validation failed; configuration was not changed.')
    replace_config(config, original, updated)
    try:
        local_json(s['changed']['base_url']+'/shutdown', b'', {'X-Compat-Control':s['control']})
        print('Local adapter stopped.')
    except Exception:
        print('Connection restored; local adapter was already stopped or could not be reached.')
    (dest/'settings.json').rename(dest/('settings.uninstalled.'+s['installation']+'.json'))
    print('Original connection restored. Backup files were retained. Restart Codex.')


def main():
    here = Path(__file__).resolve().parent
    default_dest = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local')))/'CodexCopilotCompat'
    if (here/'settings.json').exists() or any(here.glob('settings.uninstalled.*.json')):
        default_dest = here
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'update', 'check', 'restart', 'uninstall'])
    parser.add_argument('--port', type=int, help='Local port for installation (default: 43129; reinstall via update reuses the saved port).')
    parser.add_argument('--dest', type=Path, help=f'Installation directory (default: {default_dest.resolve()}).')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--hostname', help='Persist the credential helper hostname when installing or updating.')
    parser.add_argument('--automatic-context',action='store_true',help='Remove manual context/compaction overrides after validating the live model catalog.')
    parser.add_argument('--codex',help='Path to the matching Codex binary (default: codex; reinstall via update reuses the saved command).')
    options = parser.parse_args()
    if options.hostname is not None and options.action not in ('install', 'update'):
        parser.error('--hostname is only supported with install or update.')
    dest = (options.dest or default_dest).resolve()
    config = (options.config or Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex')))/'config.toml').resolve()
    try:
        print(f'Installation directory: {dest}')
        if options.action == 'install': install(config, dest, options.port if options.port is not None else 43129,options.automatic_context,options.codex or 'codex',options.hostname)
        elif options.action == 'update': update(dest, options.hostname, config=config if options.config else None,
                                               port=options.port, automatic_context=options.automatic_context, codex_command=options.codex)
        elif options.action == 'check': check(dest, options.live)
        elif options.action == 'restart':
            restart_bridge(dest, settings_at(dest))
            print('Local bridge restarted.')
        else: uninstall(dest)
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
