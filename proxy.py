"""Loopback-only Responses SSE item-ID compatibility adapter. No credential storage."""
import json
import hmac
import socket
import threading
import urllib.error
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from catalog import adapt, ensure_defaults
from upstream import settings_upstream

PORT = 43129
VERSION = 'copilot-item-id-compat-v1'
SETTINGS = {}


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Normalizer:
    def __init__(self):
        self.ids = {}

    def normalize(self, event):
        index = event.get('output_index')
        item = event.get('item')
        if isinstance(index, int):
            original = item.get('id') if isinstance(item, dict) else event.get('item_id')
            if original:
                canonical = self.ids.setdefault(index, original)
                if isinstance(item, dict) and 'id' in item:
                    item['id'] = canonical
                if 'item_id' in event:
                    event['item_id'] = canonical
        response = event.get('response')
        if isinstance(response, dict):
            for index, item in enumerate(response.get('output', [])):
                if item.get('id'):
                    item['id'] = self.ids.setdefault(index, item['id'])
        return event


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'adapter': VERSION, 'installation': SETTINGS.get('installation'), 'catalog_adapter': '1.1.0',
                                    'upstream_url': settings_upstream(SETTINGS)})
        if self.path.split('?')[0] != '/models':
            return self.reply(404, {'error': 'Unsupported path'})
        self.serve_catalog()

    def serve_catalog(self):
        if not self.headers.get('Authorization'):
            return self.reply(401, {'error': 'Upstream authorization required'})
        try:
            saved=ensure_defaults(Path(__file__).resolve().parent, SETTINGS.get('codex_command','codex'))
            requested=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('client_version',[None])[0]
            if requested and requested != saved['client_version']:
                return self.reply(409, {'error':'Catalog schema version mismatch; refresh defaults using the matching desktop Codex binary.',
                                        'requested':requested,'available':saved['client_version']})
            headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','content-length','accept-encoding')}
            headers['Accept-Encoding']='identity'
            request=urllib.request.Request(settings_upstream(SETTINGS)+'/models',headers=headers)
            with urllib.request.urlopen(request,timeout=30) as upstream:
                data=json.load(upstream)
            result=adapt(data,saved['catalog'])
            return self.reply(200,result)
        except urllib.error.HTTPError as exc:
            return self.reply(exc.code, {'error':'Copilot model catalog request failed','status':exc.code,
                                         'upstream_url':settings_upstream(SETTINGS)})
        except Exception as exc:
            message=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
            return self.reply(502,{'error':'Catalog adaptation failed','detail':message})

    def do_POST(self):
        if self.path == '/shutdown':
            if not hmac.compare_digest(self.headers.get('X-Compat-Control', ''), SETTINGS.get('control', 'disabled')):
                return self.reply(403, {'error': 'Forbidden'})
            self.reply(200, {'stopping': True})
            self.close_connection = True
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path.split('?')[0] not in ('/responses', '/responses/compact'):
            return self.reply(404, {'error': 'Unsupported path'})
        if self.headers.get('Transfer-Encoding'):
            return self.reply(411, {'error': 'Content-Length required'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.reply(400, {'error': 'Invalid Content-Length'})
        if not 0 <= length <= 128 * 1024 * 1024:
            return self.reply(413, {'error': 'Request too large'})
        self.forward(self.rfile.read(length))

    def forward(self, body):
        if not self.headers.get('Authorization'):
            return self.reply(401, {'error': 'Upstream authorization required'})
        excluded = {'host', 'connection', 'content-length', 'accept-encoding', 'transfer-encoding', 'proxy-authorization', 'proxy-connection'}
        headers = {k:v for k,v in self.headers.items() if k.lower() not in excluded}
        headers['Accept-Encoding'] = 'identity'
        req = urllib.request.Request(settings_upstream(SETTINGS) + self.path, data=body, headers=headers, method=self.command)
        try:
            response = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            response = e
        except Exception:
            return self.reply(502, {'error': 'Could not connect to configured Copilot upstream'})
        with response:
            self.send_response(response.status)
            for k,v in response.headers.items():
                if k.lower() not in ('content-length', 'connection', 'transfer-encoding', 'content-encoding'):
                    self.send_header(k,v)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                if 'text/event-stream' in response.headers.get('Content-Type',''):
                    normalizer = Normalizer()
                    frame = []
                    for line in response:
                        if line.strip():
                            frame.append(line)
                        else:
                            self.emit_frame(frame, normalizer)
                            frame = []
                    if frame:
                        self.emit_frame(frame, normalizer)
                else:
                    while chunk := response.read(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def emit_frame(self, frame, normalizer):
        data = b'\n'.join(line[5:].lstrip().rstrip(b'\r\n') for line in frame if line.startswith(b'data:'))
        if data and data != b'[DONE]':
            try:
                event = normalizer.normalize(json.loads(data))
            except (ValueError, TypeError, AttributeError):
                pass
            else:
                frame = [line for line in frame if not line.startswith(b'data:')]
                frame.append(b'data: ' + json.dumps(event,ensure_ascii=False,separators=(',',':')).encode() + b'\n')
        self.wfile.write(b''.join(frame) + b'\n')
        self.wfile.flush()


if __name__ == '__main__':
    SETTINGS = json.loads(Path(__file__).with_name('settings.json').read_text(encoding='utf-8'))
    PORT = SETTINGS['port']
    settings_upstream(SETTINGS)
    with LocalServer(('127.0.0.1', PORT), Handler) as server:
        server.serve_forever()
