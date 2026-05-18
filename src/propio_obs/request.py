"""Request handle — opaque token returned by start_request, passed to all subsequent verbs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Request:
    request_id: str
    request_type: str
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    turn_id: Optional[str] = None
    tenant_id: Optional[str] = None
    user_id_hash: Optional[str] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    # Internal scratch — exporters may stash backend-specific handles here.
    _state: Dict[str, Any] = field(default_factory=dict)
