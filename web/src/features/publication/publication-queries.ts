import { queryOptions } from "@tanstack/react-query";
import { request } from "@/api/client";
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type Preview = Schemas["PreviewResponse"];
export type PreviewOptions = Schemas["PreviewOptions"];
export type PublicationIntent =
	| { kind: "confirm" | "recover" }
	| { kind: "retry"; acceptDuplicateRisk: boolean };

export const publicationQuery = (id: string) =>
	queryOptions({
		queryKey: ["publication", id],
		queryFn: ({ signal }) =>
			request("get", "/api/v1/publications/{id}", { params: { id }, signal }),
	});

export const unconfirmedQuery = queryOptions({
	queryKey: ["publication", "unconfirmed"],
	queryFn: ({ signal }) =>
		request("get", "/api/v1/publications/unconfirmed", { signal }),
});

export const preparePreview = (runId: string, body: PreviewOptions) =>
	request("post", "/api/v1/drafts/{runId}/publication-preview", {
		params: { runId },
		body,
	});

export function operatePublication(id: string, intent: PublicationIntent) {
	const params = { id };
	switch (intent.kind) {
		case "confirm":
			return request("post", "/api/v1/publications/{id}/confirm", { params });
		case "recover":
			return request("post", "/api/v1/publications/{id}/recover", { params });
		case "retry":
			return request("post", "/api/v1/publications/{id}/retry", {
				params,
				body: { acceptDuplicateRisk: intent.acceptDuplicateRisk },
			});
	}
}
