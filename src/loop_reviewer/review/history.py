"""Read immutable completions and saved publication attempts offline."""

from loop_reviewer.review.model import CompletedReview, require_run_id
from loop_reviewer.review.publication_model import PublicationId, PublicationRecord
from loop_reviewer.review.store import CompletionHistoryReader, PublicationHistoryReader


class ReviewHistory:
    def __init__(
        self,
        completions: CompletionHistoryReader,
        publications: PublicationHistoryReader,
    ) -> None:
        self._completions = completions
        self._publications = publications

    def completions(
        self, before_run_id: int | None, limit: int
    ) -> tuple[CompletedReview, ...]:
        if before_run_id is not None:
            require_run_id(before_run_id)
        if limit < 1:
            raise ValueError("History page size must be positive.")
        return self._completions.completions(before_run_id, min(limit, 50))

    def completion(self, run_id: int) -> CompletedReview | None:
        require_run_id(run_id)
        return self._completions.completion(run_id)

    def publication(self, id: PublicationId) -> PublicationRecord | None:
        return self._publications.get_publication(id)

    def unconfirmed_publications(self) -> tuple[PublicationRecord, ...]:
        return self._publications.unconfirmed_publications()

    def unconfirmed_for(self, run_id: int) -> PublicationRecord | None:
        require_run_id(run_id)
        return self._publications.unconfirmed_for(run_id)
