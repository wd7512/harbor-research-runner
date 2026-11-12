# How to add a model

1. **Choose the model card path and matching identifier.** Add a JSON card under `src/harbor_research_runner/job_config/model_cards/<provider>/<model>.json`. Its `id` must match the job file's `model` value and its path must match that identifier. For an identifier containing `:`, the filename encodes the colon as `__colon__`.

2. **Fill the required fields.**

| Field | Meaning |
|---|---|
| `id` | Model card identifier used by the job. |
| `upstream_model` | Exact model identifier sent upstream and expected in the response. |
| `api` | Upstream API shape: `chat` or `responses`. |
| `endpoints` | List of upstream base URLs. |
| `key_env` | Environment variable names that hold credentials. Never put credential values in the card. |
| `tokens_per_minute` | Token pacing limit, or `null` if no limit is declared. |
| `pricing` | Per-million-token prices with exactly `input`, `output`, and `cached_read` numeric entries. |
| `variants` | Mapping from nonempty variant IDs to nonempty settings objects. |
| `withdrawn` | Boolean; a withdrawn card cannot be used to launch a run. |
| `reason` | Withdrawal explanation, or `null`. |

3. **Add optional settings when needed.**

- `default_variant`: a declared variant ID or `null`. If omitted, it defaults to `null`.
- `request`: request shaping settings. `drop_message_fields` is a list of nonempty strings; `max_tokens_cap` is a positive integer or `null`.

4. **Check the card shape and keep credentials out of it.** For example:

```json
{
  "id": "provider/model-name",
  "upstream_model": "model-name",
  "api": "chat",
  "endpoints": ["https://provider.example/v1"],
  "key_env": ["<YOUR_KEY_VARIABLE>"],
  "tokens_per_minute": null,
  "pricing": {"input": 1.0, "output": 2.0, "cached_read": 0.1},
  "variants": {"standard": {"reasoningEffort": "medium"}},
  "default_variant": "standard",
  "withdrawn": false,
  "reason": null,
  "request": {"drop_message_fields": [], "max_tokens_cap": null}
}
```

   The card pins the API, endpoints, model name, key variable names, pricing, pacing, and variant settings used by the proxy and harness. Secrets come from the environment and are never written to files.
