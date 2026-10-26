"""Select the mock or FastAPI client per customer journey.

Configuration errors never fall back to the mock. The site never calls AWS itself.
"""

from ..config import ConfigError


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
