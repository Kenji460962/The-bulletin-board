import asyncio
import json
import os

import redis.asyncio as redis
import websockets

REDIS_URL = os.environ.get('REDIS_URL')
WS_SHARED_SECRET = os.environ.get('WS_SHARED_SECRET')  # CloudflareのTransform Ruleで付与する合言葉

# room_key -> 接続中のwebsocketの集合
rooms = {}


async def register(ws, room_key):
    rooms.setdefault(room_key, set()).add(ws)
    print(f"[connect] {room_key} (現在 {len(rooms[room_key])} 人)")


async def unregister(ws, room_key):
    if room_key in rooms:
        rooms[room_key].discard(ws)
        if not rooms[room_key]:
            del rooms[room_key]


def parse_room_key(path):
    # 期待する形式: /ws/thread/123 や /ws/game/ABC123 など
    parts = [p for p in path.strip('/').split('/') if p]
    if len(parts) < 3 or parts[0] != 'ws':
        return None
    room_type, room_id = parts[1], parts[2]
    return f"{room_type}:{room_id}"


async def handler(websocket):
    # Cloudflareを経由していないアクセスは弾く(直接この配信サーバーを叩かれるのを防ぐ)
    if WS_SHARED_SECRET:
        headers = websocket.request.headers if hasattr(websocket, 'request') else {}
        if headers.get('X-Origin-Verify') != WS_SHARED_SECRET:
            await websocket.close(code=4403, reason='forbidden')
            return

    path = websocket.request.path if hasattr(websocket, 'request') else '/'
    room_key = parse_room_key(path)
    if not room_key:
        await websocket.close(code=4400, reason='invalid room')
        return

    await register(websocket, room_key)
    try:
        async for _ in websocket:
            # クライアントからのメッセージは今のところ使わない(サーバー→クライアントの一方向配信のみ)
            pass
    except Exception:
        pass
    finally:
        await unregister(websocket, room_key)


async def redis_listener():
    r = redis.from_url(REDIS_URL)
    pubsub = r.pubsub()
    await pubsub.psubscribe('room:*')
    print("Redis購読を開始しました")
    async for message in pubsub.listen():
        if message.get('type') != 'pmessage':
            continue
        channel = message['channel']
        if isinstance(channel, bytes):
            channel = channel.decode()
        room_key = channel.replace('room:', '', 1)
        data = message['data']
        if isinstance(data, bytes):
            data = data.decode()

        listeners = rooms.get(room_key)
        if not listeners:
            continue

        dead = []
        for ws in list(listeners):
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            listeners.discard(ws)


async def main():
    port = int(os.environ.get('PORT', 8765))
    async with websockets.serve(handler, '0.0.0.0', port, ping_interval=20, ping_timeout=20):
        print(f"WebSocketサーバー起動: ポート{port}")
        await redis_listener()


if __name__ == '__main__':
    asyncio.run(main())
