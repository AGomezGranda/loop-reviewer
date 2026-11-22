import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { request } from "@/api/client";

export const historyKey = ["history"] as const;

export const historyQuery = infiniteQueryOptions({
	queryKey: [...historyKey, "list"],
	queryFn: ({ signal, pageParam }) =>
		request("get", "/api/v1/history", {
			signal,
			query: { before: pageParam ?? undefined },
		}),
	initialPageParam: null as string | null,
	getNextPageParam: (page) => page.nextBefore ?? undefined,
});

export const completionQuery = (runId: string) =>
	queryOptions({
		queryKey: [...historyKey, "completion", runId],
		queryFn: ({ signal }) =>
			request("get", "/api/v1/history/{runId}", { params: { runId }, signal }),
	});
