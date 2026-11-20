_MAX_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def request_keys_to_drop(body: dict, card: dict) -> list[str]:
    """Return the card-declared top-level keys present in a request body."""
    request = card.get("request")
    if not isinstance(request, dict):
        return []
    keys = request.get("drop_request_keys", [])
    if not isinstance(keys, list):
        return []
    return [key for key in keys if isinstance(key, str) and key in body]


def shape_request(body: dict, card: dict) -> dict:
    """Apply the request transformations declared by a model card."""
    shaped = dict(body)
    request = card.get("request")
    if not isinstance(request, dict):
        return shaped

    for key in request_keys_to_drop(shaped, card):
        del shaped[key]

    cap = request.get("max_tokens_cap")
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        for key in _MAX_TOKEN_FIELDS:
            value = shaped.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > cap:
                shaped[key] = cap

    drop_fields = request.get("drop_message_fields", [])
    messages = shaped.get("messages")
    if isinstance(drop_fields, list) and drop_fields and isinstance(messages, list):
        shaped["messages"] = [
            {key: value for key, value in message.items() if key not in drop_fields}
            if isinstance(message, dict) and any(key in message for key in drop_fields)
            else message
            for message in messages
        ]
    return shaped
