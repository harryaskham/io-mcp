"""Tests for freeform reply tagging in chat bubbles.

When a user types a freeform reply (instead of selecting a choice),
the chat bubble should show the reply with connecting characters
(╰─►) linking it to the choice set, in purple to match user message
styling. All choice labels should be dim (no highlight).
"""

import time

from io_mcp.session import InboxItem, Session
from io_mcp.tui.chat_view import ChatBubbleItem


# ─── ChatBubbleItem freeform flag ────────────────────────────────────

class TestChatBubbleItemFreeform:
    """Test the freeform flag on ChatBubbleItem."""

    def test_freeform_flag_default_false(self):
        """Freeform flag should default to False."""
        item = ChatBubbleItem(
            kind="choices",
            text="Pick one",
            timestamp=time.time(),
            resolved=True,
            result="Option A",
            choices=[{"label": "Option A"}, {"label": "Option B"}],
        )
        assert item.bubble_freeform is False

    def test_freeform_flag_set_true(self):
        """Freeform flag should be settable to True."""
        item = ChatBubbleItem(
            kind="choices",
            text="Pick one",
            timestamp=time.time(),
            resolved=True,
            result="custom reply",
            choices=[{"label": "Option A"}, {"label": "Option B"}],
            freeform=True,
        )
        assert item.bubble_freeform is True

    def test_freeform_tts_text(self):
        """Freeform resolved choices should say 'replied' instead of 'selected'."""
        item = ChatBubbleItem(
            kind="choices",
            text="Pick one",
            timestamp=time.time(),
            resolved=True,
            result="custom reply",
            choices=[{"label": "Option A"}, {"label": "Option B"}],
            freeform=True,
        )
        assert item.tts_text == "replied: custom reply"

    def test_non_freeform_tts_text(self):
        """Normal resolved choices should still say 'selected'."""
        item = ChatBubbleItem(
            kind="choices",
            text="Pick one",
            timestamp=time.time(),
            resolved=True,
            result="Option A",
            choices=[{"label": "Option A"}, {"label": "Option B"}],
            freeform=False,
        )
        assert item.tts_text == "selected Option A"

    def test_freeform_unresolved_not_marked(self):
        """Unresolved items should not show freeform text."""
        item = ChatBubbleItem(
            kind="choices",
            text="Pick one",
            timestamp=time.time(),
            resolved=False,
            result="",
            choices=[{"label": "Option A"}],
            freeform=True,  # freeform flag but not resolved
        )
        # Should use the normal unresolved text (labels listed)
        assert "replied" not in item.tts_text
        assert "Option A" in item.tts_text


# ─── Freeform detection logic ────────────────────────────────────────

class TestFreeformDetection:
    """Test the logic for detecting freeform replies from inbox items."""

    def test_freeform_result_detection(self):
        """Results with summary='(freeform input)' should be detected."""
        result = {"selected": "my typed reply", "summary": "(freeform input)"}
        is_freeform = result.get("summary", "") == "(freeform input)"
        assert is_freeform is True

    def test_normal_result_not_freeform(self):
        """Normal selection results should not be detected as freeform."""
        result = {"selected": "Option A", "summary": "Some description"}
        is_freeform = result.get("summary", "") == "(freeform input)"
        assert is_freeform is False

    def test_no_summary_not_freeform(self):
        """Results without summary should not be detected as freeform."""
        result = {"selected": "Option A"}
        is_freeform = result.get("summary", "") == "(freeform input)"
        assert is_freeform is False

    def test_none_result_not_freeform(self):
        """None results should not crash detection logic."""
        result = None
        is_freeform = False
        if result:
            is_freeform = result.get("summary", "") == "(freeform input)"
        assert is_freeform is False

    def test_bubble_from_freeform_inbox_item(self):
        """Building a ChatBubbleItem from a freeform inbox item result."""
        item = InboxItem(
            kind="choices",
            preamble="What should we do?",
            choices=[{"label": "A", "summary": ""}, {"label": "B", "summary": ""}],
        )
        item.result = {"selected": "my custom reply", "summary": "(freeform input)"}
        item.done = True

        # Simulate what _collect_chat_items does
        result_label = ""
        is_freeform = False
        if item.result:
            result_label = item.result.get("selected", "")
            is_freeform = item.result.get("summary", "") == "(freeform input)"

        bubble = ChatBubbleItem(
            kind="choices",
            text=item.preamble,
            timestamp=item.timestamp,
            resolved=True,
            result=result_label,
            choices=item.choices[:9],
            freeform=is_freeform,
        )

        assert bubble.bubble_freeform is True
        assert bubble.bubble_result == "my custom reply"
        assert bubble.tts_text == "replied: my custom reply"

    def test_bubble_from_normal_inbox_item(self):
        """Building a ChatBubbleItem from a normal selection."""
        item = InboxItem(
            kind="choices",
            preamble="Pick one",
            choices=[{"label": "Alpha", "summary": ""}, {"label": "Beta", "summary": ""}],
        )
        item.result = {"selected": "Alpha", "summary": "choice summary"}
        item.done = True

        result_label = ""
        is_freeform = False
        if item.result:
            result_label = item.result.get("selected", "")
            is_freeform = item.result.get("summary", "") == "(freeform input)"

        bubble = ChatBubbleItem(
            kind="choices",
            text=item.preamble,
            timestamp=item.timestamp,
            resolved=True,
            result=result_label,
            choices=item.choices[:9],
            freeform=is_freeform,
        )

        assert bubble.bubble_freeform is False
        assert bubble.bubble_result == "Alpha"
        assert bubble.tts_text == "selected Alpha"
