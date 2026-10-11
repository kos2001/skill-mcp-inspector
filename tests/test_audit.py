import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import audit
import probes

FIXTURE = ROOT / 'tests/fixtures/mcp_stdio.py'


def stdio(mode='ok'):
    return audit.McpServer(name=mode, source=str(FIXTURE), scope='project', transport='stdio',
                           command=sys.executable, args=[str(FIXTURE), mode])


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.home = self.root / '.claude'
        self.home.mkdir()
        self.global_config = self.root / '.claude.json'
        self.out = self.root / 'out'
        self.base_args = ['--claude-home', str(self.home), '--project-root', str(self.project), '--out', str(self.out)]

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def run_cli(self, extra=()):
        with contextlib.redirect_stdout(io.StringIO()):
            result = audit.main(self.base_args + list(extra))
        return result, json.loads((self.out / 'report.json').read_text())

    def test_scope_and_precedence(self):
        self.write(self.global_config, {'mcpServers': {'shared': {'command': 'user'}}, 'projects': {
            str(self.project): {'mcpServers': {'shared': {'command': 'local'}}},
            '/other': {'mcpServers': {'elsewhere': {'command': 'elsewhere'}}}}})
        self.write(self.project / '.mcp.json', {'mcpServers': {'shared': {'command': 'project'}}})
        _, report = self.run_cli()
        self.assertEqual([(s['scope'], s['command']) for s in report['mcp_servers'] if s['effective']], [('local', 'local')])
        _, report = self.run_cli(['--scope', 'project'])
        self.assertEqual({s['scope'] for s in report['mcp_servers']}, {'local', 'project'})
        _, report = self.run_cli(['--scope', 'user'])
        self.assertEqual([s['command'] for s in report['mcp_servers']], ['user'])

    def test_empty_scope_is_success(self):
        result, report = self.run_cli(['--scope', 'project', '--strict'])
        self.assertEqual(result, 0)
        self.assertFalse(report['diagnostics'])

    def test_settings_and_server_disabled(self):
        self.write(self.project / '.claude/settings.local.json', {'disabledMcpjsonServers': ['off']})
        self.write(self.project / '.mcp.json', {'mcpServers': {
            'off': {'command': 'ignored'}, 'explicit': {'command': 'ignored', 'disabled': True}}})
        _, report = self.run_cli(['--scope', 'project', '--probe'])
        self.assertEqual({s['health']['status'] for s in report['mcp_servers']}, {'disabled'})
        self.assertEqual(report['summary']['servers_probed'], 0)

    def test_invalid_configs_are_reported_without_crashing(self):
        self.write(self.project / '.mcp.json', {'mcpServers': {
            'bad_args': {'command': 'python', 'args': 'string'},
            'bad_env': {'command': 'python', 'env': ['broken']},
            'bad_port': {'type': 'http', 'url': 'http://localhost:bad'},
            'good': {'command': 'python', 'args': []}}})
        result, report = self.run_cli(['--scope', 'project', '--strict'])
        self.assertEqual(result, 1)
        self.assertEqual(len(report['diagnostics']), 3)
        self.assertEqual([s['name'] for s in report['mcp_servers'] if not s['validation_errors']], ['good'])
        self.assertEqual(len(report['mcp_servers']), 4)
        (self.project / '.mcp.json').write_text('{ invalid secret text')
        result, report = self.run_cli(['--scope', 'project', '--strict'])
        self.assertEqual(result, 1)
        self.assertIn('invalid JSON at line', report['diagnostics'][0]['message'])
        self.assertNotIn('secret text', json.dumps(report))

    def test_cached_plugin_is_not_probed(self):
        plugin = self.home / 'plugins/cache/market/demo/1.0'
        self.write(plugin / '.mcp.json', {'mcpServers': {'demo': {'command': 'ignored'}}})
        _, report = self.run_cli(['--probe'])
        self.assertFalse(report['mcp_servers'][0]['enabled'])
        self.write(self.home / 'plugins/installed_plugins.json', {'version': 2, 'plugins': {
            'demo@market': [{'scope': 'user', 'installPath': str(plugin)}]}})
        _, report = self.run_cli()
        self.assertTrue(report['mcp_servers'][0]['enabled'])
        self.write(self.home / 'settings.json', {'enabledPlugins': {'demo@market': False}})
        _, report = self.run_cli()
        self.assertFalse(report['mcp_servers'][0]['enabled'])

    def test_expansion_credentials_reach_child_and_are_redacted(self):
        secret = 'fixture-secret-123456'
        self.write(self.project / '.mcp.json', {'mcpServers': {'noisy': {
            'command': '${FIXTURE_PYTHON}', 'args': [str(FIXTURE), 'noisy'],
            'env': {'TEST_API_TOKEN': '${MISSING_TOKEN:-' + secret + '}'}}}})
        with patch.dict(os.environ, {'FIXTURE_PYTHON': sys.executable}):
            result, report = self.run_cli(['--scope', 'project', '--probe', '--fail-on-unhealthy'])
        self.assertEqual(result, 0)
        self.assertEqual(report['mcp_servers'][0]['health']['status'], 'ok')
        self.assertIn('[REDACTED]', report['mcp_servers'][0]['health']['stderr_tail'])
        self.assertNotIn(secret, (self.out / 'report.json').read_text() + (self.out / 'report.md').read_text())
        self.assertNotIn('_env', report['mcp_servers'][0])

    def test_skill_evidence_frontmatter_pruning_and_limits(self):
        path = self.project / '.claude/skills/demo/SKILL.md'
        path.parent.mkdir(parents=True)
        path.write_text('---\nname: "demo"\ndescription: >\n  multi line\n  description\n---\nOffline guide\n')
        script = path.parent / 'scripts/run.py'
        script.parent.mkdir()
        script.write_text('# test\nimport urllib.request\nurllib.request.urlopen(url)\n')
        ignored = path.parent / 'scripts/node_modules/ignored.py'
        ignored.parent.mkdir()
        ignored.write_text('WebSearch')
        _, report = self.run_cli(['--scope', 'project'])
        skill = report['skills'][0]
        self.assertEqual(skill['name'], 'demo')
        self.assertEqual(skill['description'], 'multi line description')
        self.assertEqual(skill['classification'], 'external')
        self.assertEqual(skill['evidence'], [{'path': str(script), 'line': 3, 'reason': 'uses urllib HTTP client'}])
        script.write_text('x' * (audit.MAX_SCAN_BYTES + 1))
        result, report = self.run_cli(['--scope', 'project', '--strict'])
        self.assertEqual(result, 1)
        self.assertEqual(report['skills'][0]['classification'], 'unknown')

    def test_baseline_and_ci(self):
        self.write(self.project / '.mcp.json', {'mcpServers': {'one': {'command': 'missing-fixture-command'}}})
        _, before = self.run_cli(['--scope', 'project'])
        baseline = self.root / 'baseline.json'
        baseline.write_text(json.dumps(before))
        self.write(self.project / '.mcp.json', {'mcpServers': {'two': {'command': 'missing-fixture-command'}}})
        result, report = self.run_cli(['--scope', 'project', '--baseline', str(baseline), '--probe', '--fail-on-unhealthy'])
        self.assertEqual(result, 1)
        self.assertEqual(report['changes']['mcp_servers']['added'][0]['name'], 'two')
        self.assertEqual(report['changes']['mcp_servers']['removed'][0]['name'], 'one')
        self.assertIn('Changes from baseline', (self.out / 'report.md').read_text())

    def test_filters_and_invalid_flags(self):
        for flags in (['--workers', '0'], ['--probe-timeout', 'nan'], ['--probe-timeout', '-1'],
                      ['--fail-on-unhealthy'], ['--server', 'absent']):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                audit.main(self.base_args + flags)
            self.assertEqual(caught.exception.code, 2)

    def test_explicit_config_filter_and_modern_cli(self):
        config = self.root / 'portable.json'
        self.write(config, {'mcpServers': {
            'selected': {'command': sys.executable, 'args': [str(FIXTURE), 'modern']},
            'other': {'command': 'missing-fixture-command'}}})
        result, report = self.run_cli(['--scope', 'project', '--config', str(config),
                                       '--server', 'selected', '--probe', '--protocol-era', 'modern',
                                       '--fail-on-unhealthy'])
        self.assertEqual(result, 0)
        self.assertEqual([s['name'] for s in report['mcp_servers']], ['selected'])
        self.assertEqual(report['mcp_servers'][0]['health']['protocol_version'], '2026-07-28')

    def test_plugin_placeholders_and_cwd(self):
        plugin = self.home / 'plugins/cache/market/demo/1.0'
        plugin.mkdir(parents=True)
        (plugin / 'server.py').write_text(FIXTURE.read_text())
        self.write(plugin / '.mcp.json', {'mcpServers': {'demo': {
            'command': sys.executable, 'args': ['${CLAUDE_PLUGIN_ROOT}/server.py', 'ok']}}})
        self.write(self.home / 'plugins/installed_plugins.json', {'plugins': {
            'demo@market': [{'scope': 'user', 'installPath': str(plugin)}]}})
        result, report = self.run_cli(['--probe', '--fail-on-unhealthy'])
        self.assertEqual(result, 0)
        self.assertEqual(report['mcp_servers'][0]['health']['tool_count'], 2)

    def test_offline_inventory_does_not_probe(self):
        self.write(self.project / '.mcp.json', {'mcpServers': {'remote': {
            'type': 'http', 'url': 'https://example.invalid/mcp'}}})
        with patch.object(audit, 'run_probes') as runner:
            result, report = self.run_cli(['--scope', 'project'])
            runner.assert_not_called()
        self.assertEqual(result, 0)
        self.assertFalse(report['mcp_servers'][0]['health'])


class ClassificationTests(unittest.TestCase):
    def test_deterministic_token_hints_and_unknown_default(self):
        server = stdio()
        server.args = ['example-driver']  # neither exa nor drive substring should match.
        audit.classify_server(server)
        self.assertEqual(server.classification, 'unknown')
        server.args = ['@modelcontextprotocol/server-github', 'filesystem']
        audit.classify_server(server)
        self.assertEqual(server.classification, 'external')
        server.args = ['server-filesystem']
        audit.classify_server(server)
        self.assertEqual(server.classification, 'local')
        server.env_keys = ['DATABASE_PASSWORD']
        audit.classify_server(server)
        self.assertEqual(server.classification, 'unknown')

    def test_loopback_and_remote(self):
        for url, expected in [('http://127.0.0.1/mcp', 'local'), ('http://[::1]/mcp', 'local'),
                              ('https://localhost/mcp', 'local'), ('https://example.org/mcp', 'external')]:
            server = audit.McpServer('remote', 'test', 'user', 'http', url=url)
            audit.classify_server(server)
            self.assertEqual(server.classification, expected)

    def test_report_redaction_and_markdown_escaping(self):
        secret = 'query-secret-111'
        server = audit.McpServer('a|b`<script>', 'source', 'project', 'http',
                                 url='https://user:pw-secret@example.org/mcp?key=' + secret,
                                 args=['--token', 'arg-secret', '--password=inline-secret'])
        server.health = {'error': f'{secret} arg-secret inline-secret pw-secret'}
        audit.classify_server(server)
        with tempfile.TemporaryDirectory() as temp:
            audit.write_reports(Path(temp), [server], [], {
                'generated_at': 'test', 'scope': 'project', 'probe': False,
                'claude_home': 'fixture', 'project_root': 'fixture'})
            rendered = (Path(temp) / 'report.md').read_text()
            all_text = rendered + (Path(temp) / 'report.json').read_text()
        for value in (secret, 'pw-secret', 'arg-secret', 'inline-secret'):
            self.assertNotIn(value, all_text)
        self.assertNotIn('<script>', rendered)
        self.assertIn('a&#124;b&#96;&lt;script&gt;', rendered)

    def test_baseline_ignores_timing_and_detects_changes(self):
        server = {'source': 'fixture', 'scope': 'project', 'name': 'demo', 'enabled': True,
                  'classification': 'local', 'health': {'status': 'ok', 'latency_ms': 1}}
        previous = {'mcp_servers': [server], 'skills': []}
        current = copy.deepcopy(previous)
        current['mcp_servers'][0]['health']['latency_ms'] = 999
        self.assertFalse(audit.compare_reports(previous, current)['mcp_servers']['changed'])
        current['mcp_servers'][0]['classification'] = 'external'
        current['mcp_servers'][0]['health']['status'] = 'partial'
        self.assertEqual(audit.compare_reports(previous, current)['mcp_servers']['changed'][0]['fields'],
                         ['classification', 'health.status'])

    def test_short_secrets_do_not_corrupt_health_enums(self):
        self.assertEqual(audit.sanitize({'status': 'ok', 'classification': 'local', 'error': 'token: ok'}, {'ok', 'local'}),
                         {'status': 'ok', 'classification': 'local', 'error': 'token: [REDACTED]'})


class StdioTests(unittest.TestCase):
    def test_protocol_outcomes(self):
        for mode, status in [('ok', 'ok'), ('init_error', 'error'), ('tools_error', 'partial'),
                             ('invalid_tools', 'partial'), ('invalid_json', 'error'), ('version', 'error'),
                             ('resources', 'ok'), ('cursor_loop', 'partial'), ('buffered', 'ok')]:
            with self.subTest(mode=mode):
                result = probes.probe_stdio(stdio(mode), 2)
                self.assertEqual(result['status'], status, result)
                if mode == 'ok':
                    self.assertEqual(result['tool_count'], 2)
                    self.assertEqual(result['tool_names'], ['first', 'second'])

    def test_timeout_does_not_hang(self):
        start = time.monotonic()
        result = probes.probe_stdio(stdio('silent'), 0.15)
        self.assertEqual(result['status'], 'timeout')
        self.assertLess(time.monotonic() - start, 1.5)

    def test_subprocesses_and_pipes_are_closed(self):
        original = subprocess.Popen
        processes = []
        def record(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(probes.subprocess, 'Popen', side_effect=record):
            probes.probe_stdio(stdio('ok'), 1)
            probes.probe_stdio(stdio('silent'), 0.05)
        for process in processes:
            self.assertIsNotNone(process.poll())
            self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))

    def test_modern_discovery(self):
        server = stdio('modern')
        server._protocol_era = 'modern'
        result = probes.probe_stdio(server, 2)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['protocol_version'], '2026-07-28')
        self.assertEqual(result['tool_count'], 2)

    def test_parallel_probes_preserve_results(self):
        servers = [stdio('slow') for _ in range(8)]
        start = time.monotonic()
        audit.run_probes(servers, 2, 4)
        self.assertTrue(all(s.health['status'] == 'ok' for s in servers))
        self.assertLess(time.monotonic() - start, 2)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def respond(self, status, data=b'', content_type='application/json', extra=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.respond(200, b'', 'text/event-stream')

    def do_DELETE(self):
        self.server.deleted = self.headers.get('MCP-Session-Id')
        self.respond(200)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        path = self.path
        if path in ('/401', '/403', '/404', '/405', '/500'):
            self.respond(int(path[1:]))
            return
        if path == '/redirect':
            self.respond(307, extra={'Location': '/mcp'})
            return
        if path == '/html':
            self.respond(200, b'<html>not MCP</html>', 'text/html')
            return
        if path == '/slow':
            time.sleep(0.3)
        if path == '/headers' and self.headers.get('Authorization') != 'Bearer fixture-header-secret':
            self.respond(401)
            return
        method = request['method']
        reply = {'jsonrpc': '2.0', 'id': request.get('id')}
        extra = {}
        if method == 'initialize':
            reply['result'] = {'protocolVersion': '2025-11-25', 'serverInfo': {'name': 'http'}, 'capabilities': {'tools': {}}}
            extra['MCP-Session-Id'] = 'fixture-session'
        elif method == 'server/discover':
            assert self.headers.get('Mcp-Method') == method
            assert self.headers.get('MCP-Protocol-Version') == '2026-07-28'
            assert request['params']['_meta']['io.modelcontextprotocol/clientCapabilities'] == {}
            reply['result'] = {'resultType': 'complete', 'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {}}}
        else:
            modern = path == '/modern'
            if not modern:
                assert self.headers.get('MCP-Session-Id') == 'fixture-session'
                assert self.headers.get('MCP-Protocol-Version') == '2025-11-25'
            if method == 'notifications/initialized':
                self.respond(202)
                return
            if path == '/rpc_error':
                reply['error'] = {'code': -32603, 'message': 'failed'}
            else:
                cursor = request['params'].get('cursor')
                reply['result'] = {'tools': [{'name': 'b' if cursor else 'a'}]}
                if not cursor:
                    reply['result']['nextCursor'] = 'next'
        if path == '/sse':
            data = b': heartbeat\r\n\r\n' + b'data: ' + json.dumps(reply).encode() + b'\r\n\r\n'
            self.respond(200, data, 'text/event-stream', extra)
        elif path == '/wrong_id':
            reply['id'] = 999
            self.respond(200, json.dumps(reply).encode(), extra=extra)
        else:
            self.respond(200, json.dumps(reply).encode(), extra=extra)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def remote(self, path='/mcp'):
        return audit.McpServer('http', 'fixture', 'project', 'http', url=self.url + path)

    def test_json_sse_auth_and_errors(self):
        for path, status in [('/mcp', 'ok'), ('/sse', 'ok'), ('/401', 'auth_required'),
                             ('/403', 'forbidden'), ('/404', 'error'), ('/405', 'error'), ('/500', 'error'),
                             ('/html', 'error'), ('/redirect', 'error'), ('/rpc_error', 'partial'), ('/wrong_id', 'error')]:
            with self.subTest(path=path):
                result = probes.probe_http(self.remote(path), 2)
                self.assertEqual(result['status'], status, result)
                if status == 'ok':
                    self.assertEqual(result['tool_count'], 2)
                    self.assertEqual(self.server.deleted, 'fixture-session')

    def test_headers_and_legacy_sse(self):
        server = self.remote('/headers')
        server._headers = {'Authorization': 'Bearer fixture-header-secret'}
        self.assertEqual(probes.probe_http(server, 2)['status'], 'ok')
        server.transport = 'sse'
        result = probes.probe_http(server, 2)
        self.assertEqual(result['status'], 'reachable')
        self.assertNotIn('tool_count', result)

    def test_http_timeout(self):
        result = probes.probe_http(self.remote('/slow'), 0.05)
        self.assertEqual(result['status'], 'timeout', result)

    def test_modern(self):
        server = self.remote('/modern')
        server._protocol_era = 'modern'
        result = probes.probe_http(server, 2)
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['protocol_version'], '2026-07-28')


if __name__ == '__main__':
    unittest.main()
