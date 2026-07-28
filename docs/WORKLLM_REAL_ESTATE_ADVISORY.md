# WorkLLM real-estate advisory lane

PropertyQuarry uses WorkLLM as a bounded second opinion, not as an autonomous real-estate operator.

## Product flow

1. PropertyQuarry creates a structured property packet from listing facts, search preferences, and evidence references.
2. The WorkLLM adapter removes credential, contact, account, and principal fields and enforces a 64 KiB input limit.
3. The configured real-estate agent receives the packet as untrusted evidence. WorkLLM web search and organization-memory writes are both disabled.
4. The agent returns an advisory packet containing a summary, fit and risk signals, market questions, evidence used, unknowns, confidence, and recommended human checks.
5. PropertyQuarry stores the result as review-required evidence with input/output hashes and provider/thread identifiers.

The lane cannot contact a broker or owner, schedule a viewing, submit an offer, alter a listing, provision an agent, or accept WorkLLM memory suggestions.

## Agent choice

Use the **Listing & Client Context Agent** as the primary organization agent because its scope matches a single property packet plus buyer/search preferences. A **Market Knowledge Agent** can later be added as an independent comparison lane, but its market claims must carry inspectable sources before PropertyQuarry treats them as evidence. The Practice Knowledge Agent is useful for internal playbooks, not for the first property-specific integration.

## Activation gates

The provider route remains unavailable unless all of these conditions are true:

- `WORKLLM_BASE_URL`, `WORKLLM_EMAIL`, and `WORKLLM_PASSWORD` are configured.
- A real-estate template has been provisioned and its organization agent ID is stored as `WORKLLM_REAL_ESTATE_AGENT_ID`.
- Read-only live verification confirms access to the real-estate agent category.
- `WORKLLM_PROVIDER_VERIFIED=true` and `WORKLLM_RUNTIME_ENABLED=true`.

Run the verifier without invoking an agent:

```bash
python3 scripts/verify_workllm_provider.py --env-file /path/to/ea.env --live
```

The verifier authenticates, lists prompt tools, and checks the real-estate template category. It never provisions or runs an agent and never writes organization memory.

## Execution and receipts

The executable tool is `provider.workllm.real_estate_advisory`. It is quota-consuming and defaults to required approval. Every successful invocation produces a `propertyquarry.workllm_real_estate_advisory.v1` receipt with:

- workspace host and a one-way account hash;
- configured agent, thread, and message identifiers;
- input and output SHA-256 digests;
- explicit `memory_mode=OFF` and `web_search_mode=OFF`;
- explicit review and prohibited-action flags.

Credentials, cookies, full login responses, and unredacted contact data are never written to the receipt.
