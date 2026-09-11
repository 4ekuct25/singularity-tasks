#!/usr/bin/env python3
"""Разведка MCP-сервера SingularityApp — только чтение.

Зачем: решить, нужен ли MCP как альтернатива sing.py (карточка
T-c3070463). Скрипт ничего не меняет в трекере: шлёт только
initialize / tools/list и, по явному флагу, один read-only вызов.

Токен берётся там же, где его берёт sing.py: $SINGULARITY_TOKEN,
иначе macOS Keychain (-s singularity-app -a rest-token). В вывод
токен не попадает.

    python3 tools/mcp-probe.py tools
    python3 tools/mcp-probe.py tools --toolsets tasks,projects
    python3 tools/mcp-probe.py call <tool> '{"json":"args"}'
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

MCP_URL = "https://mcp.singularity-app.com/mcp"


def token() -> str:
    tok = os.environ.get("SINGULARITY_TOKEN")
    if tok:
        return tok.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "singularity-app",
             "-a", "rest-token", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    sys.exit("нет токена: ни $SINGULARITY_TOKEN, ни записи в Keychain")


def rpc(method: str, params=None, toolsets=None, session=None):
    """Один JSON-RPC вызов. Возвращает (http_code, headers, текст тела)."""
    url = MCP_URL + (f"?toolsets={toolsets}" if toolsets else "")
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
    )
    if session:
        req.add_header("Mcp-Session-Id", session)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, {}, f"URLError: {e.reason}"


def unwrap(text: str):
    """Тело приходит либо голым JSON, либо SSE-кадрами `data: {...}`."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def initialize(toolsets=None):
    code, headers, text = rpc(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "singularity-tasks-probe", "version": "0"},
        },
        toolsets=toolsets,
    )
    sid = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
    print(f"initialize: HTTP {code} session={sid!r}")
    data = unwrap(text)
    if data:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    else:
        print(text[:800])
    return code, sid


def main():
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    cmd = argv[0]
    toolsets = None
    if "--toolsets" in argv:
        toolsets = argv[argv.index("--toolsets") + 1]

    code, sid = initialize(toolsets)
    if code != 200:
        return 1

    if cmd == "tools":
        code, _, text = rpc("tools/list", {}, toolsets=toolsets, session=sid)
        print(f"\ntools/list: HTTP {code}")
        data = unwrap(text)
        if not data:
            print(text[:1000])
            return 1
        tools = (data.get("result") or {}).get("tools")
        if tools is None:
            print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
            return 1
        print(f"инструментов: {len(tools)}\n")
        for t in tools:
            props = ((t.get("inputSchema") or {}).get("properties") or {}).keys()
            print(f"- {t['name']}: {(t.get('description') or '').strip()[:160]}")
            print(f"    args: {', '.join(props) or '—'}")
    elif cmd == "call":
        name = argv[1]
        args = json.loads(argv[2]) if len(argv) > 2 else {}
        code, _, text = rpc("tools/call", {"name": name, "arguments": args},
                            toolsets=toolsets, session=sid)
        print(f"\ntools/call {name}: HTTP {code}")
        data = unwrap(text)
        print(json.dumps(data, ensure_ascii=False, indent=2)[:4000]
              if data else text[:2000])
    else:
        sys.exit(f"неизвестная команда: {cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
