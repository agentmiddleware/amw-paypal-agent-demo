# amw-paypal-agent-demo

An OpenAI agent buys something through PayPal sandbox, and a governed gateway stands between the agent and the money.

The agent gets a permit capped at the amount the user allowed. The gateway checks that permit before the PayPal tool runs, debits the wallet at most once per idempotency key, and signs an Ed25519 receipt. Receipts verify offline with the vendored MIT verifier (`b2a_sdk.receipt_verifier`) and nothing else. If the response is lost after the receipt is stored, a retry with the same key returns the original receipt instead of dispatching again.

## What runs here, honestly

The gateway in this repo is `mini_gateway/`: a small Apache 2.0 reference gateway I wrote for this demo, about 1,000 lines of Python. It serves the HTTP contract that Agent Middleware's (AMW) MIT client SDK speaks: wallets, API keys, permits, MCP `tools/call`, signed receipts and the published key set. That is what lets the whole thing run from this repo with one plain pip install and no GitHub access.

It is **not** the production AMW core. The AMW gateway is Fair Source (FSL-1.1-ALv2), see https://github.com/agentmiddleware/agent-middleware. It is not included here, and is not needed to run anything in this repo. The mini gateway has no velocity limits, no daily limits (it accepts `daily_limit` and ignores it), no KYC, no multi node storage and no admin UI.

## PayPal AI Hackathon (AMW Preview)

This entry demonstrates governed PayPal sandbox checkout with the self-contained `mini_gateway` reference gateway. It is not production AMW core. The five scenes show a successful sandbox checkout, replay after a lost response, a budget denial, replay of the same signed denial receipt, and offline Ed25519 verification. The MCP call carries its idempotency key in `params._meta["io.agentmiddleware/idempotency_key"]`; the existing header and `mcpContext` paths remain accepted. A disagreement between supplied keys is rejected.

Judge run without PayPal or OpenAI credentials (install dependencies before going offline):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python demo.py --mock-paypal --mock-llm
.venv/bin/python -m pytest
```

Judge run with PayPal SANDBOX: set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` from a PayPal sandbox app, and `OPENAI_API_KEY` for the OpenAI model. The optional `PAYPAL_SANDBOX_CARD_NUMBER` and `PAYPAL_SANDBOX_CARD_EXPIRY` values must come from PayPal's sandbox card generator if a buyer cannot approve the order. Keep `PAYPAL_BASE_URL` at the sandbox endpoint. Put credentials in an untracked `.env`, load it locally, then run:

```bash
set -a; . ./.env; set +a
.venv/bin/python demo.py
```

The PayPal sandbox card capture path has not been verified against the remote sandbox from this repository. The offline command above is the reproducible judge path.

### What changed since Oct 1, 2026

The first commit in this repository is dated Oct 14, 2026, so there is no Oct 1 repository version to compare. Since then, this repo added the governed checkout agent and offline tests, replaced the external gateway dependency with the Apache 2.0 `mini_gateway` reference implementation and vendored MIT clients, and added signed receipt verification. This preview adds MCP `_meta` idempotency keys and stored, signed replay of budget denials. These are behaviors of this reference gateway, not claims about production AMW.

## What the demo shows

`demo.py` runs five scenes against the mini gateway, in this process.

1. **Happy path.** Permit capped at 25.00 USD, one PayPal order created and captured, one signed receipt.
2. **Lost response, then retry.** The gateway charges, PayPal captures, the gateway writes the receipt, and then the response times out before it reaches the agent. For this completed call, a retry with the same key returns the original receipt instead of dispatching again. In this run, PayPal shows one capture and the wallet shows one debit.
3. **Over budget.** The user allows 12.50 USD and the price is 25.00 USD. The gateway answers `permit_budget_exceeded` before the tool runs, so no PayPal request is sent.
4. **Retry after denial.** The MCP key is shown in `_meta`. The same denied call is retried with the same key; both attempts return the same signed receipt with no PayPal request or wallet debit.
5. **Offline receipt verification.** The receipt from scene 1 and the published key set are written to `out/`, the gateway shuts down, and the Ed25519 signature is checked from those files alone. An edited copy is rejected.

## How it fits together

* `mini_gateway/` is the gateway. For each `tools/call` it does this, in order:
  1. Resolves the idempotency key from MCP `_meta`, the header, or `mcpContext`, and rejects disagreement. For a finished key, a retry with the same key returns the original receipt instead of dispatching again. A key whose first attempt is still running gets `idempotency_in_progress`. A key whose first attempt was cut off, or was left in flight by a process that died, gets `delivery_uncertain` with a signed receipt saying so, and the charge is held for a human to look at.
  2. Checks the permit: right wallet, not expired, tool and scope allowed, and the price fits in what is left of `max_credits`. A policy denial is signed and stored under the key before any charge or dispatch. A retry replays that same denial receipt.
  3. Debits the wallet and marks the key in flight, in the same SQLite transaction as the checks.
  4. Runs the tool. If it raises, the charge and the permit budget are refunded. Either way a signed receipt is stored under the key.
* `paypal_client.py` does sandbox OAuth, `POST /v2/checkout/orders` and `POST /v2/checkout/orders/{id}/capture`. Every write sends `PayPal-Request-Id`, a SHA256 hash of the idempotency key (with the operation name mixed in, so create and capture never share an id). Credentials come only from `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` and are never logged.
* `paypal_tool.py` registers `paypal_checkout` on the gateway with `ToolRegistry.register_local`, using the b2a_sdk `@mcp_tool` metadata. The merchant sets the price at registration. One credit is one US cent, so the gateway charges and caps the real order amount. The gateway also fills in the tool's `amw_idempotency_key` argument from the call's own key, so a caller can not pick a different one.
* `agent.py` is the LLM agent. It uses AMW's MIT OpenAI wrapper (`GovernedToolRunner`), which creates the permit with `max_credits` equal to the user's limit and derives the idempotency key from the model's `tool_call.id`. The model only chooses the item description; it can not set the price or the key.
* `gateway.py` starts the mini gateway on SQLite, generates a fresh admin key and signing key for each run, and creates wallets through the gateway's HTTP routes.
* `fakes.py` holds the offline PayPal stand in, a scripted LLM, and the transport that loses a response.
* `vendor/` holds the two MIT SDKs: `b2a_sdk` (client, models, offline receipt verifier, `b2a-verify-receipt` CLI) and `openai-agent-middleware` (`openai_b2a`).

## Setup

Python 3.11 or newer. From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` installs the two vendored SDKs from `vendor/` and this project, all editable, plus their PyPI dependencies. With `uv` it is `uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt`.

## Run

Fully offline (no credentials needed):

```bash
.venv/bin/python demo.py --mock-paypal --mock-llm
```

With real services, fill in `.env` (copy it from `.env.example`) and load it first:

```bash
set -a; . ./.env; set +a
.venv/bin/python demo.py
```

The demo uses the PayPal sandbox when both PayPal variables are set, and OpenAI when `OPENAI_API_KEY` is set. Either one can be forced offline with `--mock-paypal` or `--mock-llm`. Add `--json` for a machine readable summary.

Verify a receipt yourself with the SDK command line tool:

```bash
.venv/bin/b2a-verify-receipt --bundle "$(ls -t out/rcpt_*.json | head -n 1)" --keys out/trust-keys.json
```

Tests (always offline; a guard fails any test that opens a network connection):

```bash
.venv/bin/python -m pytest
```

Docker builds from this repo alone:

```bash
docker compose up --build demo
docker compose --profile tests run --rm tests
```

## PayPal sandbox accounts

1. Sign in at developer.paypal.com and open Apps and Credentials, Sandbox tab.
2. Create an app. Its Client ID and Secret go into `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`.
3. Under Sandbox accounts you get a business account (the merchant, which receives the money) and a personal account (a buyer). Check the business account balance there after a run.
4. A server side agent has no buyer to click Approve. To let the order be captured with no approval step, put a sandbox test card from the developer dashboard card generator into `PAYPAL_SANDBOX_CARD_NUMBER` and `PAYPAL_SANDBOX_CARD_EXPIRY` (format `YYYY-MM`). Without it, PayPal refuses the capture with `ORDER_NOT_APPROVED`. This card path has not been run against the live sandbox from this repository yet.

## Limits worth knowing

* The gateway prices a tool per call from its registration, not from the call arguments. That is why the merchant fixes the price at registration and the permit cap is compared against it.
* The gateway charges before the tool body runs and refunds if the tool raises. If the process dies after PayPal captures but before the receipt is stored, the next start marks that key `delivery_uncertain` and keeps the charge; a human settles it. The PayPal client reuses `PayPal-Request-Id` if the tool body runs again under that key.
* The signing key is generated per run and lives only in memory, so receipts from an earlier run verify only against the `trust-keys.json` saved from that run.
* SQLite is used from one event loop with one connection. That is right for a demo running in one process and wrong for a multi process server.
* `GovernedToolRunner` stores operation records in memory in this demo. A real agent that must survive a crash should pass `JsonFileOperationKeyStore` or its own store.

## License

This repository is Apache 2.0. The separate production AMW gateway is Fair Source (FSL-1.1-ALv2).

* This project, including `mini_gateway/`, is Apache-2.0 (see `LICENSE`).
* `vendor/b2a_sdk` and `vendor/openai-agent-middleware` are AMW's MIT client SDKs, copied with their own `LICENSE` files. Copyright stays with their authors.
* The AMW gateway is not included, not fetched and not required. It is Fair Source (FSL-1.1-ALv2); see https://github.com/agentmiddleware/agent-middleware.
