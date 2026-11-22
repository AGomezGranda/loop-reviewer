import { useQueryClient } from "@tanstack/react-query";
import { useLayoutEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router";
import { inboxQuery } from "@/features/inbox/inbox-queries";
import { draftQuery, type OpenOutcome } from "@/features/review/review-queries";

export function useRouteNavigation() {
	const navigate = useNavigate();
	const { key } = useLocation();
	const currentKey = useRef<string | null>(null);
	useLayoutEffect(() => {
		currentKey.current = key;
		return () => {
			currentKey.current = null;
		};
	}, [key]);

	return (path: string, state?: { notice?: string }) => {
		if (currentKey.current !== key) return false;
		void navigate(path, { state });
		return true;
	};
}

export function useReviewNavigation() {
	const navigate = useRouteNavigation();
	const cache = useQueryClient();

	return (outcome: OpenOutcome): string | undefined => {
		void cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
		switch (outcome.kind) {
			case "draft": {
				const query = draftQuery(outcome.draft.runId);
				void cache.cancelQueries({ queryKey: query.queryKey });
				cache.setQueryData(query.queryKey, outcome.draft);
				const changes = outcome.changes;
				const notice =
					changes === "unavailable"
						? "The current GitHub revision could not be verified after review."
						: changes.length
							? `The pull request changed during review (${changes.join(", ")}).`
							: undefined;
				if (!navigate(`/drafts/${outcome.draft.runId}`, { notice }))
					return "The draft is ready. Open the same pull request to resume it.";
				return;
			}
			case "completion":
				if (!navigate(`/history/${outcome.completion.runId}`))
					return "The completed review is available in History.";
				return;
			case "failure":
				return outcome.reason === "setup"
					? `${outcome.message} Check your Pi setup before retrying.`
					: outcome.message;
			case "unavailable":
				switch (outcome.reason) {
					case "abandoned":
						return "The previous review was interrupted. Retry using the pull request URL.";
					case "in_progress":
						return "A review is in progress. Open the same pull request again to resume.";
					case "no_retryable_run":
						return "Open this pull request to start a review.";
				}
		}
	};
}
