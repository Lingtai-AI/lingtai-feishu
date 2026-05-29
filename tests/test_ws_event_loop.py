"""Regression tests for issue Lingtai-AI/lingtai#113.

lark-oapi's ``ws.Client.start()`` does **not** look up the event loop at call
time. ``lark_oapi.ws.client`` captures a module-level ``loop`` global at import
time::

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

and ``Client.start()`` then runs ``loop.run_until_complete(...)`` against that
*module global*. When the SDK is imported on the main MCP thread while
``asyncio.run(serve())`` is active, the global captures the already-running main
loop. Calling ``run_until_complete()`` on it raises::

    RuntimeError: This event loop is already running

so inbound WebSocket messages are never delivered.

Setting a *thread-current* loop inside ``_ws_loop`` (the original PR #18 attempt)
does **not** help, because ``Client.start()`` ignores the thread-current loop and
uses the module global. The real fix must make ``lark_oapi.ws.client.loop``
resolve to a fresh, thread-owned loop while ``start()`` runs — and must stay
correct when multiple accounts each run their own WS thread, since the SDK
exposes a single shared module global.

These tests reproduce the real SDK contract (module-global ``loop`` used inside
``start()``) without touching the network. They are written as ``unittest``
cases so ``python -m unittest discover`` collects them, since pytest segfaults in
the project's Anaconda Python 3.12 environment.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from lingtai_feishu import account as account_mod  # noqa: E402
from lingtai_feishu.account import FeishuAccount  # noqa: E402


def _make_account(alias: str = "test") -> FeishuAccount:
    return FeishuAccount(
        alias=alias,
        app_id="app",
        app_secret="secret",
        allowed_users=None,
    )


class SdkLoopModule:
    """Faithful stand-in for the ``lark_oapi.ws.client`` module.

    The real SDK keeps a module-level ``loop`` attribute and ``Client.start()``
    references it as ``loop.run_until_complete(...)``. We model exactly that: the
    fake client reads ``module.loop`` *each time* ``start()`` runs, mirroring how
    Python resolves the module global at call time.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop


class SdkBackedWsClient:
    """Stand-in for ``lark_oapi.ws.Client`` that uses the SDK module-global loop.

    ``start()`` does what the real SDK does: ``self._module.loop.run_until_complete``
    on a trivial coroutine, then blocks (like the real ``_select()``) until
    stopped. It records the loop it actually ran on and any RuntimeError raised.
    """

    def __init__(self, module: SdkLoopModule) -> None:
        self._module = module
        self.started = threading.Event()
        self.connected = threading.Event()
        self._stop = threading.Event()
        self.run_loop: asyncio.AbstractEventLoop | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        self.started.set()
        # Mirror lark_oapi.ws.Client.start(): use the *module-global* loop, not
        # the thread-current loop.
        loop = self._module.loop

        async def _connect() -> None:
            # Record the loop this coroutine actually executes on, so the test
            # sees the real (per-thread-resolved) loop, not a shared proxy.
            self.run_loop = asyncio.get_running_loop()

        try:
            loop.run_until_complete(_connect())
        except RuntimeError as exc:
            self.error = exc
            return
        self.connected.set()
        # Block like the real client's _select() until stop() is signalled.
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()


class WsEventLoopTest(unittest.TestCase):
    """Regression coverage for the module-global SDK loop blind spot."""

    def _install_sdk_module(
        self, loop: asyncio.AbstractEventLoop
    ) -> SdkLoopModule:
        """Install a fake SDK module on the account module and clean it up."""
        module = SdkLoopModule(loop)
        prev = getattr(account_mod, "_sdk_ws_client_module", None)
        account_mod._sdk_ws_client_module = module  # type: ignore[attr-defined]
        self.addCleanup(
            setattr, account_mod, "_sdk_ws_client_module", prev
        )
        return module

    def test_ws_loop_uses_thread_loop_for_sdk_module_global(self) -> None:
        """The SDK module-global loop used inside start() must NOT be the
        already-running main loop.

        This reproduces issue #113: with the SDK module global captured as the
        running main loop, ``start()``'s ``run_until_complete`` raises
        ``This event loop is already running``. The fix must rebind the SDK's
        module-global ``loop`` to the WS thread's fresh loop for the duration of
        ``start()``.
        """

        async def main() -> None:
            running = asyncio.get_running_loop()
            # SDK module global == the already-running main loop, as it would be
            # when lark_oapi is imported on the main MCP thread.
            module = self._install_sdk_module(running)

            account = _make_account()
            fake = SdkBackedWsClient(module)
            account._ws_client = fake

            thread = threading.Thread(target=account._ws_loop, daemon=True)
            thread.start()
            self.assertTrue(
                fake.started.wait(timeout=5.0), "start() never invoked"
            )
            # Wait for start() to attempt run_until_complete (success or error).
            connected = await asyncio.get_running_loop().run_in_executor(
                None, lambda: fake.connected.wait(timeout=5.0) or fake.error
            )

            self.assertIsNone(
                fake.error,
                f"start() hit the running-loop bug: {fake.error!r}",
            )
            self.assertTrue(connected)
            self.assertIsNotNone(fake.run_loop)
            self.assertIsNot(
                fake.run_loop,
                running,
                "start() ran against the already-running main loop "
                "(module-global was not rebound to the WS thread loop)",
            )

            account.stop()
            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive())

        asyncio.run(main())

    def test_two_accounts_get_independent_loops(self) -> None:
        """Two concurrent WS threads must each run start() on their own loop.

        The SDK exposes a single module-global loop. A naive rebind makes the
        second thread clobber the first, so the first thread's blocking client
        ends up sharing the second thread's running loop -> RuntimeError. The fix
        must isolate each WS thread's loop even though the SDK global is shared.
        """

        async def main() -> None:
            running = asyncio.get_running_loop()
            module = self._install_sdk_module(running)

            acct_a = _make_account("a")
            acct_b = _make_account("b")
            fake_a = SdkBackedWsClient(module)
            fake_b = SdkBackedWsClient(module)
            acct_a._ws_client = fake_a
            acct_b._ws_client = fake_b

            t_a = threading.Thread(target=acct_a._ws_loop, daemon=True)
            t_b = threading.Thread(target=acct_b._ws_loop, daemon=True)
            t_a.start()
            t_b.start()

            loop = asyncio.get_running_loop()
            for fake in (fake_a, fake_b):
                ok = await loop.run_in_executor(
                    None,
                    lambda f=fake: f.connected.wait(timeout=5.0) or f.error,
                )
                self.assertIsNone(
                    fake.error,
                    f"concurrent start() failed: {fake.error!r}",
                )
                self.assertTrue(ok)

            self.assertIsNotNone(fake_a.run_loop)
            self.assertIsNotNone(fake_b.run_loop)
            self.assertIsNot(
                fake_a.run_loop,
                fake_b.run_loop,
                "two WS threads shared one loop (global clobbered)",
            )
            self.assertIsNot(fake_a.run_loop, running)
            self.assertIsNot(fake_b.run_loop, running)

            acct_a.stop()
            acct_b.stop()
            t_a.join(timeout=5.0)
            t_b.join(timeout=5.0)
            self.assertFalse(t_a.is_alive())
            self.assertFalse(t_b.is_alive())

        asyncio.run(main())

    def test_sdk_module_global_resolves_to_original_from_main_thread(
        self,
    ) -> None:
        """The WS-thread loop binding must not leak to other threads.

        After the WS thread exits, ``module.loop`` accessed from the main thread
        must still behave like the original loop — we must not leave the SDK
        resolving to a closed WS-thread loop, or a later ``start()`` from another
        thread would break.
        """

        async def main() -> None:
            running = asyncio.get_running_loop()
            module = self._install_sdk_module(running)

            account = _make_account()
            fake = SdkBackedWsClient(module)
            account._ws_client = fake

            thread = threading.Thread(target=account._ws_loop, daemon=True)
            thread.start()
            self.assertTrue(fake.started.wait(timeout=5.0))
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: fake.connected.wait(timeout=5.0)
            )

            account.stop()
            thread.join(timeout=5.0)
            self.assertFalse(thread.is_alive())

            # From the main thread, the SDK loop must still resolve to the live
            # original loop, never to the now-closed WS-thread loop. We probe a
            # representative attribute rather than object identity, because the
            # fix may wrap the global in a per-thread proxy.
            self.assertFalse(
                module.loop.is_closed(),
                "SDK module loop resolves to a closed loop from main thread",
            )

        asyncio.run(main())

    def test_ws_loop_swallows_start_error_when_stopping(self) -> None:
        """Existing behavior: errors during stop are not logged as unexpected."""

        class RaisingWsClient:
            def __init__(self) -> None:
                self.started = threading.Event()

            def start(self) -> None:
                self.started.set()
                raise RuntimeError("boom")

            def stop(self) -> None:
                pass

        # Inject a fake SDK module so _ws_loop does not import the real
        # lark_oapi.ws.client (which captures an event loop at import time).
        fallback_loop = asyncio.new_event_loop()
        self.addCleanup(fallback_loop.close)
        self._install_sdk_module(fallback_loop)

        account = _make_account()
        account._stop_event.set()  # simulate stop already requested
        fake = RaisingWsClient()
        account._ws_client = fake
        thread = threading.Thread(target=account._ws_loop, daemon=True)
        thread.start()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())  # returned cleanly, no propagation


if __name__ == "__main__":
    unittest.main()
