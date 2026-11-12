_MAX_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def shape_request(body: dict, card: dict) -> dict:
    """Apply the request transformations declared by a model card."""
    shaped = dict(body)
    request = card.get("request")
    if not isinstance(request, dict):
        return shaped

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
