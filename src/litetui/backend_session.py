"""Provider session state independent of Textual and subprocess implementation."""
from dataclasses import dataclass, field
from enum import Enum


class SessionState(str, Enum):
    DISCONNECTED = 'disconnected'
    CONNECTING = 'connecting'
    READY = 'ready'
    STOPPING = 'stopping'
    FAILED = 'failed'


@dataclass
class BackendSession:
    state: SessionState = SessionState.DISCONNECTED
    generation: int = 0
    active_turn: str | None = None
    failure: str | None = None
    cleanup_errors: list[str] = field(default_factory=list)

    def begin(self):
        self.generation += 1
        self.state = SessionState.CONNECTING
        self.active_turn = None
        self.failure = None
        self.cleanup_errors.clear()
        return self.generation

    def ready(self, generation):
        if generation != self.generation or self.state != SessionState.CONNECTING:
            return False
        self.state = SessionState.READY
        return True

    def disconnected(self, reason=None):
        self.state = SessionState.FAILED if reason else SessionState.DISCONNECTED
        self.failure = reason
        self.active_turn = None

    def start_turn(self, turn_id):
        if self.state != SessionState.READY or self.active_turn is not None:
            raise RuntimeError('Provider is not ready for a new turn')
        self.active_turn = turn_id

    def finish_turn(self, turn_id):
        if self.active_turn != turn_id:
            return False
        self.active_turn = None
        return True


async def cleanup_steps(steps, *, timeout=10):
    """Attempt every cleanup with a bound; return diagnostics, never replace cause."""
    import asyncio
    errors = []
    for step in steps:
        try:
            await asyncio.wait_for(step(), timeout)
        except BaseException as exc:
            errors.append(f'{type(exc).__name__}: {exc}')
    return errors
