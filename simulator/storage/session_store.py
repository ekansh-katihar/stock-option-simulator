"""
simulator/storage/session_store.py

Interface for holding TradingSession instances between requests/reruns.
InMemorySessionStore is today's implementation (single process, no
persistence). A future SQLiteSessionStore implements the same interface --
Streamlit (and later, a FastAPI backend) won't need to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from simulator.engine.session import TradingSession


class SessionStore(ABC):

    @abstractmethod
    def create(self, session_id: str, session: TradingSession) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, session_id: str) -> TradingSession | None:
        raise NotImplementedError

    @abstractmethod
    def delete(self, session_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_ids(self) -> list:
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    """Holds live TradingSession objects in a plain dict. No persistence across restarts."""

    def __init__(self):
        self._sessions: dict = {}

    def create(self, session_id: str, session: TradingSession) -> None:
        self._sessions[session_id] = session

    def get(self, session_id: str) -> TradingSession | None:
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def list_ids(self) -> list:
        return list(self._sessions.keys())