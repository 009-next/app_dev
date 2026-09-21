"""時間のかかる処理（AI の判断など）を、リクエストの外で実行する。

- Inline: その場で、同じ接続で実行する（テスト・従来どおりの同期の動作）。判断は順番に実行する（parallel=False）。
- Threaded: 別スレッドで、専用の接続を使って実行する。同時に動かす処理の数を制限する。判断は同時に実行する（parallel=True）。
  サーバーが止まると、実行中の処理は失われる（daemon）。カードは保存済みで、AI の状態は running のまま残る
  （画面は、一定時間を過ぎた running を「中断された可能性」として扱う）。
"""

from __future__ import annotations

import threading


class Inline:
    is_async = False
    parallel = False

    def submit(self, conn, fn) -> None:
        fn(conn)

    def join(self, timeout: float = 0.0) -> bool:
        return True


class Threaded:
    is_async = True
    parallel = True

    def __init__(self, conn_factory, *, close: bool = True, max_running: int = 4):
        self._factory = conn_factory
        self._close = close  # conn_factory が、その処理専用の接続を作るときだけ、終わりに閉じる
        self._slots = threading.Semaphore(max_running)
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def submit(self, conn, fn) -> None:
        def run():
            with self._slots:
                job_conn = self._factory()
                try:
                    fn(job_conn)
                finally:
                    if self._close:
                        job_conn.close()

        t = threading.Thread(target=run, daemon=True, name="ai-job")
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()

    def join(self, timeout: float = 10.0) -> bool:
        """テスト・終了時用。すべての処理が終わったら True。"""
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout)
        return not any(t.is_alive() for t in threads)
