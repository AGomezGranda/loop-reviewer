// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { components } from "@/api/schema";
import { StatsScreen } from "./stats-screen";

const active = {
	key: "github.com/42",
	host: "github.com",
	slug: "org/repo",
	legacyNameOnly: false,
};
const older = { ...active, key: "github.com/99" };
const zero = { completed: 0, published: 0, completedLocally: 0 };
const published = { completed: 1, published: 1, completedLocally: 0 };
const local = { completed: 1, published: 0, completedLocally: 1 };
const week = {
	start: "2026-11-02T00:00:00Z",
	end: "2026-11-09T00:00:00Z",
	counts: published,
};
const recent: components["schemas"]["StatsResponse"] = {
	totals: published,
	completedLast7Days: 1,
	undatedCompletions: 1,
	repositoryOptions: [active, older],
	repositories: [{ repository: active, counts: published }],
	weeks: [week],
};

beforeEach(() => sessionStorage.setItem("loop-reviewer.capability", "test"));
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

function mount() {
	const cache = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	render(
		<QueryClientProvider client={cache}>
			<StatsScreen />
		</QueryClientProvider>,
	);
}

test("shows outcomes and weekly counts, and keeps colliding repository names distinct across filters", async () => {
	const send = vi.fn(async (path: string) => {
		const query = new URL(path, "http://localhost").searchParams;
		const all = query.get("range") === "all";
		if (query.get("repository") === older.key) {
			const counts = all ? local : zero;
			return Response.json({
				...recent,
				totals: counts,
				repositories: [{ repository: older, counts }],
				weeks: [{ ...week, counts: zero }],
			});
		}
		return Response.json(
			all
				? {
						...recent,
						totals: { completed: 2, published: 1, completedLocally: 1 },
						repositories: [
							...recent.repositories,
							{ repository: older, counts: local },
						],
					}
				: recent,
		);
	});
	vi.stubGlobal("fetch", send);
	mount();
	const user = userEvent.setup();
	await screen.findByText("All accounts in this local workspace");
	const weekly = await screen.findByRole("region", { name: "Weekly activity" });
	expect(weekly.textContent).toContain("Last 30 days · UTC");
	expect(weekly.textContent).toContain("1 completed");
	await screen.findByText(/1 completion has no recorded date/);

	const breakdown = screen.getByRole("region", {
		name: "Repository breakdown",
	});
	expect(
		within(breakdown).getByText("github.com/org/repo · ID 42"),
	).toBeTruthy();
	const repo = screen.getByRole("combobox", { name: "Repository" });
	const period = screen.getByRole("combobox", { name: "Period" });
	expect(
		within(repo).getByRole("option", { name: "github.com/org/repo · ID 99" }),
	).toBeTruthy();
	await user.selectOptions(repo, older.key);
	await screen.findByText("No reviews were completed in this period.");
	expect(send.mock.calls.at(-1)?.[0]).toContain("repository=github.com%2F99");
	await user.selectOptions(period, "all");
	const totals = await screen.findByRole("region", { name: "Totals" });
	expect(totals.textContent).toMatch(/Completed locally\s*1/);
	expect(
		screen.getByRole("region", { name: "Weekly activity" }).textContent,
	).toContain("0 completed");
	await user.selectOptions(repo, "");
	const allRepos = await screen.findByRole("region", {
		name: "Repository breakdown",
	});
	expect(
		within(allRepos).getByText("github.com/org/repo · ID 42"),
	).toBeTruthy();
	expect(
		within(allRepos).getByText("github.com/org/repo · ID 99"),
	).toBeTruthy();
});

test("shows loading, offers retry after failure, and identifies a genuinely empty workspace", async () => {
	let fail!: (response: Response) => void;
	vi.stubGlobal(
		"fetch",
		vi
			.fn()
			.mockImplementationOnce(
				() =>
					new Promise<Response>((resolve) => {
						fail = resolve;
					}),
			)
			.mockImplementationOnce(async () =>
				Response.json({
					...recent,
					totals: zero,
					undatedCompletions: 0,
					repositoryOptions: [],
					repositories: [],
					weeks: [{ ...week, counts: zero }],
				}),
			),
	);
	mount();
	expect(screen.getByRole("status").textContent).toBe("Loading stats…");
	fail(
		Response.json(
			{ code: "storage", message: "Stats unavailable" },
			{ status: 500 },
		),
	);
	expect((await screen.findByRole("alert")).textContent).toBe(
		"Stats unavailable",
	);
	await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
	await screen.findByText("No reviews have been completed in this workspace.");
	expect(
		screen.queryByText("No reviews were completed in this period."),
	).toBeNull();
});
