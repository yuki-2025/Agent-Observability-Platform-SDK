"""ID minting helpers — request_id, turn_id, conversation_id."""
import uuid


def new_request_id() -> str:
    return str(uuid.uuid4())


def new_turn_id() -> str:
    """Short prefixed id for a single turn within a request."""
    return f"t-{uuid.uuid4().hex[:8]}"


def new_conversation_id() -> str:
    return f"c-{uuid.uuid4().hex[:8]}"
