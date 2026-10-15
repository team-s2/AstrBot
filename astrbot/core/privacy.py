"""Process-wide privacy policy, fixed at startup, and run-scoped state."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.core.db.po import ConversationV2

enabled = False


@dataclass
class PrivateRun:
    """State owned by one pipeline, shared only with its child tasks."""

    conversations: dict[str, "ConversationV2"] = field(default_factory=dict)
    selected: dict[str, str] = field(default_factory=dict)
    temp_dir: str = ""
    active: bool = True


current_run: ContextVar[PrivateRun | None] = ContextVar("private_run", default=None)


@contextmanager
def run_scope() -> Iterator[None]:
    """Dispose of conversation state and owned media even on cancellation.

    Child tasks inherit the same state object. Invalidation prevents a detached
    task from repopulating it after its owning pipeline has finished.

    Yields:
        Control to a single message pipeline.
    """
    if not enabled:
        yield
        return
    if state := current_run.get():
        if not state.active:
            raise RuntimeError("The private response has ended")
        yield
        return
    with TemporaryDirectory(prefix="astrbot-private-") as temp_dir:
        state = PrivateRun(temp_dir=temp_dir)
        token = current_run.set(state)
        try:
            yield
        finally:
            state.active = False
            for conversation in state.conversations.values():
                conversation.content = []
                conversation.title = None
            state.conversations.clear()
            state.selected.clear()
            current_run.reset(token)
