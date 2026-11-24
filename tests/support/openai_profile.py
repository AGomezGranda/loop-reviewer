"""Historical quote used only with the offline fixture transport."""

from datetime import date

from loop_reviewer.reviewer.integrations.openai_profiles import QualifiedProfile
from loop_reviewer.reviewer.integrations.openai_review import schema_digest
from loop_reviewer.reviewer.model_contract import TokenQuote

PILOT_PROFILE = TokenQuote("gpt-5-nano-2025-08-07", 400_000, 2048, 50_000, 400_000)
FIXTURE_PROFILE = QualifiedProfile(
    PILOT_PROFILE,
    272_000,
    schema_digest("investigation"),
    schema_digest("verification"),
    date(2026, 11, 24),
)
