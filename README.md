# amw-paypal-agent-demo

An OpenAI agent buys something through PayPal sandbox, and Agent Middleware (AMW) stands between the agent and the money.

The agent gets a permit capped at the amount the user allowed. AMW checks that permit before the PayPal tool runs, charges the wallet once per idempotency key, and signs a receipt that anyone can verify offline. If the response is lost after PayPal captured the payment, the agent retries the same tool call and gets the same receipt back, with at most one PayPal capture.

## What the demo shows

`demo.py` runs four scenes against the real AMW gateway code, pinned to commit `b082579d`, running in this process.

1. **Happy path.** Permit capped at 25.00 USD, one PayPal order created and captured, one signed receipt.
2. **Lost response, then retry.** AMW charges, PayPal captures, AMW writes the receipt, and then the response times out before it reaches the agent. The agent retries the same tool call. It gets the receipt from the lost response, PayPal shows one capture, and the wallet shows one debit.
3. **Over budget.** The user allows 12.50 USD and the price is 25.00 USD. AMW answers `permit_budget_exceeded` before the tool runs, so no PayPal request is sent.
4. **Offline receipt verification.** The receipt from scene 1 and the published key set are written to `out/`, the gateway shuts down, and the Ed25519 signature is checked from those files alone. An edited copy is rejected.

## How it fits together

* `paypal_client.py` does sandbox OAuth, `POST /v2/checkout/orders` and `POST /v2/checkout/orders/{id}/capture`. Every write sends `PayPal-Request-Id`, a SHA256 hash of the AMW idempotency key (with the operation name mixed in, so create and capture never share an id). Credentials come only from `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` and are never logged.
* `paypal_tool.py` registers `paypal_checkout` as an AMW tool: b2a_sdk `@mcp_tool` metadata plus the gateway's `service_registry.register_local(..., require_permit=True)`. The merchant sets the price at registration. One AMW credit is one US cent, so AMW charges and caps the real order amount.
* `agent.py` is the LLM agent. It uses AMW's OpenAI wrapper (`GovernedToolRunner` from `wrappers/openai-agent-middleware`), which creates the permit with `max_credits` equal to the user's limit and derives the idempotency key from the model's `tool_call.id`. The model only chooses the item description; it can not set the price or the key.
* `gateway.py` boots the pinned AMW app on SQLite, generates a fresh admin key and signing key for each run, and creates wallets through AMW's own HTTP routes.
* `fakes.py` holds the offline PayPal stand in, a scripted LLM, and the transport that loses a response.

## Setup

Python 3.11 or newer and git. These commands use `uv`; plain `pip` works the same way.

```bash
uv venv --python 3.12 .venv
scripts/fetch_amw.sh
uv pip install --python .venv/bin/python -r .amw/agent-middleware-api/requirements.txt -e ".[dev]"
```

Why the extra fetch step: at commit `b082579d` the AMW repository root has no build system and no package list, so `pip install git+https://github.com/agentmiddleware/agent-middleware-api@b082579d` fails. The two installable AMW packages (`b2a_sdk` and `wrappers/openai-agent-middleware`) are pinned in `pyproject.toml` by subdirectory at that same commit. The gateway server package (`app/`) is fetched at the same commit into `.amw/`, which git ignores.

### Using a local AMW checkout instead

If you already have AMW checked out (for example to test a change to it), point the demo at it and install its packages in editable mode:

```bash
export AMW_SOURCE_DIR=/path/to/agent-middleware-api
uv pip install --python .venv/bin/python -r "$AMW_SOURCE_DIR/requirements.txt" \
  -e "$AMW_SOURCE_DIR/b2a_sdk[verify]" \
  -e "$AMW_SOURCE_DIR/wrappers/openai-agent-middleware" \
  openai "httpx>=0.28.1,<0.29" pytest pytest-asyncio
uv pip install --python .venv/bin/python --no-deps -e .
```

The `--no-deps` on the last line stops the pinned git dependencies from replacing your editable copies.

## Run

Fully offline (no credentials needed):

```bash
.venv/bin/python demo.py
```

With real services, fill in `.env` (copy it from `.env.example`) and load it first:

```bash
set -a; . ./.env; set +a
.venv/bin/python demo.py
```

The demo uses the PayPal sandbox when both PayPal variables are set, and OpenAI when `OPENAI_API_KEY` is set. Either one can be forced offline with `--mock-paypal` or `--mock-llm`. Add `--json` for a machine readable summary.

Verify a receipt yourself with the AMW SDK command line tool:

```bash
.venv/bin/b2a-verify-receipt --bundle out/<receipt id>.json --keys out/trust-keys.json
```

Tests (always offline; a guard fails any test that opens a network connection):

```bash
.venv/bin/python -m pytest
```

Docker:

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

* AMW prices a local tool per call from its registration, not from the call arguments. That is why the price is fixed by the merchant at registration time and the permit cap is compared against it.
* The gateway charges the wallet before the tool body runs and refunds if the tool raises. If the process dies after PayPal captures but before AMW stores the receipt, AMW's own documentation (`docs/failure-semantics.md` in AMW) sends that operation to manual review. If the tool body ever runs again under the same key, it sends the same `PayPal-Request-Id`, so PayPal replays the capture instead of taking a second one.
* `GovernedToolRunner` stores operation records in memory in this demo. A real agent that must survive a crash should pass `JsonFileOperationKeyStore` or its own store.

## License

This project is MIT licensed (see `LICENSE`). AMW's `b2a_sdk` and `wrappers/` packages are MIT. AMW's gateway server code (`app/`), which this demo fetches and runs but does not include, is under the Business Source License 1.1; see AMW's `LICENSING.md` for what that allows.
