"""Regression tests for issue Lingtai-AI/lingtai#113.

lark-oapi's ``ws.Client.start()`` calls ``asyncio.get_event_loop()`` and then
``loop.run_until_complete(...)``. When ``_ws_loop`` runs in a daemon thread that
inherits (or is otherwise associated with) the main process's already-running
event loop, ``start()`` fails with::

    RuntimeError: This event loop is already running

The fix: ``_ws_loop`` must create a *fresh* event loop, set it as the current
thread's loop before calling ``start()``, and close/unset it afterwards.

These tests fake ``ws_client.start()`` so they never touch the network or real
lark credentials, and assert on the event-loop state observed inside the thread.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lingtai_feishu.account import FeishuAccount  # noqa: E402


class FakeWsClient:
    """Stand-in for ``lark_oapi.ws.Client``.

    ``start()`` records the event loop that is current *inside the WS thread*,
    mimicking lark-oapi's ``asyncio.get_event_loop()`` lookup, then blocks until
    ``stop()`` is called so the thread stays alive like the real client.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self._stop = threading.Event()
        self.loop_in_thread: asyncio.AbstractEventLoop | None = None
        self.get_loop_error: Exception | None = None
        self.stopped = False

    def start(self) -> None:
        try:
            self.loop_in_thread = asyncio.get_event_loop()
        except RuntimeError as exc:  # pragma: no cover - exercised by bug repro
            self.get_loop_error = exc
        self.started.set()
        # Block like the real blocking client until stop() is signalled.
        self._stop.wait()

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()


def _make_account() -> FeishuAccount:
    return FeishuAccount(
        alias="test",
        app_id="app",
        app_secret="secret",
        allowed_users=None,
    )


def _run_ws_loop_with(account: FeishuAccount, fake: FakeWsClient) -> threading.Thread:
    """Run ``_ws_loop`` in a daemon thread, return once ``start()`` is entered."""
    account._ws_client = fake
    thread = threading.Thread(target=account._ws_loop, daemon=True)
    thread.start()
    assert fake.started.wait(timeout=5.0), "ws_client.start() never invoked"
    return thread


def test_ws_loop_runs_with_fresh_thread_loop():
    """Inside _ws_loop the current loop must be a fresh, thread-local loop."""
    # Establish an ambient running loop on the main thread, like the MCP
    # asyncio stdio server does, so a naive get_event_loop() would see it.
    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    account = _make_account()
    fake = FakeWsClient()
    try:
        thread = _run_ws_loop_with(account, fake)

        assert fake.get_loop_error is None, (
            "get_event_loop() raised inside the WS thread: "
            f"{fake.get_loop_error!r}"
        )
        assert fake.loop_in_thread is not None
        assert fake.loop_in_thread is not main_loop, (
            "WS thread used the ambient main loop instead of a fresh one"
        )

        account.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
    finally:
        main_loop.close()
        asyncio.set_event_loop(None)


def test_ws_loop_closes_loop_after_start_returns():
    """The fresh loop is closed once start() returns (no leak)."""
    account = _make_account()
    fake = FakeWsClient()
    thread = _run_ws_loop_with(account, fake)

    account.stop()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    loop = fake.loop_in_thread
    assert loop is not None
    assert loop.is_closed(), "_ws_loop must close the fresh loop after start() returns"


def test_ws_loop_swallows_start_error_when_stopping():
    """Existing behavior: errors during stop are not logged as unexpected."""

    class RaisingWsClient(FakeWsClient):
        def start(self) -> None:  # type: ignore[override]
            self.started.set()
            raise RuntimeError("boom")

    account = _make_account()
    account._stop_event.set()  # simulate stop already requested
    fake = RaisingWsClient()
    account._ws_client = fake
    thread = threading.Thread(target=account._ws_loop, daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()  # _ws_loop returned cleanly, no propagation
