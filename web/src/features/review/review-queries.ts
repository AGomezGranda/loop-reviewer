import { queryOptions } from "@tanstack/react-query";
import { request } from "@/api/client";
import type { components } from "@/api/schema";

type Schemas = components["schemas"];
export type Draft = Schemas["DraftResponse"];
export type Edit =
	| Schemas["EditSummary"]
	| Schemas["EditFinding"]
	| Schemas["DropFinding"];

export const draftKey = ["draft"] as const;

export const draftQuery = (runId: string) =>
	queryOptions({
		queryKey: [...draftKey, runId],
		queryFn: ({ signal }) =>
			request("get", "/api/v1/drafts/{runId}", { params: { runId }, signal }),
	});

export const saveDraft = (runId: string, body: Edit) =>
	request("patch", "/api/v1/drafts/{runId}", { params: { runId }, body });

export const finishDraft = (runId: string, version: string) =>
	request("post", "/api/v1/drafts/{runId}/finish", {
		params: { runId },
		body: { version },
	});

export const copyDraft = (runId: string) =>
	request("get", "/api/v1/drafts/{runId}/copy", { params: { runId } });

export const launchReview = (url: string) =>
	request("post", "/api/v1/reviews/launches", { body: { url } });
