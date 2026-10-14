"""Immutable generation defaults at the ordinary OpenAI request boundary."""
from .content import require


def validate_request(body, frozen):
    require(isinstance(body, dict), "invalid-request", "OpenAI request must be an object")
    generation, protocol = frozen["generation"], frozen["protocol"]
    chat = protocol["api"] == "chat-completions"
    if chat:
        require(isinstance(body.get("messages"), list) and body["messages"]
                and all(isinstance(m, dict) and m.get("role") in ("system", "developer", "user", "assistant", "tool") for m in body["messages"]),
                "invalid-messages", "complete chat messages are required")
        require(body.get("n", 1) == 1, "inference-parameter-conflict", "one completion is required per request")
    for key in ("temperature", "top_p", "top_k", "min_p", "repetition_penalty", "seed"):
        wanted = generation[key]
        actual = body.get(key, wanted)
        equivalent = key == "top_k" and wanted == 0 and actual == -1
        require(actual == wanted or equivalent, "inference-parameter-conflict", "request conflicts with locked " + key)
        body[key] = -1 if key == "top_k" and wanted == 0 else wanted
    require(body.get("parallel_tool_calls", protocol["parallel_tool_calls"]) == protocol["parallel_tool_calls"],
            "inference-parameter-conflict", "parallel tool calls conflict with the lock")
    tools = body.get("tools", [])
    require(isinstance(tools, list) and (not tools or protocol["tool_calls"]), "unsupported-tools", "tools are unavailable for this protocol")
    require(not chat or all(isinstance(tool, dict) and tool.get("type") == "function" for tool in tools),
            "unsupported-tools", "chat accepts function tools")
    require("stream" not in body or type(body["stream"]) is bool, "invalid-stream", "stream must be boolean")
    require(not body.get("stream") or protocol["streaming"], "unsupported-stream", "streaming is unavailable")
    require(body.get("store", False) is False, "inference-parameter-conflict", "store must be false")
    if chat:
        require(not ("max_tokens" in body and "max_completion_tokens" in body), "invalid-token-limit", "use one output token limit")
        maximum = body.get("max_tokens", body.pop("max_completion_tokens", generation["max_output_tokens"]))
        require(type(maximum) is int and 1 <= maximum <= generation["max_output_tokens"], "invalid-token-limit", "output token limit exceeds the lock")
        body["max_tokens"] = maximum
    body["parallel_tool_calls"] = protocol["parallel_tool_calls"]
    body["store"] = False
    return body
