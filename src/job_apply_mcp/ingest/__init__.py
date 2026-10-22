"""Mail-confirmation dedupe (ADR-0016): read-only ingestion of application-confirmation emails."""
from .confirmations import Confirmation, ConfirmationParser, Message, load_patterns
from .sync import ingest_messages

__all__ = ["Confirmation", "ConfirmationParser", "Message", "ingest_messages", "load_patterns"]
