// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { InboxReport } from "./inbox-queries";
import { InboxScreen } from "./inbox-screen";

const report: InboxReport = {
	kind: "report",
	prefix: "[ABC-…]",
	settingsVersion: "9007199254740993",
	repositories: [
		{
			host: "github.com",
			repositoryId: "44",
			repository: "org/repo",
			account: "alice",
			status: "success",
			lastSuccess: "2026-01-01T10:00:00Z",
			error: null,
		},
	],
	active: [],
	later: [],
};

beforeEach(() => sessionStorage.setItem("loop-reviewer.capability", "test"));
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

function mount() {
	render(
		<QueryClientProvider
			client={
				new QueryClient({ defaultOptions: { queries: { retry: false } } })
			}
		>
			<InboxScreen
				onOpened={() => undefined}
				setupLink={<a href="/setup">Setup</a>}
			/>
		</QueryClientProvider>,
	);
}

test.each([
	["healthy", report, "No pull requests are waiting for review."],
	[
		"partial failure",
		{
			...report,
			repositories: [
				...report.repositories,
				{
					...report.repositories[0],
					repositoryId: "45",
					repository: "org/other",
					status: "failed",
					error: "Access denied",
				},
			],
		},
		"org/other failed",
	],
])("shows the %s inbox state", async (_name, data, message) => {
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => Response.json(data)),
	);
	mount();
	await screen.findByText(message);
});

test("Later and Restore submit the identity and settings version displayed with the row", async () => {
	const row = {
		title: "Fix polling",
		url: "https://github.com/org/repo/pull/1",
		repository: "org/repo",
		identity: {
			host: "github.com",
			repositoryId: "44",
			number: "1",
			account: "alice",
		},
		author: "bob",
		stale: false,
		reasons: ["Your review was requested"],
		lastSaved: null,
	};
	let current = { ...report, active: [row] };
	const send = vi.fn(async (path: string, init: RequestInit) => {
		if (path.endsWith("/later"))
			current = { ...report, later: [row], active: [] };
		if (path.endsWith("/restore")) current = { ...report, active: [row] };
		if (init.method === "POST")
			expect(JSON.parse(String(init.body))).toEqual({
				url: row.url,
				settingsVersion: report.settingsVersion,
				identity: row.identity,
			});
		return Response.json(current);
	});
	vi.stubGlobal("fetch", send);
	mount();
	const user = userEvent.setup();
	await user.click(await screen.findByRole("button", { name: "Later" }));
	await user.click(await screen.findByRole("tab", { name: "Later 1" }));
	await user.click(await screen.findByRole("button", { name: "Restore" }));
	await user.click(await screen.findByRole("tab", { name: "Active 1" }));
	await screen.findByRole("button", { name: "Later" });
	expect(
		send.mock.calls
			.filter(([, init]) => init.method === "POST")
			.map(([url]) => url),
	).toEqual(["/api/v1/inbox/later", "/api/v1/inbox/restore"]);
});

test("searches across hosts and scopes both queues to the selected repository", async () => {
	const repositories = Array.from({ length: 12 }, (_, i) => ({
		...report.repositories[0],
		repositoryId: String(i + 1),
		repository: `org/service-${i + 1}`,
	}));
	const selected = { ...repositories[0], host: "enterprise.example" };
	repositories.push(selected);
	function row(repo: typeof selected, title: string) {
		return {
			title,
			repository: repo.repository,
			identity: {
				host: repo.host,
				repositoryId: repo.repositoryId,
				account: repo.account,
				number: "1",
			},
			url: `https://${repo.host}/${repo.repository}/pull/1`,
			author: "sam",
			stale: false,
			reasons: ["Your PR"],
			lastSaved: null,
		};
	}
	vi.stubGlobal(
		"fetch",
		vi.fn(async () =>
			Response.json({
				...report,
				repositories,
				active: [
					row(repositories[0], "Public change"),
					row(selected, "Enterprise change"),
				],
				later: [],
			}),
		),
	);
	mount();
	const user = userEvent.setup();
	await user.click(
		await screen.findByRole("button", { name: "All repositories" }),
	);
	await user.type(
		screen.getByRole("textbox", { name: "Search repositories" }),
		"enterprise",
	);
	await user.click(
		screen.getByRole("button", {
			name: "enterprise.example/org/service-1 1 active",
		}),
	);
	await screen.findByRole("heading", { name: "Enterprise change" });
	expect(screen.queryByText("Public change")).toBeNull();
	expect(screen.getByRole("tab", { name: "Active 1" })).toBeTruthy();
	await user.click(screen.getByRole("tab", { name: "Later 0" }));
	await screen.findByText("No deferred pull requests.");
	await user.click(
		screen.getByRole("button", { name: "enterprise.example/org/service-1" }),
	);
	await user.click(
		screen.getByRole("button", { name: "All repositories 2 active" }),
	);
	expect(screen.getByRole("tab", { name: "Active 2" })).toBeTruthy();
});
