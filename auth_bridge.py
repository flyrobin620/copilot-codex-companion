"""Reuse this machine's original credential helper; never print diagnostics to stdout."""
import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from catalog import ensure_defaults
from upstream import select_upstream, settings_upstream


ROOT = Path(__file__).resolve().parent


def auth_args(args, hostname=None):
    """Override the helper's hostname without adding a default host."""
    if hostname is None:
        return list(args)
    if not hostname.strip() or hostname.startswith('-'):
        raise ValueError('Hostname must be nonempty and cannot start with a dash.')
    result = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--':
            break
        if arg == '--hostname':
            index += 1
            if index < len(args) and not args[index].startswith('-'):
                index += 1
            continue
        if not arg.startswith('--hostname='):
            result.append(arg)
        index += 1
    return [*result, '--hostname', hostname, *args[index:]]


def health(settings):
    # Loopback management must not use a corporate HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        # Windows can take about two seconds to report a refused loopback connection.
        with opener.open(f"http://127.0.0.1:{settings['port']}/health", timeout=3) as r:
            return json.load(r)
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return None
        raise


def healthy(settings):
    try:
        result = health(settings)
        return result is not None and result.get('installation') == settings['installation']
    except Exception:
        return False


def start_bridge(root, settings):
    if not healthy(settings):
        ensure_defaults(root, settings.get('codex_command','codex'))
        pythonw = Path(sys.executable).with_name('pythonw.exe')
        subprocess.Popen([str(pythonw), str(root/'proxy.py')], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
        for _ in range(50):
            if healthy(settings): break
            time.sleep(0.1)
        else:
            raise RuntimeError('Local adapter did not start; run check.cmd for diagnostics.')


def stop_bridge(settings):
    try:
        current = health(settings)
    except Exception:
        raise RuntimeError('Cannot verify the local service; restart was not attempted.') from None
    if current is not None:
        if current.get('installation') != settings['installation']:
            raise RuntimeError('Local service identity mismatch; refusing to stop it.')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f"http://127.0.0.1:{settings['port']}/shutdown",
                                         data=b'', headers={'X-Compat-Control': settings['control']})
        with opener.open(request, timeout=3) as response:
            if json.load(response).get('stopping') is not True:
                raise RuntimeError('Local adapter did not acknowledge shutdown.')
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(('127.0.0.1', settings['port']), timeout=3):
                    pass
            except ConnectionRefusedError:
                break
            except TimeoutError:
                pass
            except OSError:
                raise RuntimeError('Cannot verify that the local adapter stopped.') from None
            if time.monotonic() >= deadline:
                raise RuntimeError('Local adapter did not stop in time; restart was not attempted.')
            time.sleep(0.1)


def restart_bridge(root, settings):
    stop_bridge(settings)
    start_bridge(root, settings)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--hostname', help='Override the original credential helper hostname for this invocation.')
    mode.add_argument('--restart', action='store_true', help='Restart the local bridge without requesting credentials.')
    options = parser.parse_args(argv)
    settings = json.loads((ROOT/'settings.json').read_text(encoding='utf-8'))
    if options.restart:
        restart_bridge(ROOT, settings)
        print('Local bridge restarted.', file=sys.stderr)
        return
    args = auth_args(settings['auth_args'], options.hostname)
    current_upstream = settings_upstream(settings)
    if select_upstream(args, current_upstream) != current_upstream:
        raise RuntimeError('This hostname requires a different Copilot API. Use update.cmd --hostname HOST to switch credentials and endpoint together.')
    start_bridge(ROOT, settings)
    result = subprocess.run([settings['auth_command'], *args],
                            capture_output=True, timeout=settings['auth_timeout_ms']/1000)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError('Original credential helper failed; renew its login on this computer.')
    sys.stdout.buffer.write(result.stdout)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No credential-helper output or request headers are included in diagnostics.
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print(f'Copilot compatibility: {detail}', file=sys.stderr)
        sys.exit(1)
