# amw-paypal-agent-demo

An OpenAI agent buys something through PayPal sandbox, and a governed gateway stands between the agent and the money.

The agent gets a permit capped at the amount the user allowed. The gateway checks that permit before the PayPal tool runs, debits the wallet at most once per idempotency key, and signs an Ed25519 receipt. Receipts verify offline with the vendored MIT verifier (`b2a_sdk.receipt_verifier`) and nothing else. If the response is lost after PayPal captured the payment, the agent retries the same tool call and gets the same receipt back, with at most one PayPal capture.

## What runs here, honestly

The gateway in this repo is `mini_gateway/`: a small MIT reference gateway I wrote for this demo, about 1,000 lines of Python. It serves the HTTP contract that Agent Middleware's (AMW) MIT client SDK speaks: wallets, API keys, permits, MCP `tools/call`, signed receipts and the published key set. That is what lets the whole thing run from this repo with one plain pip install and no GitHub access.

It is **not** the production AMW core. The AMW core is source available under the Business Source License, is not included here, and is not needed to run anything in this repo. The mini gateway has no velocity limits, no daily limits (it accepts `daily_limit` and ignores it), no KYC, no multi node storage and no admin UI. It does the four things this demo is about and does them carefully.

## What the demo shows

`demo.py` runs four scenes against the mini gateway, in this process.

1. **Happy path.** Permit capped at 25.00 USD, one PayPal order created and captured, one signed receipt.
2. **Lost response, then retry.** The gateway charges, PayPal captures, the gateway writes the receipt, and then the response times out before it reaches the agent. The agent retries the same tool call. It gets the receipt from the lost response, PayPal shows one capture, and the wallet shows one debit.
3. **Over budget.** The user allows 12.50 USD and the price is 25.00 USD. The gateway answers `permit_budget_exceeded` before the tool runs, so no PayPal request is sent.
4. **Offline receipt verification.** The receipt from scene 1 and the published key set are written to `out/`, the gateway shuts down, and the Ed25519 signature is checked from those files alone. An edited copy is rejected.

## How it fits together

* `mini_gateway/` is the gateway. For each `tools/call` it does this, in order:
  1. Looks up the idempotency key. A finished key replays the stored response (same receipt, no new charge, the tool does not run). A key whose first attempt is still running gets `idempotency_in_progress`. A key whose first attempt was cut off, or was left in flight by a process that died, gets `delivery_uncertain` with a signed receipt saying so, and the charge is held for a human to look at.
  2. Checks the permit: right wallet, not expired, tool and scope allowed, and the price fits in what is left of `max_credits`. A denial happens here, before any charge.
  3. Debits the wallet and marks the key in flight, in the same SQLite transaction as the checks.
  4. Runs the tool. If it raises, the charge and the permit budget are refunded. Either way a signed receipt is stored under the key.
* `paypal_client.py` does sandbox OAuth, `POST /v2/checkout/orders` and `POST /v2/checkout/orders/{id}/capture`. Every write sends `PayPal-Request-Id`, a SHA256 hash of the idempotency key (with the operation name mixed in, so create and capture never share an id). Credentials come only from `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` and are never logged.
* `paypal_tool.py` registers `paypal_checkout` on the gateway with `ToolRegistry.register_local`, using the b2a_sdk `@mcp_tool` metadata. The merchant sets the price at registration. One credit is one US cent, so the gateway charges and caps the real order amount. The gateway also fills in the tool's `amw_idempotency_key` argument from the call's own key, so a caller can not pick a different one.
* `agent.py` is the LLM agent. It uses AMW's MIT OpenAI wrapper (`GovernedToolRunner`), which creates the permit with `max_credits` equal to the user's limit and derives the idempotency key from the model's `tool_call.id`. The model only chooses the item description; it can not set the price or the key.
* `gateway.py` starts the mini gateway on SQLite, generates a fresh admin key and signing key for each run, and creates wallets through the gateway's HTTP routes.
* `fakes.py` holds the offline PayPal stand in, a scripted LLM, and the transport that loses a response.
* `vendor/` holds the two MIT SDKs, unmodified: `b2a_sdk` (client, models, offline receipt verifier, `b2a-verify-receipt` CLI) and `openai-agent-middleware` (`openai_b2a`).

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
.venv/bin/b2a-verify-receipt --bundle out/<receipt id>.json --keys out/trust-keys.json
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

## Judge credentials

Put these in `.env`. Values here are placeholders only; never commit real ones.

```bash
OPENAI_API_KEY=<judge OpenAI API key>
OPENAI_MODEL=<model name, default gpt-4.1-mini>
PAYPAL_CLIENT_ID=<judge PayPal sandbox client id>
PAYPAL_CLIENT_SECRET=<judge PayPal sandbox client secret>
PAYPAL_SANDBOX_CARD_NUMBER=<sandbox generated test card number>
PAYPAL_SANDBOX_CARD_EXPIRY=<YYYY-MM>
```

## Limits worth knowing

* The gateway prices a tool per call from its registration, not from the call arguments. That is why the merchant fixes the price at registration and the permit cap is compared against it.
* The gateway charges before the tool body runs and refunds if the tool raises. If the process dies after PayPal captures but before the receipt is stored, the next start marks that key `delivery_uncertain` and keeps the charge; a human settles it. If the tool body ever runs again under the same key, it sends the same `PayPal-Request-Id`, so PayPal replays the capture instead of taking a second one.
* The signing key is generated per run and lives only in memory, so receipts from an earlier run verify only against the `trust-keys.json` saved from that run.
* SQLite is used from one event loop with one connection. That is right for an in-process demo and wrong for a multi process server.
* `GovernedToolRunner` stores operation records in memory in this demo. A real agent that must survive a crash should pass `JsonFileOperationKeyStore` or its own store.

## License

Everything in this repo is MIT.

* This project, including `mini_gateway/`, is MIT (see `LICENSE`).
* `vendor/b2a_sdk` and `vendor/openai-agent-middleware` are AMW's MIT client SDKs, copied unmodified with their own `LICENSE` files. Copyright stays with their authors.
* The AMW gateway core is not included, not fetched and not required. It is source available under the Business Source License; see the AMW repository if you want it.
