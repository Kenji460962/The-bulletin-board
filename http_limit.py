"""処理中のHTTPリクエスト数を制限する純ASGIミドルウェア。

- WebSocket / lifespan は数えず素通りさせる(uvicornの --limit-concurrency は
  接続数で数えるため、WebSocketやkeep-alive接続で枠が埋まって503になっていた)。
- 上限を超えたらすぐ503にせず、max_wait 秒まで順番待ちさせる(FIFO)。
  待ち行列が max_queue を超えるか、max_wait 秒待っても枠が空かなかったときだけ503を返す。
- asyncio.Semaphore + wait_for は使わない。古いPythonでは、タイムアウトと取得が同時に起きると
  枠が漏れて永久に減るバグがあるため、枠の受け渡しを自前で行う(単一スレッド前提で競合しない)。
"""
import asyncio
import collections
import time


class HttpConcurrencyLimit:
    def __init__(self, app, limit: int = 150, max_wait: float = 8.0, max_queue: int = 300):
        self.app = app
        self.limit = limit
        self.max_wait = max_wait
        self.max_queue = max_queue
        self.inflight = 0                       # 処理中の数(待ち行列は含まない)
        self.waiters = collections.deque()      # 枠待ちのFuture
        self.rejected = 0                       # 503を返した累計
        self.peak = 0                           # inflightの最大値
        self._last_log = 0.0

    def stats(self) -> dict:
        return {"inflight": self.inflight, "waiting": len(self.waiters),
                "rejected": self.rejected, "peak": self.peak, "limit": self.limit}

    # ---- 枠の取得・返却 ----
    async def _acquire(self) -> bool:
        # 不変条件: 生きている待ち手がいる間は inflight == limit(枠は譲渡され、減らない)
        if self.inflight < self.limit:
            self.inflight += 1
            if self.inflight > self.peak:
                self.peak = self.inflight
            return True
        if len(self.waiters) >= self.max_queue:
            return False
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.waiters.append(fut)
        timer = loop.call_later(self.max_wait, self._expire, fut)
        try:
            return await fut
        except asyncio.CancelledError:
            # 枠を譲られた直後にキャンセルされた場合は、その枠を返す
            if fut.done() and not fut.cancelled() and fut.result():
                self._release()
            raise
        finally:
            timer.cancel()
            try:
                self.waiters.remove(fut)
            except ValueError:
                pass

    @staticmethod
    def _expire(fut) -> None:
        if not fut.done():
            fut.set_result(False)

    def _release(self) -> None:
        while self.waiters:
            fut = self.waiters.popleft()
            if not fut.done():
                fut.set_result(True)            # 枠を次の待ち手へそのまま譲る
                return
        self.inflight -= 1

    # ---- 503応答 ----
    async def _reject(self, send) -> None:
        self.rejected += 1
        now = time.monotonic()
        if now - self._last_log > 30:
            self._last_log = now
            print(f"HTTP同時処理数が上限({self.limit})に達し、待ちきれず503を返しました "
                  f"(累計{self.rejected}件 / 待ち{len(self.waiters)}件)")
        body = b"Service Unavailable"
        await send({"type": "http.response.start", "status": 503, "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            (b"retry-after", b"2")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if not await self._acquire():
            return await self._reject(send)
        try:
            await self.app(scope, receive, send)
        finally:
            self._release()
