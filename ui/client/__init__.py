"""Backend client selection.

Only this package knows whether a screen talks to the mock or to the live
FastAPI backend. V1 (app_v1.py) keeps using get_client(); the V3 customer
site uses get_agent_client(journey, ...), which picks a backend per journey
(mock or api) and never falls back to the mock when the live backend is
misconfigured. The site never calls AWS itself.
"""

import os

from ..config import ConfigError
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


def get_agent_client(journey: str, settings, overlay):
    mode = settings.backend_for(journey)
    if mode == "mock":
        from .mock_agent import MockAgentClient

        return MockAgentClient(journey, overlay, settings)
    if mode == "api":
        if not settings.api_base_url:
            raise ConfigError("ACCEPTMATCH_API_BASE_URL is missing or invalid: use the backend's https URL "
                              f"(http is only allowed for localhost). Journey '{journey}'.")
        from .api_agent import ApiAgentClient

        return ApiAgentClient(journey, settings)
    if mode in ("agentcore", "local"):
        raise ConfigError(f"The direct '{mode}' client was superseded: set ACCEPTMATCH_AGENT=api and let the "
                          f"FastAPI backend call AgentCore (journey '{journey}').")
    raise ConfigError(f"Unknown backend '{mode}' for journey '{journey}'. Use mock or api.")
