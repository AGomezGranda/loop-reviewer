// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider } from "react-router";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { components } from "@/api/schema";
import { createWorkspaceRouter } from "./routes";

const stats = {
	totals: { completed: 0, published: 0, completedLocally: 0 },
	completedLast7Days: 0,
	undatedCompletions: 0,
	repositoryOptions: [],
	repositories: [],
	weeks: [],
} satisfies components["schemas"]["StatsResponse"];
const report = {
	kind: "report",
	prefix: "[ABC-…]",
	settingsVersion: "1",
	repositories: [],
	active: [],
	later: [],
} satisfies components["schemas"]["ConfiguredInbox"];
const summaryName = "0 reviews completed in the last 7 days";
let router: ReturnType<typeof createWorkspaceRouter> | undefined;

beforeEach(() => {
	window.history.replaceState(null, "", "/inbox");
	sessionStorage.setItem("loop-reviewer.capability", "test");
});
afterEach(() => {
	cleanup();
	router?.dispose();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

function mount({ unconfigured = false, statsError = false } = {}) {
	let failStats = statsError;
	const send = vi.fn(async (path: string, init?: RequestInit) => {
		if (path.startsWith("/api/v1/stats")) {
			return failStats
				? Response.json(
						{ code: "storage", message: "Stats unavailable" },
						{ status: 500 },
					)
				: Response.json(stats);
		}
		if (path === "/api/v1/inbox/watch") {
			return new Promise<Response>((_resolve, reject) => {
				init?.signal?.addEventListener(
					"abort",
					() => reject(init.signal?.reason),
					{ once: true },
				);
			});
		}
		if (path === "/api/v1/inbox/scan" && init?.method === "POST")
			return Response.json(report);
		if (path === "/api/v1/inbox")
			return Response.json(unconfigured ? { kind: "unconfigured" } : report);
		throw new Error(`Unexpected request: ${path}`);
	});
	vi.stubGlobal("fetch", send);
	const cache = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	router = createWorkspaceRouter();
	render(
		<QueryClientProvider client={cache}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
	return {
		send,
		async failRefresh() {
			failStats = true;
			await act(() => cache.refetchQueries({ type: "active" }));
		},
	};
}

test("links a successful zero completion count to Stats even before Inbox setup", async () => {
	mount({ unconfigured: true });
	await screen.findByRole("heading", { name: "Inbox setup needed" });
	const link = await screen.findByRole("link", { name: summaryName });
	expect(link.getAttribute("href")).toBe("/stats");
});

test.each(["initial read", "refresh"])(
	"Stats failure during %s leaves Inbox and scanning usable",
	async (failure) => {
		const { send, failRefresh } = mount({
			statsError: failure === "initial read",
		});
		await screen.findByText("No pull requests are waiting for review.");
		if (failure === "refresh") {
			await screen.findByRole("link", { name: summaryName });
			await failRefresh();
		}
		await userEvent
			.setup()
			.click(screen.getByRole("button", { name: "Scan now" }));
		expect(send).toHaveBeenCalledWith(
			"/api/v1/inbox/scan",
			expect.objectContaining({ method: "POST" }),
		);
		await screen.findByText("No pull requests are waiting for review.");
		expect(
			screen.queryByRole("link", { name: /reviews completed/ }),
		).toBeNull();
		expect(screen.queryByText("Stats unavailable")).toBeNull();
	},
);
