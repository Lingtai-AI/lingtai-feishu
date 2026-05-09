# Re-review: PR #1 — lingtai-feishu (Voice + Rich Feedback)

**Reviewer:** pr-reviewer
**Date:** 2026-05-07 (re-review of 2026-05-06 initial review)
**Branch:** `feat/voice-rich-feedback`
**Verdict:** ✅ **APPROVE**

---

## Fixes Verified

The patcher addressed both moderate issues from the initial review. The fixes are on the local branch (not yet pushed as a new commit).

### Fix 1: `stop_all()` — temp message cleanup ✅

```python
def stop_all(self, accounts: dict | None = None) -> None:
```

- Now accepts an `accounts` dict (alias → FeishuAccount)
- Snapshots `_active_chats` under lock, clears immediately, then iterates to delete temp messages
- `None`-safe: degrades gracefully to old behavior if accounts not provided
- Each delete wrapped in try/except — one failed delete doesn't block the rest

### Fix 2: `_send()` / `_reply()` — try/finally cleanup ✅

Both methods now wrap the full send path in `try/finally` with `_typing_manager.stop_typing(acct, chat_id)` in the `finally` block.

- `_send()`: `chat_id` is pre-initialized for `receive_id_type == "chat_id"`, then updated from result. If `send_text` throws and we had a known `chat_id`, cleanup still fires.
- `_reply()`: `_chat_id` is always available from `_parse_compound_id`, so cleanup always fires.

### Shutdown cleanup ✅

`FeishuManager.stop()` now calls `_typing_manager.stop_all(...)` with the full accounts dict before stopping the service — catches any orphan temp messages.

---

## No New Issues

The fixes are clean, correctly indented, and introduce no regressions. The typing lifecycle is now fully robust: start on message receipt, stop on send/reply (even on failure), and clean up on shutdown.

---

## Verdict

**APPROVE.** Both issues resolved. Ship it.
