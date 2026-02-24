# Inbox Model — Design Document

## Problem

Currently, each session has a single `selection_event` and `active` flag. When `present_choices` is called:
1. It sets `session.active = True`, stores choices, and blocks on `session.selection_event.wait()`
2. When the user selects, `_do_select` sets `session.selection` and `session.selection_event.set()`
3. The blocked thread returns the selection

This breaks when:
- **Parallel choices**: If two callers (e.g. `io-mcp-send` and an agent) both call `present_choices` on the same session, the second clobbers the first's state
- **Queued speech**: `speak` calls during an active `present_choices` should queue, not interleave
- **Lost calls**: A `present_choices` arriving while another is active gets overwritten silently

## Solution: Per-Session Inbox Queue

### Data Model (already implemented in session.py)

```python
@dataclass
class InboxItem:
    kind: str          # "choices" or "speech"
    preamble: str = ""
    choices: list = field(default_factory=list)
    text: str = ""
    blocking: bool = False
    priority: int = 0
    result: Optional[dict] = None
    event: threading.Event = field(default_factory=threading.Event)
    timestamp: float = field(default_factory=time.time)
    done: bool = False

class Session:
    inbox: collections.deque[InboxItem]  # pending items
    inbox_done: list[InboxItem]          # completed items (for history)
```

### Key Design: Drain Loop

The core change is restructuring `_present_choices_inner` from a one-shot method into a **drain pattern**:

```
present_choices(session, preamble, choices):
    item = InboxItem(kind="choices", preamble=preamble, choices=choices)
    session.enqueue(item)

    # If not front of queue, just wait on our own event
    if session.peek_inbox() is not item:
        item.event.wait()  # blocked until we reach front AND are resolved
        return item.result

    # We're at the front — show and wait
    _show_and_wait(session, item)
    return item.result

_show_and_wait(session, item):
    # Set up session state from item
    session.preamble = item.preamble
    session.choices = item.choices
    session.active = True

    # Do TTS intro, show UI, pregenerate
    _do_tts_and_ui(session, item)

    # Block on THIS item's event (not session.selection_event)
    item.event.wait()

    # After resolution, activate next item if any
    next = session.peek_inbox()
    if next and next.kind == "choices":
        # Re-enter _show_and_wait for the next item
        # BUT: this runs in the PREVIOUS caller's thread!
        # Solution: just set() the next item's event — its own
        # thread will wake up and call _show_and_wait itself
        pass  # next item's thread handles its own presentation
```

### The Thread Ownership Problem

The key challenge: each `present_choices` call runs in its own HTTP thread. When item A finishes and item B should show next:

1. **Option A: Item B's thread handles presentation** — Item B's thread is blocked at `item.event.wait()`. When A finishes, B's thread wakes up, sees it's now at the front, and does its own `_show_and_wait`. This is clean but means we need TWO code paths in `_present_choices_inner`:
   - Front-of-queue: show immediately
   - Not-front: wait, then when woken, check if we're now at the front, then show

2. **Option B: Shared presentation thread** — A background thread per session that drains the inbox. `present_choices` just enqueues and waits. The drain thread handles all TTS/UI. This is cleaner but adds another thread.

**Recommended: Option A** — simpler, no extra threads.

### Implementation Plan

1. **Modify `_present_choices_inner`**:
   ```python
   def _present_choices_inner(self, session, preamble, choices):
       item = InboxItem(kind="choices", ...)
       session.enqueue(item)

       while True:
           front = session.peek_inbox()
           if front is item:
               # We're at the front — present choices
               self._activate_and_present(session, item)
               item.event.wait()  # wait for user selection

               # Resolved — drain from inbox
               session.peek_inbox()  # moves done items

               # Wake next queued item's thread (if any)
               next = session.peek_inbox()
               if next and not next.done:
                   # Don't set event yet — we set it below
                   pass

               return item.result
           else:
               # Not front — wait for our turn
               # Use a separate "turn" event or poll
               item.event.wait(timeout=0.5)
               if item.done:
                   return item.result
   ```

2. **Modify `_do_select` and all `selection_event.set()` calls**:
   Instead of setting `session.selection` + `session.selection_event.set()`, call:
   ```python
   item = getattr(session, '_active_inbox_item', None)
   if item:
       item.result = {"selected": label, "summary": summary}
       item.done = True
       item.event.set()
   # Also keep session.selection_event.set() for backward compat
   session.selection = result
   session.selection_event.set()
   ```

3. **Update `_show_choices`**: No changes needed — it reads from `session.choices` which is set per-item.

4. **Update tab bar**: Show inbox count badge when `session.inbox_choices_count() > 1`.

5. **Speech queuing**: Speech items go into the same inbox. The TTS engine plays them in order. Non-blocking speech (`speak_async`) doesn't need inbox — only blocking speech benefits from queuing.

### Files to Change

- `src/io_mcp/tui/app.py` — `_present_choices_inner`, `_do_select`, all `selection_event.set()` callers
- `src/io_mcp/tui/app.py` — tab bar rendering (inbox count badge)
- `src/io_mcp/__main__.py` — `_tool_present_choices` loop (handle new return shape)
- `src/io_mcp/tui/app.py` — `_show_waiting_with_shortcuts` (waiting view with shortcuts)

### Testing

- Add test: two concurrent `present_choices` on same session → both resolve in order
- Add test: `present_choices` during active choices → queued, not clobbered
- Add test: `speak` during active choices → plays after current TTS
- Add test: inbox count updates tab bar

### Migration

The inbox model is backward compatible:
- Single-caller sessions work exactly as before (inbox always has 0 or 1 items)
- `session.selection` and `session.selection_event` remain for backward compat
- Existing tests pass without changes
