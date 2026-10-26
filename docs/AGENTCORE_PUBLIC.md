# AgentCore integration boundary

This repository publishes the AgentCore client and runtime abstractions for review, but it does not publish an
operational deployment runbook, live infrastructure identifiers, account details, logs, or deployment automation.
The live path is unverified on this sanitized branch.

## Implemented components

- `backend/agent/agentcore_client.py` invokes an AgentCore runtime through the AWS SDK, parses its response, and
  records redacted execution evidence.
- `backend/agentcore/app.py` exposes the AgentCore Runtime entry point.
- `backend/agentcore/remote.py` reconstructs a read-only shopping service from a sanitized inventory snapshot and
  runs the shared orchestration layer.
- `tests/backend/test_backend_agentcore.py` tests both sides without AWS by using a scripted model and a botocore
  stubber.

The local FastAPI process remains the source of truth. It sends the remote shopping agent only the fields needed to
search sellable inventory. It does not send customer profiles, orders, returns, uploaded photos, credentials, or
storage keys. Returned item identifiers are checked against DynamoDB Local before the API builds product cards.

## Placeholder-only configuration

Keep real values in the process environment or an ignored `.local/agentcore.json` file:

```json
{
  "runtime_arn": "arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:runtime/<RUNTIME_NAME>-<SUFFIX>",
  "model_id": "<BEDROCK_MODEL_OR_INFERENCE_PROFILE_ID>"
}
```

Equivalent environment variables are:

```bash
APP_MODE=agentcore
AGENTCORE_RUNTIME_ARN='arn:aws:bedrock-agentcore:<REGION>:<ACCOUNT_ID>:runtime/<RUNTIME_NAME>-<SUFFIX>'
AGENTCORE_MODEL_ID='<BEDROCK_MODEL_OR_INFERENCE_PROFILE_ID>'
AGENTCORE_QUALIFIER='DEFAULT'
```

Do not commit substituted values. Standard AWS SDK credential resolution supplies credentials at runtime; the code
does not package credentials with the remote agent.

## Publication limits

The portfolio branch intentionally excludes infrastructure creation, update, invocation, log-inspection, and
destruction scripts. Before any live use, separately review IAM scope, network and data boundaries, regional model
routing, logging, retention, cost controls, runtime packaging, and teardown procedures in the target account.

Offline contract tests do not establish that a runtime exists, that IAM permits invocation, that a model is
available in the selected region, or that live latency and output quality are acceptable.
