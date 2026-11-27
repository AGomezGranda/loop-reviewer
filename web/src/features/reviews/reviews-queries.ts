import { queryOptions } from "@tanstack/react-query";
import { request } from "@/api/client";
import type { components } from "@/api/schema";

export const reviewsQuery = queryOptions({
	queryKey: ["reviews"],
	queryFn: ({ signal }) => request("get", "/api/v1/reviews", { signal }),
	refetchInterval: 2000,
	refetchOnWindowFocus: "always",
});

export function retryReview(runId: string) {
	return request("post", "/api/v1/reviews/{runId}/retry", {
		params: { runId },
	});
}

export function revisionNotice(
	changes: components["schemas"]["SavedReviewRow"]["changes"],
) {
	if (changes === "unavailable")
		return "The current GitHub revision could not be verified after review.";
	if (changes.length)
		return `The pull request changed during review (${changes.join(", ")}).`;
}
