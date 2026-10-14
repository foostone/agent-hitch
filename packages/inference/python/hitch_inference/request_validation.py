"""Canonical user-unit validation. SGLang conversion belongs only to the engine hop."""
from .content import require


def integer(value):
    return type(value) in (int, float) and value == int(value) and abs(value) <= 9007199254740991

RESPONSE_FIELDS = set("background include input instructions max_output_tokens max_tool_calls metadata model parallel_tool_calls previous_response_id reasoning service_tier store stream temperature tool_choice tools top_logprobs top_p truncation user request_id session_id priority extra_key cache_salt frequency_penalty presence_penalty stop top_k min_p repetition_penalty prompt_cache_key client_metadata".split())
CHAT_FIELDS = set("model messages tools tool_choice parallel_tool_calls temperature top_p top_k min_p repetition_penalty max_tokens max_completion_tokens stream stream_options stop n seed user metadata store frequency_penalty presence_penalty logprobs top_logprobs response_format cache_salt prompt_cache_key".split())


def check(condition, message):
    require(condition, "inference-parameter-conflict", message)


def unsupported_media(value):
    if isinstance(value, list):
        return any(unsupported_media(v) for v in value)
    if isinstance(value, dict):
        return any(k in ("image_url", "input_image", "audio_url", "input_audio") or unsupported_media(v) for k, v in value.items())
    return False


def validate_request(body, frozen):
    check(isinstance(body, dict), "OpenAI request must be an object")
    body = dict(body)
    generation, protocol = frozen["generation"], frozen["protocol"]
    chat = protocol["api"] == "chat-completions"
    check(not (set(body) - (CHAT_FIELDS if chat else RESPONSE_FIELDS)), "unsupported local inference request field")
    require(not unsupported_media(body), "inference-modality-unsupported", "local inference accepts text input only")
    if chat:
        check(isinstance(body.get("messages"), list) and body["messages"]
              and all(isinstance(m, dict) and isinstance(m.get("role"), str) and m.get("role") in ("system", "developer", "user", "assistant", "tool") for m in body["messages"]), "complete chat messages are required")
        check(integer(body.get("n", 1)) and body.get("n", 1) == 1, "one completion is required per request")
        check(body.get("seed", generation["seed"]) == generation["seed"] and integer(body.get("seed", generation["seed"])), "seed conflicts with the lock")
        if "stream_options" in body:
            options = body["stream_options"]
            check(isinstance(options, dict) and set(options) == {"include_usage"} and type(options["include_usage"]) is bool, "invalid chat stream_options")
        body.update(n=1, seed=generation["seed"])
    check("stream" not in body or type(body["stream"]) is bool, "stream must be boolean")
    check(not body.get("stream") or protocol["streaming"], "streaming is unavailable")
    tools = body.get("tools", [])
    check(isinstance(tools, list) and (not tools or protocol["tool_calls"]), "tools are unavailable for this protocol")
    accepted = {"function"} if chat else {"function", "namespace", "tool_search", "custom"}
    check(all(isinstance(tool, dict) and isinstance(tool.get("type"), str) and tool.get("type") in accepted for tool in tools), "unsupported local tool")
    check("parallel_tool_calls" not in body or type(body["parallel_tool_calls"]) is bool and body["parallel_tool_calls"] == protocol["parallel_tool_calls"], "parallel tool calls conflict with the lock")
    check("store" not in body or body["store"] is False, "store must be false")
    check("truncation" not in body or body["truncation"] == "disabled", "automatic truncation is unavailable")
    check(body.get("previous_response_id") is None, "complete request history is required")
    check("background" not in body or body["background"] is False, "background responses are unavailable")
    if body.get("reasoning") is not None:
        check(isinstance(body["reasoning"], dict), "reasoning must be an object")
        check("effort" not in body["reasoning"] or protocol["reasoning_parser"] is not None, "reasoning effort requires a certified parser")
    for key in ("temperature", "top_p", "top_k", "min_p", "repetition_penalty"):
        wanted = generation[key]
        actual = body.get(key, wanted)
        check(type(actual) in (int, float) and actual == wanted, "request conflicts with locked " + key)
        body[key] = wanted
    check(not chat or not ("max_tokens" in body and "max_completion_tokens" in body), "use one output token limit")
    maximum = body.get("max_tokens", body.get("max_completion_tokens")) if chat else body.get("max_output_tokens")
    maximum = generation["max_output_tokens"] if maximum is None else maximum
    check(integer(maximum) and 1 <= maximum <= generation["max_output_tokens"], "output token limit exceeds the lock")
    if chat:
        body["max_tokens"] = int(maximum)
        body.pop("max_completion_tokens", None)
    else:
        body.update(max_output_tokens=int(maximum), truncation="disabled")
    body.update(parallel_tool_calls=protocol["parallel_tool_calls"], store=False)
    if "prompt_cache_key" in body:
        key = body.pop("prompt_cache_key")
        check(isinstance(key, str) and len(key.encode("utf-16-le", errors="surrogatepass")) // 2 <= 256, "invalid prompt_cache_key")
        if frozen["execution"]["prefix_cache"]["mode"] == "radix" and "cache_salt" not in body:
            body["cache_salt"] = key
    if "client_metadata" in body:
        metadata = body.pop("client_metadata")
        check(isinstance(metadata, dict), "invalid client_metadata")
        body.setdefault("metadata", metadata)
    return body


def engine_request(body, frozen):
    """Called by the direct engine relay only, after canonical validation."""
    body = dict(body)
    if body.get("top_k") == 0:
        body["top_k"] = -1
    if frozen["protocol"]["api"] == "responses":
        # Pinned SGLang reserves two tokens internally; user budgets stay unchanged.
        body["max_output_tokens"] += 2
    return body
