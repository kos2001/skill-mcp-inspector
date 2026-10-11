"""Local fake MCP server; never contacts external services."""
import json
import os
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else 'ok'
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if mode == 'silent':
        time.sleep(60)
    if method == 'notifications/initialized':
        initialized = True
        continue
    if 'id' not in request:
        continue
    reply = {'jsonrpc': '2.0', 'id': request['id']}
    if method == 'initialize':
        if mode == 'init_error':
            reply['error'] = {'code': -32602, 'message': 'denied'}
        else:
            if mode == 'noisy':
                sys.stderr.write('x' * 200000 + '\n' + os.environ.get('TEST_API_TOKEN', '') + '\n')
                sys.stderr.flush()
            time.sleep(0.15 if mode == 'slow' else 0)
            reply['result'] = {
                'protocolVersion': '2099-01-01' if mode == 'version' else '2025-11-25',
                'serverInfo': {'name': 'fixture', 'version': '1'},
                'capabilities': {} if mode == 'resources' else {'tools': {}},
            }
    elif method == 'server/discover':
        assert request['params']['_meta']['io.modelcontextprotocol/protocolVersion'] == '2026-07-28'
        reply['result'] = {'resultType': 'complete', 'supportedVersions': ['2026-07-28'], 'capabilities': {'tools': {}}}
    elif method == 'tools/list':
        assert initialized or mode == 'modern'
        if mode == 'tools_error':
            reply['error'] = {'code': -32603, 'message': 'failed'}
        elif mode == 'invalid_tools':
            reply['result'] = {'tools': ['invalid']}
        else:
            cursor = request.get('params', {}).get('cursor')
            reply['result'] = {'tools': [{'name': 'second' if cursor else 'first'}]}
            if not cursor or mode == 'cursor_loop':
                reply['result']['nextCursor'] = 'page2'
    else:
        reply['error'] = {'code': -32601, 'message': 'unknown'}
    if mode == 'buffered' and method == 'initialize':
        # Both messages share one OS read; the client's pending buffer must survive.
        reply2 = {'jsonrpc': '2.0', 'id': 2, 'result': {'tools': [{'name': 'buffered'}]}}
        sys.stdout.write(json.dumps(reply) + '\n' + json.dumps(reply2) + '\n')
        sys.stdout.flush()
        continue
    if mode == 'invalid_json':
        print('not JSON', flush=True)
    else:
        print(json.dumps(reply), flush=True)
