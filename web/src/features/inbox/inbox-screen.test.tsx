// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
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

const requested = {
	code: "direct_request",
	label: "Review requested",
} as const;
const authored = { code: "authored", label: "You opened this PR" } as const;

beforeEach(() => sessionStorage.setItem("loop-reviewer.capability", "test"));
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

function mount(completionSummary?: ReactNode) {
	render(
		<QueryClientProvider
			client={
				new QueryClient({ defaultOptions: { queries: { retry: false } } })
			}
		>
			<InboxScreen
				onAccepted={() => "Review submitted."}
				reviewsLink={<a href="/reviews">View Reviews</a>}
				setupLink={<a href="/setup">Setup</a>}
				completionSummary={completionSummary}
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

test.each([
	["configured", report, "No pull requests are waiting for review."],
	["unconfigured", { kind: "unconfigured" }, "Inbox setup needed"],
])(
	"renders the %s Inbox without an optional summary",
	async (_state, data, message) => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async () => Response.json(data)),
		);
		mount();
		await screen.findByText(message);
		expect(screen.getByRole("button", { name: "Scan now" })).toBeTruthy();
		expect(
			screen.queryByRole("link", { name: /reviews completed/ }),
		).toBeNull();
	},
);

test("renders the supplied completion summary with the Inbox controls", async () => {
	vi.stubGlobal(
		"fetch",
		vi.fn(async () => Response.json(report)),
	);
	mount(<a href="/stats">4 reviews completed in the last 7 days</a>);
	await screen.findByText("No pull requests are waiting for review.");
	expect(
		screen
			.getByRole("link", { name: "4 reviews completed in the last 7 days" })
			.getAttribute("href"),
	).toBe("/stats");
	expect(screen.getByRole("button", { name: "Scan now" })).toBeTruthy();
});

test("launches a second row while the first request is pending", async () => {
	const rows = ["1", "2"].map((number) => ({
		title: `Pull request ${number}`,
		url: `https://github.com/org/repo/pull/${number}`,
		repository: "org/repo",
		identity: {
			host: "github.com",
			repositoryId: "44",
			number,
			account: "alice",
		},
		author: "bob",
		stale: false,
		reasons: [requested.label],
		reasonDetails: [requested],
		openedAt: null,
		lastSaved: null,
	}));
	let acceptFirst!: (response: Response) => void;
	const firstReceipt = new Promise<Response>((resolve) => {
		acceptFirst = resolve;
	});
	const send = vi.fn(async (path: string, init: RequestInit) => {
		if (path.endsWith("/launch")) {
			const body = JSON.parse(String(init.body));
			return body.url === rows[0].url
				? firstReceipt
				: Response.json({ launchId: "second" }, { status: 202 });
		}
		return Response.json({ ...report, active: rows });
	});
	vi.stubGlobal("fetch", send);
	mount();
	const user = userEvent.setup();
	const heading = await screen.findByRole("heading", {
		name: "Pull request 1",
	});
	const row = heading.closest("li");
	if (!row) throw new Error("Inbox row missing");
	const first = within(row);
	const buttons = screen.getAllByRole<HTMLButtonElement>("button", {
		name: "Open review",
	});
	await user.click(buttons[0]);
	expect(buttons[0].disabled).toBe(true);
	expect(buttons[1].disabled).toBe(false);
	expect(
		first.getByRole<HTMLButtonElement>("button", { name: "Later" }).disabled,
	).toBe(false);
	expect(
		screen.getByRole<HTMLButtonElement>("button", { name: "Scan now" })
			.disabled,
	).toBe(false);
	await user.click(buttons[1]);
	await screen.findByRole("link", { name: "View Reviews" });
	expect(
		send.mock.calls.filter(([path]) => path.endsWith("/launch")),
	).toHaveLength(2);
	acceptFirst(Response.json({ launchId: "first" }, { status: 202 }));
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
		reasons: [requested.label],
		reasonDetails: [requested],
		openedAt: null,
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
			reasons: [authored.label],
			reasonDetails: [authored],
			openedAt: null,
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
		screen.getByRole("button", { name: "All repositories 0 later" }),
	);
	expect(screen.getByRole("tab", { name: "Active 2" })).toBeTruthy();
});

test.each([
	["legacy timestamp", null],
	["timestamp below the browser date range", -8_640_000_000_001],
	["timestamp above the browser date range", 8_640_000_000_001],
])("filters and sorts requests with a %s", async (_name, unavailable) => {
	const now = Math.floor(Date.now() / 1000);
	const rows = [
		"Recent request",
		"Other change",
		"Older request",
		"Legacy request",
	].map((title, index) => ({
		title,
		author: "sam",
		repository: "org/repo",
		identity: {
			host: "github.com",
			repositoryId: "44",
			account: "alice",
			number: String(index + 1),
		},
		url: `https://github.com/org/repo/pull/${index + 1}`,
		reasons: [index === 1 ? authored.label : requested.label],
		reasonDetails: [index === 1 ? authored : requested],
		openedAt: index === 3 ? unavailable : now - (index + 1) * 3600,
		lastSaved: null,
		stale: false,
	}));
	vi.stubGlobal(
		"fetch",
		vi.fn(async () =>
			Response.json({
				...report,
				active:
					unavailable !== null && unavailable > now
						? [rows[3], ...rows.slice(0, 3)]
						: rows,
				later: [rows[2]],
			}),
		),
	);
	mount();
	const user = userEvent.setup();
	await screen.findByText("1h ago");
	expect(screen.getByText(/opening time unavailable/i)).toBeTruthy();
	const titles = () =>
		screen.getAllByRole("heading", { level: 3 }).map((el) => el.textContent);
	expect(titles()).toEqual(rows.map((row) => row.title));
	await user.click(screen.getByRole("button", { name: "Requested from me 3" }));
	await user.click(
		screen.getByRole("button", { name: "Sort by opening time: newest first" }),
	);
	expect(titles()).toEqual([
		"Older request",
		"Recent request",
		"Legacy request",
	]);
	await user.click(screen.getByRole("tab", { name: "Later 1" }));
	await user.click(screen.getByRole("button", { name: "All repositories" }));
	expect(
		screen.getByRole("button", { name: "github.com/org/repo 1 later" }),
	).toBeTruthy();
});
