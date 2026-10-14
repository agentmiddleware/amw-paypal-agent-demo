# REPORT: amw-paypal-agent-demo

## Update 2026-10-14: no private dependency

The AMW pin (`b082579d`, later `bc5b671f`) is gone. The demo now runs on `mini_gateway/`, a small MIT reference gateway written clean room for this demo from the vendored MIT SDKs' HTTP contract. The two SDKs are vendored unmodified under `vendor/` and install from there (`pip install -r requirements.txt`); nothing is fetched from GitHub, and `scripts/fetch_amw.sh` and `AMW_SOURCE_DIR` are removed. The original 18 tests pass against it with their assertions unchanged, 11 new gateway tests were added (29 total), and the offline demo passes all four scenes. Findings below that cite the AMW gateway describe the earlier pinned build.

## Summary

The project is built, tested and committed locally on `main` (no remote, nothing pushed).

An OpenAI tool calling agent buys an item through PayPal sandbox, with a governed gateway in front of the payment (originally AMW pinned to `b082579d`, now the in-repo `mini_gateway`; see the update above). The agent gets a permit capped at the user's limit, AMW checks it before the PayPal tool runs, charges once per idempotency key, and signs a receipt that verifies offline.

`demo.py` runs four scenes. All pass offline with the PayPal fake and the scripted LLM:

1. Happy path.
2. Lost response then retry. Same receipt id, 1 PayPal capture, 1 wallet debit.
3. Over budget. Denied with `permit_budget_exceeded`, 0 PayPal requests.
4. Offline receipt verification. Signature verified from files after the gateway stopped; an edited copy is rejected.

18 pytest tests pass offline. A socket guard fails any test that opens a network connection.

Not run:

* Live PayPal sandbox, live OpenAI and Docker. No credentials were used, and Docker is not installed on this machine.

## Findings by severity

### High

**H1. Resolved by removal: the AMW repository root at `b082579d` is not pip installable.** The pin no longer exists; see the update at the top.

* `pip install git+https://github.com/agentmiddleware/agent-middleware-api@b082579d` fails with a setuptools "Multiple top-level packages discovered in a flat-layout" error. The root `pyproject.toml` has no `[build-system]` and no package list. I verified this with uv against GitHub.
* What I did instead:
  * `pyproject.toml` pins the two installable AMW packages at that same commit, by subdirectory: `b2a_sdk` (with the `verify` extra) and `wrappers/openai-agent-middleware`.
  * The gateway server package (`app/`) is fetched at the same commit by `scripts/fetch_amw.sh` into `.amw/`, which git ignores. `gateway.py` loads it from there, or from `AMW_SOURCE_DIR`.
* The local editable install option is documented in the README. I tested it in a throwaway venv: imports resolved to the local checkout, and 11 tests passed (the suite size at that point).

**H2. Fixed: AMW refunded credits while PayPal had captured.**

* The independent review found this. If both capture responses were lost, the tool raised. AMW then refunds the charge (`app/routers/mcp.py` around line 2816) even though money moved.
* Fix: after an ambiguous capture the tool reads the order back (`GET /v2/checkout/orders/{id}`) and returns success when PayPal says `COMPLETED`.
* Test: `test_capture_with_lost_responses_is_looked_up_not_refunded`. A mutation check showed this test fails without the fix. The first version of the test did not fail, because the fake returned replays before dropping them; I corrected the fake.

### Medium

**M1. AMW prices local tools per call from the registration, not from the arguments.**

* So "permit capped at the amount" works like this: the merchant registers `paypal_checkout` at a fixed price (`CHECKOUT_PRICE_USD`, 1 credit = 1 US cent), and the permit `max_credits` is the user's limit in cents.
* AMW's own `permit_budget_exceeded` check then blocks over budget calls before the tool body runs.
* A checkout whose price changes per call would need re-registering the tool or AMW's quote and action permit features. I did not build that.

**M2. Live PayPal capture needs an approved order.**

* A server side agent has no buyer to click Approve. I expect PayPal to refuse the capture with `ORDER_NOT_APPROVED` (guess, from PayPal docs, not run live).
* Optional `PAYPAL_SANDBOX_CARD_NUMBER` and `PAYPAL_SANDBOX_CARD_EXPIRY` add a sandbox test card as the payment source, so no approval step is needed.
* This card path is **not verified** against the live sandbox. A rejected capture is tested offline: the agent reports `failed` and AMW refunds the credits.

**M3. The operation store is in memory only.**

* `GovernedToolRunner` keeps its operation records in memory here. If the agent process crashes and the model is asked again, the new tool call id means a new key.
* What saves the demo today: a rerun with the same `run_id` conflicts on permit creation. That is a side effect, not a design.
* The README tells real deployments to pass `JsonFileOperationKeyStore`.

**M4. The PayPal request id hashes the operation name with the key.**

* The spec said `PayPal-Request-Id` = hash of the AMW key. I used `sha256("<operation>:<amw key>")` so create order and capture never share an id; one id for both could make PayPal answer the capture with the cached create response.
* It is still a pure hash of the AMW key, but it is a deliberate deviation from the spec.

### Low

* **L1. Fixed:**
  * the agent API key appeared in the `AgentCredentials` repr;
  * `PAYPAL_BASE_URL` could point credentials at any host (now https `*.paypal.com` only);
  * a card number without an expiry was accepted;
  * the demo checked `<= 1` captures where it meant `== 1`;
  * the capture status itself was not checked.
* **L2. Retry timeout only matters with a remote gateway.** `B2AClient` defaults to a 10 second timeout, and a retry while the first call is still running gets `idempotency_in_progress`. The demo always runs the gateway in process, where this timeout does not fire. A remote gateway setup should raise the timeout and retry on that error.
* **L3. Two documentation nits.**
  * AMW's README for the OpenAI wrapper says to `git clone https://github.com/PetrefiedThunder/agent-middleware-api.git`, while the pin uses `agentmiddleware/agent-middleware-api`. Both may resolve to the same repository; not verified.
  * The default model `gpt-4.1-mini` is a guess at a widely available model; set `OPENAI_MODEL` to override.
* **L4. Licensing.** AMW's gateway code (`app/`) is BUSL 1.1. This project is MIT and does not include that code; it fetches it at setup time. The README says so.
* **L5. Instruction file conflicts.**
  * The global instructions ask for a Muse review. That command was denied in this session, so it was skipped. A reviewer subagent did the independent review instead.
  * In this shell, `muse` is a function that silently adds `--yolo` to `exec`, which conflicts with the global rule against `--yolo`.
  * `~/AGENTS.md` points at stale paths (`~/Documents/GitHub/...`), as `~/metacode/AGENTS.md` already notes.

## Test output

Command: `.venv/bin/python -m pytest -v` (Python 3.12.13, offline, PayPal and OpenAI mocked, real AMW gateway in process)

```
tests/test_governed_checkout.py::test_happy_path_charges_the_price_and_captures_once PASSED
tests/test_governed_checkout.py::test_lost_response_retry_returns_same_receipt_and_one_capture_per_key PASSED
tests/test_governed_checkout.py::test_over_budget_is_denied_before_paypal PASSED
tests/test_governed_checkout.py::test_cross_wallet_call_is_refused_before_paypal PASSED
tests/test_governed_checkout.py::test_capture_with_lost_responses_is_looked_up_not_refunded PASSED
tests/test_governed_checkout.py::test_rejected_capture_fails_and_is_refunded PASSED
tests/test_governed_checkout.py::test_model_cannot_choose_the_paypal_request_id PASSED
tests/test_governed_checkout.py::test_receipt_verifies_offline_and_edits_are_rejected PASSED
tests/test_paypal_client.py::test_request_id_is_a_stable_hash_of_the_amw_key PASSED
tests/test_paypal_client.py::test_blank_key_is_refused PASSED
tests/test_paypal_client.py::test_missing_credentials_refused PASSED
tests/test_paypal_client.py::test_credentials_only_sent_to_https_paypal_hosts[http://api-m.sandbox.paypal.com] PASSED
tests/test_paypal_client.py::test_credentials_only_sent_to_https_paypal_hosts[https://paypal.com.attacker.example] PASSED
tests/test_paypal_client.py::test_credentials_only_sent_to_https_paypal_hosts[https://example.com] PASSED
tests/test_paypal_client.py::test_sandbox_default_host_accepted PASSED
tests/test_paypal_client.py::test_card_number_without_expiry_refused PASSED
tests/test_paypal_client.py::test_secret_not_exposed_in_repr_or_errors PASSED
tests/test_paypal_client.py::test_capture_timeout_retry_reuses_request_id_and_captures_once PASSED
============================== 18 passed in 1.17s ==============================
```

Mutation checks, run by hand and then reverted:

* Agent sends a fresh tool call id on each retry: the lost response test fails. The permit cap alone denied the second charge, so even that bug did not double charge.
* Tool re-raises instead of looking up the order: the ambiguous capture test fails.

Other checks:

* `demo.py`, offline: exit 0, all four scenes pass.
* `b2a-verify-receipt --bundle out/<receipt>.json --keys out/trust-keys.json`: `VERIFIED`, exit 0.
* `gitleaks protect --staged`: no leaks found.
* `trufflehog filesystem` on the staged files: no verified or unknown results.

## Open questions

1. Is the sandbox card payment source acceptable for judging, or should live mode instead print an approval link and wait for a human to approve the order? Either way, someone has to run it once with real sandbox credentials.
2. Should the checkout price be fixed per merchant registration (current design), or does the demo need a price set per call? Per call pricing needs AMW quotes or action permits and is a bigger change.
3. Which OpenAI model should be the default? `gpt-4.1-mini` is a guess.
4. Resolved: there is no pin any more.
5. Docker was not run (Docker is not installed here). Please run `docker compose up --build demo` on a machine that has it.
