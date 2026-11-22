// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { components } from "@/api/schema";
import { PublicationScreen } from "./publication-screen";

const preview: components["schemas"]["PreviewResponse"] = {
	id: "75f718d3-9633-42ad-9097-22f320d068a8",
	runId: "1",
	version: "2",
	action: "comment",
	selection: {
		url: "https://github.com/org/repo/pull/1",
		account: "alice",
		author: "bob",
		targetBranch: "main",
		baseTip: "a".repeat(40),
		mergeBase: "b".repeat(40),
		head: "c".repeat(40),
	},
	body: "<!-- loop-reviewer:75f718d3-9633-42ad-9097-22f320d068a8 -->\n\n  exact body\n",
	comments: [{ path: "src/example.py", line: "7", body: "  exact inline\n" }],
};
const path = `/api/v1/publications/${preview.id}`;

beforeEach(() => sessionStorage.setItem("loop-reviewer.capability", "test"));
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

function mount() {
	return render(
		<QueryClientProvider
			client={
				new QueryClient({ defaultOptions: { queries: { retry: false } } })
			}
		>
			<PublicationScreen
				id={preview.id}
				onChanged={() => undefined}
				draftLink={(id) => <a href={`/drafts/${id}`}>Make a fresh preview</a>}
				recoveryLink={(id) => (
					<a href={`/publications/${id}`}>Recover another publication</a>
				)}
				historyLink={(id) => (
					<a href={`/history/${id}`}>Open completed review</a>
				)}
			/>
		</QueryClientProvider>,
	);
}

test("displays the exact snapshot and sends only after explicit confirmation", async () => {
	let status = "preview";
	const send = vi.fn(async (_url: string, init: RequestInit) => {
		if (init.method === "POST") {
			status = "published";
			return Response.json({
				status,
				completion: { receipt: { reviewId: "190", action: "comment" } },
			});
		}
		return Response.json({
			preview,
			status,
			receipt:
				status === "published" ? { reviewId: "190", action: "comment" } : null,
		});
	});
	vi.stubGlobal("fetch", send);
	mount();
	await screen.findByRole("heading", { name: "Publication preview" });
	expect((await screen.findByLabelText("Exact review body")).textContent).toBe(
		preview.body,
	);
	expect(
		(await screen.findByLabelText("Inline body for src/example.py:7"))
			.textContent,
	).toBe(preview.comments[0].body);
	expect(send.mock.calls.every(([, init]) => init.method === "GET")).toBe(true);
	await userEvent
		.setup()
		.click(screen.getByRole("button", { name: "Confirm comment as @alice" }));
	await screen.findByRole("link", { name: "Open completed review" });
	const sends = send.mock.calls.filter(([, init]) => init.method === "POST");
	expect(sends).toHaveLength(1);
	expect(sends[0][0]).toBe(`${path}/confirm`);
	expect(sends[0][1].body).toBeUndefined();
});

test.each([
	["unconfirmed", "Recover publication"],
	["preview", "Make a fresh preview"],
])(
	"reads %s after a lost response and requires another explicit decision",
	async (status, label) => {
		let lost = false;
		const send = vi.fn(async (_path: string, init: RequestInit) => {
			if (init.method === "POST") {
				lost = true;
				throw new TypeError("fetch failed");
			}
			return Response.json({
				preview,
				status: lost ? status : "preview",
				receipt: null,
			});
		});
		vi.stubGlobal("fetch", send);
		const view = mount();
		await userEvent.setup().click(
			await screen.findByRole("button", {
				name: "Confirm comment as @alice",
			}),
		);
		await screen.findByText(label);
		await waitFor(() =>
			expect(
				send.mock.calls.filter(([, init]) => init.method === "GET").length,
			).toBeGreaterThan(1),
		);
		expect(
			screen.queryByRole("button", { name: "Confirm comment as @alice" }),
		).toBeNull();
		view.unmount();
		mount();
		await screen.findByText(label);
		expect(
			screen.queryByRole("button", { name: "Confirm comment as @alice" }),
		).toBeNull();
		expect(
			send.mock.calls.filter(([, init]) => init.method === "POST"),
		).toHaveLength(1);
	},
);

test("recovery requires complete evidence and explicit duplicate risk consent before retry", async () => {
	let recoveries = 0;
	const send = vi.fn(async (url: string, init: RequestInit) => {
		if (url.endsWith("/recover")) {
			recoveries += 1;
			return Response.json(
				{
					code:
						recoveries === 1
							? "evidence_inconclusive"
							: "duplicate_risk_consent_required",
					message:
						recoveries === 1 ? "Evidence was incomplete." : "No marker found.",
				},
				{ status: 409 },
			);
		}
		if (url.endsWith("/retry")) return Response.json({ status: "unconfirmed" });
		expect(init.method).toBe("GET");
		return Response.json({ preview, receipt: null, status: "unconfirmed" });
	});
	vi.stubGlobal("fetch", send);
	mount();
	const user = userEvent.setup();
	await user.click(
		await screen.findByRole("button", { name: "Recover publication" }),
	);
	await screen.findByText("Evidence was incomplete.");
	expect(
		screen.queryByRole("button", { name: "Retry publication as @alice" }),
	).toBeNull();
	await waitFor(() =>
		expect(
			(
				screen.getByRole("button", {
					name: "Recover publication",
				}) as HTMLButtonElement
			).disabled,
		).toBe(false),
	);
	await user.click(screen.getByRole("button", { name: "Recover publication" }));
	const retry = await screen.findByRole("button", {
		name: "Retry publication as @alice",
	});
	expect((retry as HTMLButtonElement).disabled).toBe(true);
	expect(send.mock.calls.some(([url]) => url.endsWith("/retry"))).toBe(false);
	await user.click(
		screen.getByRole("checkbox", {
			name: "I accept the risk of publishing a duplicate review.",
		}),
	);
	await user.click(retry);
	expect(
		send.mock.calls.find(([url]) => url.endsWith("/retry"))?.[1].body,
	).toBe('{"acceptDuplicateRisk":true}');
});

test.each([
	{
		details: { code: "recovery_required", publicationId: "other" },
		link: "Recover another publication",
		href: "/publications/other",
	},
	{
		details: { code: "review_completed", completedRunId: "2" },
		link: "Open completed review",
		href: "/history/2",
	},
])(
	"links directly to $link when a preview is blocked",
	async ({ details, link, href }) => {
		vi.stubGlobal(
			"fetch",
			vi.fn(async (_url: string, init: RequestInit) =>
				init.method === "POST"
					? Response.json(
							{ ...details, message: "Publication is blocked." },
							{ status: 409 },
						)
					: Response.json({ preview, status: "preview", receipt: null }),
			),
		);
		mount();
		const confirm = { name: "Confirm comment as @alice" };
		await userEvent.setup().click(await screen.findByRole("button", confirm));
		expect(
			(await screen.findByRole("link", { name: link })).getAttribute("href"),
		).toBe(href);
		expect(screen.queryByRole("button", confirm)).toBeNull();
		expect(
			screen.queryByRole("link", { name: "Make a fresh preview" }),
		).toBeNull();
	},
);
