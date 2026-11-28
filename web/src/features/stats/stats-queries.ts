import { queryOptions } from "@tanstack/react-query";
import { request } from "@/api/client";
import type { components, paths } from "@/api/schema";

type StatsQuery = NonNullable<
	paths["/api/v1/stats"]["get"]["parameters"]["query"]
>;
export type StatsPeriod = NonNullable<StatsQuery["range"]>;
export type Stats = components["schemas"]["StatsResponse"];

export const statsKey = ["review-stats"] as const;

export function statsQuery(period: StatsPeriod = "30d", repository?: string) {
	return queryOptions({
		queryKey: [...statsKey, period, repository] as const,
		queryFn: ({ signal }) =>
			request("get", "/api/v1/stats", {
				query: { range: period, repository },
				signal,
			}),
		staleTime: 0,
		refetchOnMount: true,
		refetchOnWindowFocus: true,
	});
}
