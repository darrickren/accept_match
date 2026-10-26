"""AgentInvoker: the one place that knows which agent runs (APP_MODE).

mock           scripted turns and simulated photo analysis (analysis_source SIMULATED)
bedrock-local  Strands Agent + Amazon Bedrock (analysis_source BEDROCK, with model_id)
gemini-local   Strands Agent + Google Gemini API (analysis_source GEMINI, with model_id)
agentcore      Buy Defective: the Strands agent on Amazon Bedrock AgentCore Runtime (analysis_source AGENTCORE);
               Return: the scripted mock (SIMULATED), labelled as such

There is no fallback between them: a model failure is reported as
AI_UNAVAILABLE / RATE_LIMITED / AGENT_TIMEOUT / AI_OUTPUT_INVALID, never
answered by the mock.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ..vision import VisionResult
from . import mock
from .base import ReturnTools, TurnResult


class AgentInvoker:
    def __init__(self, settings: Any, real: Optional[Any] = None, remote: Optional[Any] = None) -> None:
        self.settings = settings
        self.mode = settings.app_mode
        self._real = real
        self._remote = remote
        if self._real is None and self.mode == "bedrock-local":
            from .strands_agent import BedrockAgent  # imported only in real modes

            self._real = BedrockAgent(settings)
        elif self._real is None and self.mode == "gemini-local":
            from .strands_agent import GeminiAgent

            self._real = GeminiAgent(settings)
        if self._remote is None and self.mode == "agentcore":
            from .agentcore_client import AgentCoreShopping

            self._remote = AgentCoreShopping(settings)

    # ------------------------------------------------------------ who answers each journey

    def real_for(self, journey: str) -> bool:
        """True when a real model answers this journey ("shopping" or "return")."""
        if self.mode == "agentcore":
            return journey == "shopping"
        return self.mode != "mock"

    def source_for(self, journey: str) -> str:
        if self.mode == "agentcore":
            return "AGENTCORE" if journey == "shopping" else "SIMULATED"
        return "SIMULATED" if self.mode == "mock" else self._real.source

    def model_for(self, journey: str) -> Optional[str]:
        if self.mode == "agentcore":
            return self._remote.model_id if journey == "shopping" else None
        return None if self.mode == "mock" else self._real.model_id

    @property
    def real(self) -> bool:
        return self.mode != "mock"

    @property
    def provider(self) -> str:
        if self.mode == "agentcore":
            return "agentcore"
        return "mock" if self.mode == "mock" else self._real.provider.name

    @property
    def model_id(self) -> Optional[str]:
        return self.model_for("shopping")

    @property
    def vision_model_id(self) -> Optional[str]:
        if self.mode in ("mock", "agentcore"):
            return None
        return self._real.vision_model_id or self._real.model_id

    @property
    def analysis_source(self) -> str:
        """The photo-review source (returns)."""
        return self.source_for("return")

    # ------------------------------------------------------------ turns

    def greeting(self, mode: str, inventory: Any) -> Dict[str, Any]:
        if mode == "shopping":
            return self._real.greeting(inventory) if self.mode in ("bedrock-local", "gemini-local") else \
                mock.greeting(inventory)
        return {"message": "Tell me about the return: the demo order ID, what happened, and when you noticed it.",
                "questions": [], "state": {"facts": {}, "asked": []}}

    def shopping_turn(self, state: Dict[str, Any], text: str, inventory: Any,
                      session_id: Optional[str] = None) -> TurnResult:
        if self.mode == "agentcore":
            return self._remote.shopping_turn(state, text, inventory, session_id=session_id)
        if self.mode == "mock":
            return mock.shopping_turn(state, text, inventory)
        return self._real.shopping_turn(state, text, inventory)

    def return_turn(self, state: Dict[str, Any], text: str, tools: ReturnTools) -> TurnResult:
        if self.mode in ("mock", "agentcore"):
            return mock.return_turn(state, text, tools, self.settings.rules)
        return self._real.return_turn(state, text, tools, self.settings.rules)

    def analyze_photos(self, photos: List[Dict[str, Any]], expected_views: List[str], facts: Dict[str, Any],
                       line: Dict[str, Any], images: Dict[str, bytes]) -> Tuple[VisionResult, Optional[Dict[str, Any]]]:
        """(merged visual observation, redacted run evidence or None for the mock)."""
        if self.mode in ("mock", "agentcore"):
            return mock.analyze(photos, expected_views, facts, line, self.settings.rules), None
        return self._real.analyze_photos(photos, expected_views, facts, line, images, self.settings.rules)
