import { queryOptions } from "@tanstack/react-query";
import { request } from "@/api/client";
import type { components } from "@/api/schema";

export type InboxReport = components["schemas"]["ConfiguredInbox"];
export type InboxRowData = components["schemas"]["InboxRowResponse"];
export type InboxResult =
	| InboxReport
	| components["schemas"]["UnconfiguredInbox"];
export type RowAction = components["schemas"]["RowAction"];

export const inboxQuery = queryOptions({
	queryKey: ["inbox"],
	queryFn: ({ signal }) => request("get", "/api/v1/inbox", { signal }),
});

export function scanInbox() {
	return request("post", "/api/v1/inbox/scan", {});
}

export function moveRow(body: RowAction, later: boolean) {
	return later
		? request("post", "/api/v1/inbox/later", { body })
		: request("post", "/api/v1/inbox/restore", { body });
}

export function launchRow(body: RowAction) {
	return request("post", "/api/v1/inbox/launch", { body });
}
