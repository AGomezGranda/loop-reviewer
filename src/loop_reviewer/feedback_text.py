"""Validate text shared by reviewer candidates and editable feedback."""

from unicodedata import category


def validate_feedback_text(text: str) -> str:
    if not text.strip():
        raise ValueError("Feedback must not be blank.")
    if any(char not in "\n\t" and category(char) in {"Cc", "Cs"} for char in text):
        raise ValueError(
            "Feedback contains an unsupported control or surrogate character."
        )
    return text
