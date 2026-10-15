"""Backend client selection.

Only this package knows whether the UI talks to the mock or to a live agent.
ACCEPTMATCH_BACKEND=mock is the default. A live adapter for Hengru's agent
will be added once the interface in contracts.py is agreed.
"""

import os

from .contracts import AgentClient
from .mock import MockClient


def get_client() -> AgentClient:
    mode = os.getenv("ACCEPTMATCH_BACKEND", "mock").strip().lower()
    if mode == "mock":
        return MockClient()
    raise RuntimeError(
        f"Backend '{mode}' is not available yet. The live agent adapter is pending agreement "
        "with the backend owner. Unset ACCEPTMATCH_BACKEND or set it to 'mock'."
    )
