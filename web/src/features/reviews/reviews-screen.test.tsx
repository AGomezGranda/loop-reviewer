// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import type { components } from "@/api/schema";
import { reviewsQuery } from "./reviews-queries";
import { ReviewsScreen } from "./reviews-screen";

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

const url = (id: string) => `https://github.com/org/repo/pull/${id}`;
const selection = (id: string) => ({
	url: url(id),
	account: "alice",
	author: "bob",
	targetBranch: "main",
	baseTip: "a".repeat(40),
	mergeBase: "b".repeat(40),
	head: "c".repeat(40),
});
type Saved = components["schemas"]["SavedReviewRow"];

function saved(runId: string, status: Saved["status"]): Saved {
	return {
		kind: "saved",
		runId,
		status,
		selection: selection(runId),
		failure: null,
		failureKind: null,
		publicationId: null,
		changes: [],
		previousDraft: false,
		retryAction: null,
	};
}

test.each(["Retry review", "Re-run analysis"])(
	"polls and submits %s for the selected attempt",
	async (action) => {
		const rerun = action === "Re-run analysis";
		sessionStorage.setItem("loop-reviewer.capability", "test");
		let rows: Array<Saved | components["schemas"]["LaunchReviewRow"]> = [];
		const fetch = vi.fn(async (path: string, options: RequestInit) => {
			if (options.method === "POST") {
				expect(path).toBe("/api/v1/reviews/4/retry");
				rows = [saved("4", "running")];
				return Response.json({ launchId: "retry" }, { status: 202 });
			}
			return Response.json(rows);
		});
		vi.stubGlobal("fetch", fetch);
		const client = new QueryClient({
			defaultOptions: { queries: { retry: false } },
		});
		render(
			<QueryClientProvider client={client}>
				<ReviewsScreen
					draftLink={(id) => <a href={`/drafts/${id}`}>Open draft</a>}
					recoveryLink={(id) => (
						<a href={`/publications/${id}`}>Recover publication</a>
					)}
					historyLink={(id) => (
						<a href={`/history/${id}`}>Open completed review</a>
					)}
					onRetried={() => {
						void client.invalidateQueries({ queryKey: reviewsQuery.queryKey });
					}}
				/>
			</QueryClientProvider>,
		);
		await screen.findByText("No reviews to show.");
		rows = [
			{
				kind: "launch",
				launchId: "queued",
				source: "url",
				url: url("1"),
				status: "queued",
				selection: null,
				error: null,
				completedRunId: null,
			},
			saved("2", "running"),
			{ ...saved("3", "draft_ready"), changes: ["head"] },
			{
				...saved("4", rerun ? "draft_ready" : "interrupted"),
				retryAction: rerun ? "rerun" : "retry",
			},
			{ ...saved("9", "draft_ready"), previousDraft: true },
			{
				...saved("5", "failed"),
				failure: "Set LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS.",
			},
			{ ...saved("6", "recovery_required"), publicationId: "recovery" },
			{
				kind: "launch",
				launchId: "error",
				source: "inbox",
				url: url("7"),
				status: "failed",
				selection: null,
				error: {
					code: "github_unavailable",
					message: "Could not read GitHub.",
				},
				completedRunId: null,
			},
			{
				kind: "launch",
				launchId: "done",
				source: "url",
				url: url("8"),
				status: "completed",
				selection: null,
				error: null,
				completedRunId: "8",
			},
		];
		await screen.findByText("Queued", {}, { timeout: 3000 });
		await screen.findByText("Running");
		await screen.findAllByText("Draft ready");
		const previous = screen.getByText("Previous draft").closest("li");
		if (!previous) throw new Error("Previous draft missing");
		expect(
			within(previous)
				.getByRole("link", { name: "Open draft" })
				.getAttribute("href"),
		).toBe("/drafts/9");
		expect(within(previous).queryByRole("button")).toBeNull();
		await screen.findByText("The pull request changed during review (head).");
		await screen.findByText("Set LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS.");
		await screen.findByText("Could not read GitHub.");
		await screen.findByText(
			"Choose the current row in the Inbox to launch again.",
		);
		const draft = screen.getByRole("link", { name: url("3") }).closest("li");
		if (!draft) throw new Error("Draft missing");
		expect(
			within(draft)
				.getByRole("link", { name: "Open draft" })
				.getAttribute("href"),
		).toBe("/drafts/3");
		expect(
			screen
				.getByRole("link", { name: "Recover publication" })
				.getAttribute("href"),
		).toBe("/publications/recovery");
		expect(
			screen
				.getByRole("link", { name: "Open completed review" })
				.getAttribute("href"),
		).toBe("/history/8");
		const target = screen.getByRole("link", { name: url("4") }).closest("li");
		if (!target) throw new Error("Retry target missing");
		await userEvent.click(within(target).getByRole("button", { name: action }));
		await screen.findByText("Running");
		expect(screen.queryByText("Interrupted")).toBeNull();
	},
);
