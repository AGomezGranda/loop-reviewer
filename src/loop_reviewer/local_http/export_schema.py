"""Export the schema without creating stores or opening external sessions."""

from json import dumps
from typing import Never, cast

from loop_reviewer.inbox.actions import InboxActions
from loop_reviewer.inbox.discovery import ConfigureInbox
from loop_reviewer.local_http.app import create_app
from loop_reviewer.review.drafts import EditReviewDraft
from loop_reviewer.review.generate import ReviewOutcome
from loop_reviewer.review.history import ReviewHistory
from loop_reviewer.review.publication import PreparePublication, PublishReview


def _offline(*_args: object) -> Never:
    raise RuntimeError("Schema export does not run workflows.")


def main() -> None:
    # Route registration captures these collaborators without accessing them.
    app = create_app(
        origin="http://127.0.0.1",
        capability="",
        read_inbox=_offline,
        scan_inbox=_offline,
        configure_inbox=cast("ConfigureInbox", None),
        inbox_actions=cast("InboxActions[ReviewOutcome]", None),
        review_pull_request=_offline,
        drafts=cast("EditReviewDraft", None),
        review_history=cast("ReviewHistory", None),
        prepare=cast("PreparePublication", None),
        publish=cast("PublishReview", None),
        join_watch=_offline,
        watch_tick=_offline,
        watch_cycle=_offline,
    )
    print(dumps(app.openapi(), sort_keys=True))


if __name__ == "__main__":
    main()
