def _detect_provider(model: str) -> str:
    """Return 'anthropic', 'google', 'openai', or 'mistral' from the model name."""
    name = model.lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("gemini"):
        return "google"
    if name.startswith(("gpt", "o1", "o3", "o4", "o5")):
        return "openai"
    if name.startswith("mistral"):
        return "mistral"
    raise ValueError(f"Unknown judge provider for model: {model!r}")
