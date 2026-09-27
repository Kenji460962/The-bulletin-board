from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

CFG = {
    "max_players": 40,
    "max_rooms": 8,
    "max_per_room": 10,
    "max_msg_bytes": 256,
    "per_ip": 4,
    "state_min_interval": 0.05,
    "ctrl_per_sec": 10,
    "idle_timeout": 120.0,
    "room_name_len": 24,
    "player_name_len": 16,
    "allowed_origins": {
        "talk-ch.com",
        "www.talk-ch.com",
    },
}

MSG_STATE = 0x01
STATE_BODY_LEN = 32

router = APIRouter()

def origin_allowed(origin: str | None) -> bool:
    if not origin:
        return False
    try:
        host = (urlparse(origin).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if os.getenv("APP_ENV", "production").lower() == "production" and host == "localhost":
        return False
    return host in CFG["allowed_origins"]

@dataclass
class Player:
    pid: int
    ws: WebSocket
    ip: str
    room: str | None = None
    name: str = ""
    last_state_at: float = 0.0
    ctrl_window: deque[float] = field(default_factory=lambda: deque(maxlen=CFG["ctrl_per_sec"]))

class Hub:
    def __init__(self) -> None:
        self.players: dict[int, Player] = {}
        self.rooms: dict[str, set[int]] = {}
        self.ip_count: dict[str, int] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    def alloc_id(self) -> int:
        pid = self._next_id
        self._next_id = (self._next_id % 60000) + 1
        return pid

    async def can_accept(self, ip: str) -> bool:
        async with self._lock:
            return (
                len(self.players) < CFG["max_players"]
                and self.ip_count.get(ip, 0) < CFG["per_ip"]
            )

    async def register(self, p: Player) -> None:
        async with self._lock:
            self.players[p.pid] = p
            self.ip_count[p.ip] = self.ip_count.get(p.ip, 0) + 1

    async def unregister(self, p: Player) -> None:
        await self.leave_room(p)
        async with self._lock:
            self.players.pop(p.pid, None)
            n = self.ip_count.get(p.ip, 0) - 1
            if n <= 0:
                self.ip_count.pop(p.ip, None)
            else:
                self.ip_count[p.ip] = n

    async def leave_room(self, p: Player) -> None:
        room = p.room
        if not room:
            return
        p.room = None
        async with self._lock:
            members = self.rooms.get(room)
            if members is None:
                return
            members.discard(p.pid)
            if not members:
                del self.rooms[room]
                return
        await self.broadcast_json(room, {"t": "leave", "id": p.pid}, exclude=p.pid)

    async def broadcast_json(self, room: str, obj: dict[str, Any], exclude: int | None = None) -> None:
        async with self._lock:
            members = self.rooms.get(room) or ()
            targets = [self.players[i].ws for i in members if i != exclude and i in self.players]
            if not targets:
                return
            await asyncio.gather(
                *(ws.send_json(obj) for ws in targets), return_exceptions=True
            )

hub = Hub()

async def handle_state(p: Player, data: bytes) -> None:
    if not p.room or len(data) != STATE_BODY_LEN:
        return

    now = time.monotonic()
    if now - p.last_state_at < CFG["state_min_interval"]:
        return
    p.last_state_at = now

    try:
        px, py, pz, qx, qy, qz, qw, speed = struct.unpack("<ffffffff", data)
    except struct.error:
        return

    if not all(math.isfinite(v) for v in (px, py, pz, qx, qy, qz, qw, speed)):
        return

    packet = struct.pack("<BH", MSG_STATE, p.pid) + data
    members = hub.rooms.get(p.room)
    if not members:
        return

    peers = [hub.players[i].ws for i in members if i != p.pid and i in hub.players]
    if peers:
        await asyncio.gather(
            *(ws.send_bytes(packet) for ws in peers), return_exceptions=True
        )

async def handle_ctrl(p: Player, raw: str) -> None:
    now = time.monotonic()
    while p.ctrl_window and now - p.ctrl_window[0] > 1.0:
        p.ctrl_window.popleft()
    if len(p.ctrl_window) >= CFG["ctrl_per_sec"]:
        return
    p.ctrl_window.append(now)

    try:
        msg = json.loads(raw)
    except ValueError:
        return

    if not isinstance(msg, dict):
        return

    t = msg.get("t")

    if t == "join":
        room = str(msg.get("room", ""))[: CFG["room_name_len"]].strip()
        name = str(msg.get("name", ""))[: CFG["player_name_len"]].strip() or f"Pilot-{p.pid}"

        if not room:
            await p.ws.send_json({"t": "error", "code": "bad_room"})
            return

        async with hub._lock:
            members = hub.rooms.get(room)
            if members is None and len(hub.rooms) >= CFG["max_rooms"]:
                await p.ws.send_json({"t": "error", "code": "rooms_full"})
                return
            if members is not None and p.pid not in members and len(members) >= CFG["max_per_room"]:
                await p.ws.send_json({"t": "error", "code": "room_full"})
                return

            if p.room and p.room != room:
                old_room = p.room
                p.room = None
                old_members = hub.rooms.get(old_room)
                if old_members is not None:
                    old_members.discard(p.pid)
                    if not old_members:
                        del hub.rooms[old_room]

            p.room, p.name = room, name
            hub.rooms.setdefault(room, set()).add(p.pid)

            roster = [
                {"id": q.pid, "name": q.name}
                for qid in hub.rooms[room]
                if qid != p.pid and (q := hub.players.get(qid))
            ]

            await p.ws.send_json({"t": "joined", "id": p.pid, "room": room, "players": roster})

            peer_targets = [hub.players[i].ws for i in hub.rooms[room] if i != p.pid and i in hub.players]
            if peer_targets:
                await asyncio.gather(
                    *(ws.send_json({"t": "join", "id": p.pid, "name": name}) for ws in peer_targets),
                    return_exceptions=True,
                )

    elif t == "leave":
        await hub.leave_room(p)

    elif t == "ping":
        await p.ws.send_json({"t": "pong", "ts": now})

@router.websocket("/ws/game")
async def game_ws(ws: WebSocket) -> None:
    origin = ws.headers.get("origin", "")
    if not origin_allowed(origin):
        await ws.close(code=4403)
        return

    ip = ws.client.host if ws.client else "unknown"
    if not await hub.can_accept(ip):
        await ws.close(code=4429)
        return

    await ws.accept()
    p = Player(pid=hub.alloc_id(), ws=ws, ip=ip)
    await hub.register(p)

    try:
        await ws.send_json({"t": "hello", "id": p.pid})
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=CFG["idle_timeout"])
            if msg.get("type") == "websocket.disconnect":
                break

            data = msg.get("bytes")
            if data is not None:
                if len(data) > CFG["max_msg_bytes"]:
                    break
                await handle_state(p, data)
                continue

            text = msg.get("text")
            if text is None:
                continue
            if len(text) > CFG["max_msg_bytes"]:
                break
            await handle_ctrl(p, text)

    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
        pass
    finally:
        await hub.unregister(p)

@router.get("/game/api/stats")
async def game_stats() -> dict[str, Any]:
    return {
        "players": len(hub.players),
        "rooms": len(hub.rooms),
        "detail": {room: len(ids) for room, ids in hub.rooms.items()},
        "limits": {
            "max_players": CFG["max_players"],
            "max_rooms": CFG["max_rooms"],
            "max_per_room": CFG["max_per_room"],
        },
    }
