# Pi native compaction

This module exports the configuration developed in the `pi compact debug` workstream.
It does not export that workstream's session history, credentials, or recovery records.

## Behavior

The extension adds `context_management` with `type: "compaction"` to supported GPT Responses requests.
It saves completed encrypted compaction items and later response items as private session checkpoints.
Later requests replay that exact window and append new messages and tool results.

Checkpoints follow the active session branch.
Resume, reload, and branch navigation retain the correct checkpoint.
Provider, model, API, and endpoint-origin changes cannot silently reuse another endpoint's checkpoint.

The extension retains the current provider's authentication, catalog, headers, and filtering.
It keeps `store: false` and uses HTTP/SSE for affected requests.
It does not use WebSocket transport for those requests.
It does not send conversation history to another provider.

Other models retain Pi's normal behavior.

## Commands

| Command | Purpose |
|---|---|
| `/native-compact status` | Show activation, model, threshold, and checkpoint. |
| `/compact` | Request native compaction for a supported GPT session. |
| `/native-compact now` | Request the same compaction through the extension command. |
| `/native-compact recover` | Recover oversized history through smaller requests. |
| `/native-compact off` | Restore readable history and Pi compaction for this session. |
| `/native-compact on` | Enable native compaction for this session. |

Wait for compaction to finish before a model change.

## Threshold

The default threshold is 200,000 tokens.
For smaller context windows, the extension uses 70% of the model's declared limit.
This threshold is an extension default, not an OpenAI service default.

Select another threshold during full installation:

```bash
python3 install.py --native-threshold 150000
```

The installer accepts integer values from 1,024 through 1,000,000 tokens.
Model-specific safety limits still apply.

## Oversized-history recovery

The recovery planner retains tool calls with their results.
It splits rejected requests only at complete message-group boundaries.
Each completed batch saves a checkpoint before the next request.
Each request has a three-minute deadline and at most two provider retries.
Valid progress has no cumulative timeout.

The recovery command omits old opaque reasoning from the request copy.
Opaque reasoning means unreadable encrypted data returned by the provider.
The command does not remove that reasoning from the saved session history.
Normal later requests keep their usual reasoning behavior.

If recovery stops, run `/native-compact recover` to continue from the saved checkpoint.
If an indivisible message group remains too large, the extension fails without deleting its content.

Compaction-only requests expose no task tools.
The extension cancels unnecessary generation after a complete native compaction item arrives.
An aborted generation after that boundary is not a passed generation result.
If final usage is absent, usage remains unknown.

## Validation boundaries

The test suite uses local fake Responses endpoints and synthetic histories.
It covers exact replay, tool continuation, model boundaries, resume, reload, failure, and cancellation.
It also exercises a fresh home-directory installation outside the repository.

The development lockfile pins Pi `0.84.4` and patched dependencies.
Live availability depends on the operator's provider and account.
A provider might support inline native compaction without a separate `/responses/compact` endpoint.
The extension uses inline Responses compaction.

References:

- https://developers.openai.com/api/docs/guides/context-management
- https://developers.openai.com/api/docs/guides/preserved-reasoning
- https://github.com/earendil-works/pi-mono
