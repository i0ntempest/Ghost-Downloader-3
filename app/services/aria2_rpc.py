from __future__ import annotations

import asyncio
import json
import socket
from functools import partial
from pathlib import Path
from secrets import token_hex
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal
from loguru import logger

from app.config.cfg import cfg
from app.config.constants import VERSION

if TYPE_CHECKING:
    from app.models.task import Task

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601


class Aria2RpcServer(QObject):
    taskDraftRequested = Signal(list)

    def __init__(self, coroutineRunner, parse, addTask, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._coroutineRunner = coroutineRunner
        self._parse = parse
        self._addTask = addTask
        self._serveWorkId: str | None = None

    def start(self) -> None:
        if self._serveWorkId is not None:
            return
        port = cfg.aria2RpcPort.value
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
            sock.listen()
        except OSError as e:
            logger.error("Aria2 RPC compat server failed to start on port {}: {}", port, e)
            sock.close()
            return
        sock.setblocking(False)
        self._serveWorkId = self._coroutineRunner.submit(self._run(sock))
        logger.info("Aria2 RPC compat server started on port {}", port)

    def stop(self) -> None:
        if self._serveWorkId is None:
            return
        self._coroutineRunner.cancel(self._serveWorkId)
        self._serveWorkId = None

    def setEnabled(self, enabled: bool) -> None:
        if enabled:
            self.start()
        else:
            self.stop()

    async def _run(self, sock: socket.socket) -> None:
        server = await asyncio.start_server(self._onConnection, sock=sock)
        async with server:
            await server.serve_forever()

    async def _onConnection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            contentLength = 0
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    contentLength = int(line.split(b":", 1)[1].strip())
                    break
            body = await reader.readexactly(contentLength)
            self._dispatchRpc(writer, body)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    def _dispatchRpc(self, writer: asyncio.StreamWriter, body: bytes) -> None:
        try:
            data = json.loads(body)
        except Exception:
            self._respondError(writer, None, JSONRPC_PARSE_ERROR, "Parse error")
            return

        if not isinstance(data, dict):
            self._respondError(writer, None, JSONRPC_INVALID_REQUEST, "Invalid Request")
            return

        rpcId = data.get("id")
        method = data.get("method", "")
        params = data.get("params", [])

        if not isinstance(params, list):
            self._respondError(writer, rpcId, JSONRPC_INVALID_REQUEST, "params must be array")
            return

        token = cfg.aria2RpcToken.value
        if token:
            if params and isinstance(params[0], str) and params[0].startswith("token:"):
                if params[0] != f"token:{token}":
                    self._respondError(writer, rpcId, 1, "Unauthorized")
                    return
                params = params[1:]
            else:
                self._respondError(writer, rpcId, 1, "Unauthorized")
                return
        elif params and isinstance(params[0], str) and params[0].startswith("token:"):
            params = params[1:]

        if method == "aria2.addUri":
            self._addUri(writer, rpcId, params)
        elif method == "aria2.getVersion":
            self._respond(writer, rpcId, {"version": VERSION, "enabledFeatures": ["HTTPS"]})
        else:
            self._respondError(writer, rpcId, JSONRPC_METHOD_NOT_FOUND, "Method not found")

    def _addUri(self, writer: asyncio.StreamWriter, rpcId: Any, params: list) -> None:
        uris = params[0] if params and isinstance(params[0], list) else []
        options = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}

        if not uris:
            self._respondError(writer, rpcId, 1, "No URI provided")
            return

        url = uris[0]
        filename = options.get("out", "")
        directory = options.get("dir", "")
        rawHeaders = options.get("header", [])

        headers: dict[str, str] = {}
        if isinstance(rawHeaders, str):
            rawHeaders = [rawHeaders]
        if isinstance(rawHeaders, list):
            for h in rawHeaders:
                if isinstance(h, str) and ":" in h:
                    k, v = h.split(":", 1)
                    headers[k.strip()] = v.strip()

        ua = options.get("user-agent", "")
        if isinstance(ua, str) and ua:
            headers.setdefault("User-Agent", ua)
        referer = options.get("referer", "")
        if isinstance(referer, str) and referer:
            headers.setdefault("Referer", referer)

        gid = token_hex(8)
        self._respond(writer, rpcId, gid)

        from app.models.task import TaskOptions

        outputFolder = Path(directory) if directory else Path(cfg.downloadFolder.value)
        clientProfile = "" if cfg.aria2RpcEmulateFingerprint.value else "raw"
        taskOptions = TaskOptions(
            url=url,
            headers=headers,
            outputFolder=outputFolder,
            clientProfile=clientProfile,
        )
        self._coroutineRunner.submit(
            self._parse(taskOptions),
            done=partial(self._onTaskParsed, filename=filename),
            failed=self._onTaskParseFailed,
        )

    def _onTaskParsed(self, task: Task, filename: str = "") -> None:
        if filename:
            task.setName(filename)

        if cfg.shouldDraftTakenDownload.value:
            self.taskDraftRequested.emit([task])
            return

        self._addTask(task)

    def _onTaskParseFailed(self, error: str) -> None:
        logger.warning("Aria2 RPC task parse failed: {}", error)

    def _respond(self, writer: asyncio.StreamWriter, rpcId: Any, result: Any) -> None:
        self._sendJson(writer, {"jsonrpc": "2.0", "id": rpcId, "result": result})

    def _respondError(self, writer: asyncio.StreamWriter, rpcId: Any, code: int, message: str) -> None:
        self._sendJson(writer, {"jsonrpc": "2.0", "id": rpcId, "error": {"code": code, "message": message}})

    def _sendJson(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        writer.write(header + body)
