"""AgentCore Runtime entrypoint: POST /invocations and GET /ping on port 8080 (bedrock-agentcore SDK).

The deployment package's main.py runs this app. Locally it is exercised in-process (tests/backend).
acceptmatch.* log lines (request IDs, model calls, tools used, token counts; never customer text or
secrets) go to stdout, which AgentCore Runtime sends to CloudWatch Logs.
"""

from __future__ import annotations

import logging
import sys

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .remote import handle

_log = logging.getLogger("acceptmatch")
if not _log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    return handle(payload)
