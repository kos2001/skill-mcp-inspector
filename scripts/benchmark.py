#!/usr/bin/env python3
"""Compare sequential and concurrent probes using local synthetic MCP servers."""
import argparse
import json
import math
import statistics
import sys
import time

from audit import McpServer, run_probes

SERVER = '''
import json, sys, time
initialized = False
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "notifications/initialized":
        initialized = True
        continue
    if "id" not in request:
        continue
    if method == "initialize":
        time.sleep(float(sys.argv[1]))
        result = {"protocolVersion": "2025-11-25", "serverInfo": {"name": "benchmark"}, "capabilities": {"tools": {}}}
    elif method == "tools/list":
        assert initialized
        result = {"tools": [{"name": "benchmark_tool"}]}
    else:
        raise RuntimeError("unexpected method")
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--servers', type=int, default=8)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--delay', type=float, default=0.15)
    args = parser.parse_args()
    if not (1 <= args.servers <= 100 and 1 <= args.workers <= 32 and 1 <= args.repeats <= 20):
        parser.error('servers: 1-100, workers: 1-32, repeats: 1-20')
    if not math.isfinite(args.delay) or not 0 <= args.delay <= 5:
        parser.error('delay must be finite and between 0 and 5 seconds')
    samples = {1: [], args.workers: []}
    for _ in range(args.repeats):
        for workers in samples:
            servers = [McpServer(name=f'fixture-{i}', source='synthetic', scope='project',
                                 transport='stdio', command=sys.executable,
                                 args=['-c', SERVER, str(args.delay)]) for i in range(args.servers)]
            start = time.monotonic()
            run_probes(servers, args.delay + 3, workers)
            samples[workers].append((time.monotonic() - start) * 1000)
            if any(s.health.get('status') != 'ok' or s.health.get('tool_count') != 1 for s in servers):
                raise RuntimeError('benchmark probe failed')
    sequential = statistics.median(samples[1])
    concurrent = statistics.median(samples[args.workers])
    print(json.dumps({
        'python': sys.version.split()[0], 'servers': args.servers, 'workers': args.workers,
        'repeats': args.repeats, 'server_delay_seconds': args.delay,
        'sequential_ms': round(sequential, 1), 'parallel_ms': round(concurrent, 1),
        'speedup': round(sequential / concurrent, 2),
        'samples_ms': {str(k): [round(v, 1) for v in values] for k, values in samples.items()},
        'note': 'Local synthetic latency benchmark; real-server speedup depends on workload.'
    }, indent=2))


if __name__ == '__main__':
    main()
