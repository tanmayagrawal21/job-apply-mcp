"""Importing this package registers every built-in provider. Heavy deps are imported lazily inside them."""
from . import anthropic, client, fake, hf_local, litellm, ollama, openai_compat  # noqa: F401
