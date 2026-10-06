"""Token-metering, vendor-locking reverse proxy plus an allowlisted CONNECT proxy.

  :8080  /anthropic/<path>  -> https://api.anthropic.com/<path>
         /openai/<path>     -> https://api.openai.com/<path>
         /budget            -> {"name", "used", "max_tokens", "remaining"} for the caller's key
  :3128  HTTP CONNECT tunnel, only to hosts in ALLOW_CONNECT_HOSTS (package mirrors)

Callers authenticate with a virtual key (x-api-key or Bearer) from KEYS_FILE:
  {"keys": {"<virtual-key>": {"name": "claude-box", "vendors": ["anthropic"], "max_tokens": 2000000}}}
The file is re-read whenever it changes. Real vendor keys never leave this process.

A token is counted for every input token the model processed (fresh, cache write
and cache read) plus every output token (including reasoning/thinking). With
BUDGET_MODE=uncached, cache reads are excluded from the budget (still logged).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

UPSTREAM = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}
REAL_KEYS = {"anthropic": os.environ.get("ANTHROPIC_API_KEY", ""), "openai": os.environ.get("OPENAI_API_KEY", "")}
KEYS_FILE = Path(os.environ.get("KEYS_FILE", "/config/keys.json"))
USAGE_DIR = Path(os.environ.get("USAGE_DIR", "/usage"))
BUDGET_MODE = os.environ.get("BUDGET_MODE", "all")
ALLOW_CONNECT = [h.strip() for h in os.environ.get(
    "ALLOW_CONNECT_HOSTS",
    "pypi.org,files.pythonhosted.org,registry.npmjs.org,proxy.golang.org,sum.golang.org,"
    "index.crates.io,static.crates.io,crates.io,repo.maven.apache.org,repo1.maven.org,plugins.gradle.org",
).split(",") if h.strip()]
METERED = ("/v1/messages", "/v1/responses", "/v1/chat/completions", "/v1/completions")
HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "accept-encoding",
               "x-api-key", "authorization", "keep-alive", "upgrade"}

_keys: dict = {}
_keys_mtime = 0.0
_used: dict[str, int] = {}


def load_keys() -> dict:
    global _keys, _keys_mtime
    try:
        m = KEYS_FILE.stat().st_mtime
    except FileNotFoundError:
        return _keys
    if m != _keys_mtime:
        _keys = json.loads(KEYS_FILE.read_text()).get("keys", {})
        _keys_mtime = m
    return _keys


def load_usage() -> None:
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    for f in USAGE_DIR.glob("*.jsonl"):
        total = 0
        for line in f.read_text().splitlines():
            try:
                total += json.loads(line).get("billed", 0)
            except json.JSONDecodeError:
                pass
        _used[f.stem] = total


def caller_key(req: web.Request) -> str:
    if k := req.headers.get("x-api-key"):
        return k
    auth = req.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


def err(status: int, kind: str, msg: str) -> web.Response:
    # Shape works for both Anthropic and OpenAI SDK error parsers.
    body = {"type": "error", "error": {"type": kind, "code": kind, "message": msg}}
    return web.json_response(body, status=status)


def extract_usage(obj) -> dict | None:
    """Pull usage out of any Anthropic / OpenAI response or stream event."""
    if not isinstance(obj, dict):
        return None
    u = None
    if isinstance(obj.get("usage"), dict):
        u = obj["usage"]
    elif isinstance(obj.get("message"), dict) and isinstance(obj["message"].get("usage"), dict):
        u = obj["message"]["usage"]
    elif isinstance(obj.get("response"), dict) and isinstance(obj["response"].get("usage"), dict):
        u = obj["response"]["usage"]
    if not u:
        return None
    out: dict = {}
    if "prompt_tokens" in u or "completion_tokens" in u:  # chat completions
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
        out = {"fresh": (u.get("prompt_tokens") or 0) - cached, "cache_read": cached, "cache_write": 0,
               "output": u.get("completion_tokens") or 0}
    elif "input_tokens_details" in u or "output_tokens_details" in u:  # responses API
        cached = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
        out = {"fresh": (u.get("input_tokens") or 0) - cached, "cache_read": cached, "cache_write": 0,
               "output": u.get("output_tokens") or 0}
    else:  # anthropic: fields may be partial (message_delta carries only some)
        for src, dst in (("input_tokens", "fresh"), ("cache_read_input_tokens", "cache_read"),
                         ("cache_creation_input_tokens", "cache_write"), ("output_tokens", "output")):
            if u.get(src) is not None:
                out[dst] = u[src]
    return out


def billed(u: dict) -> int:
    total = u.get("fresh", 0) + u.get("cache_write", 0) + u.get("output", 0)
    if BUDGET_MODE != "uncached":
        total += u.get("cache_read", 0)
    return total


def record(name: str, vendor: str, path: str, model: str, status: int, u: dict, dur: float) -> None:
    b = billed(u)
    _used[name] = _used.get(name, 0) + b
    row = {"ts": round(time.time(), 3), "vendor": vendor, "path": path, "model": model, "status": status,
           **{k: u.get(k, 0) for k in ("fresh", "cache_write", "cache_read", "output")},
           "billed": b, "cum": _used[name], "dur": round(dur, 2)}
    with (USAGE_DIR / f"{name}.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")


async def budget(req: web.Request) -> web.Response:
    info = load_keys().get(caller_key(req))
    if not info:
        return err(401, "authentication_error", "unknown virtual key")
    used = _used.get(info["name"], 0)
    mx = info.get("max_tokens")
    return web.json_response({"name": info["name"], "used": used, "max_tokens": mx,
                              "remaining": None if mx is None else max(0, mx - used), "mode": BUDGET_MODE})


async def forward(req: web.Request) -> web.StreamResponse:
    vendor, path = req.match_info["vendor"], "/" + req.match_info["path"]
    info = load_keys().get(caller_key(req))
    if not info:
        return err(401, "authentication_error", "unknown virtual key")
    if vendor not in info.get("vendors", []):
        return err(403, "permission_error", f"key '{info['name']}' may not call {vendor}")
    if req.headers.get("upgrade", "").lower() == "websocket":
        return err(426, "invalid_request_error", "websockets not supported; use HTTP streaming")
    name = info["name"]
    metered = path.startswith(METERED) and not path.endswith("count_tokens") and req.method == "POST"
    mx = info.get("max_tokens")
    if metered and mx is not None and _used.get(name, 0) >= mx:
        return err(400, "budget_exhausted",
                   f"budget_exhausted: token budget for '{name}' is used up ({_used[name]}/{mx}). Stop now.")

    body = await req.read()
    model = ""
    if metered and body:
        try:
            j = json.loads(body)
            model = j.get("model", "")
            if path.startswith("/v1/chat/completions") and j.get("stream"):
                j.setdefault("stream_options", {})["include_usage"] = True
                body = json.dumps(j).encode()
        except (json.JSONDecodeError, AttributeError):
            pass

    headers = {k: v for k, v in req.headers.items() if k.lower() not in HOP_HEADERS}
    if vendor == "anthropic":
        headers["x-api-key"] = REAL_KEYS["anthropic"]
    else:
        headers["authorization"] = f"Bearer {REAL_KEYS['openai']}"
    url = UPSTREAM[vendor] + path + (("?" + req.query_string) if req.query_string else "")

    t0 = time.time()
    session: aiohttp.ClientSession = req.app["session"]
    usage: dict = {}
    status = 0
    try:
        async with session.request(req.method, url, headers=headers, data=body) as up:
            status = up.status
            return await relay(req, up, metered, usage)
    finally:
        # Bill even if the client hung up early (Codex closes right after response.completed).
        if metered:
            record(name, vendor, path, model, status, usage, time.time() - t0)


async def relay(req: web.Request, up: aiohttp.ClientResponse, metered: bool, usage: dict) -> web.StreamResponse:
    """Stream the upstream response to the client, collecting usage into `usage`.
    Keeps reading upstream after a client disconnect so the usage event is still seen."""
    resp = web.StreamResponse(status=up.status)
    for k, v in up.headers.items():
        if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection"):
            resp.headers[k] = v
    client_gone = False
    try:
        await resp.prepare(req)
    except (ConnectionError, aiohttp.ClientConnectionResetError):
        client_gone = True
    is_sse = "text/event-stream" in up.headers.get("content-type", "")
    buf, raw = b"", bytearray()
    async for chunk in up.content.iter_any():
        if not client_gone:
            try:
                await resp.write(chunk)
            except (ConnectionError, aiohttp.ClientConnectionResetError):
                client_gone = True
        if not metered:
            if client_gone:
                break
            continue
        if is_sse:
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                if line.startswith(b"data:"):
                    try:
                        u = extract_usage(json.loads(line[5:].strip()))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        u = None
                    # Anthropic reports usage in message_start and again in message_delta,
                    # where cache fields can be 0; every field is either once or cumulative.
                    if u:
                        for k, v in u.items():
                            usage[k] = max(usage.get(k, 0), v or 0)
        else:
            raw += chunk
    if metered and not is_sse and raw:
        try:
            usage.update(extract_usage(json.loads(raw)) or {})
        except json.JSONDecodeError:
            pass
    if not client_gone:
        try:
            await resp.write_eof()
        except (ConnectionError, aiohttp.ClientConnectionResetError):
            pass
    return resp


# ------------------------------------------------------------ CONNECT proxy

def host_allowed(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in ALLOW_CONNECT)


async def _pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        w.close()


async def handle_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
        method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        host, _, port = target.rpartition(":")
        if method != "CONNECT" or not host_allowed(host):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        ur, uw = await asyncio.open_connection(host, int(port or 443))
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, uw), _pipe(ur, writer))
    except Exception:
        writer.close()


async def main() -> None:
    load_usage()
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app["session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=900),
                                           auto_decompress=True)
    app.router.add_get("/budget", budget)
    app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
    app.router.add_route("*", "/{vendor:anthropic|openai}/{path:.*}", forward)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    await asyncio.start_server(handle_connect, "0.0.0.0", 3128)
    print(f"meter proxy up: :8080 api, :3128 connect, mode={BUDGET_MODE}, allow={ALLOW_CONNECT}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
