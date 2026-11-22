// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useNavigate } from "react-router";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { DraftScreen } from "./draft-screen";
import type { Draft } from "./review-queries";

const draft: Draft = {
	runId: "9007199254740993",
	version: "7",
	selection: {
		url: "https://github.com/org/repo/pull/1",
		account: "alice",
		author: "bob",
		targetBranch: "main",
		head: "a".repeat(40),
		baseTip: "b".repeat(40),
		mergeBase: "c".repeat(40),
	},
	feedback: {
		verdict: "comment",
		summary: "Original summary",
		findings: [
			{
				number: 1,
				severity: "high",
				condition: "First condition",
				consequence: "First consequence",
				path: null,
				line: null,
			},
			{
				number: 2,
				severity: "low",
				condition: "Second condition",
				consequence: "Second consequence",
				path: null,
				line: null,
			},
		],
	},
	publicationId: null,
};

beforeEach(() => sessionStorage.setItem("loop-reviewer.capability", "test"));
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

function DraftRoute() {
	const navigate = useNavigate();
	return (
		<DraftScreen
			runId={draft.runId}
			onFinished={(runId) => void navigate(`/history/${runId}`)}
			historyLink={(id) => <a href={`/history/${id}`}>Open history</a>}
			recoveryLink={(id) => (
				<a href={`/publications/${id}`}>Recover publication</a>
			)}
			onChanged={() => undefined}
		/>
	);
}

function mount(
	client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
	const router = createMemoryRouter(
		[
			{ path: "/drafts/:runId", Component: DraftRoute },
			{ path: "/history/:runId", element: <h1>Saved review history</h1> },
			{ path: "/setup", element: <h1>Inbox setup</h1> },
		],
		{ initialEntries: [`/drafts/${draft.runId}`] },
	);
	render(
		<QueryClientProvider client={client}>
			<RouterProvider router={router} />
		</QueryClientProvider>,
	);
	return router;
}

test("a conflicting edit retains the input and requires selection at the new version", async () => {
	const second = { ...draft.feedback.findings[1], number: 1 };
	const newer = {
		...draft,
		version: "8",
		feedback: { ...draft.feedback, findings: [second] },
	};
	let current = draft;
	const edits: unknown[] = [];
	vi.stubGlobal(
		"fetch",
		vi.fn(async (_path: string, init: RequestInit) => {
			if (init.method !== "PATCH") return Response.json(current);
			edits.push(JSON.parse(String(init.body)));
			if (edits.length === 1) {
				current = newer;
				return Response.json(
					{ code: "draft_changed", message: "The draft changed." },
					{ status: 409 },
				);
			}
			return Response.json({ ...newer, version: "9" });
		}),
	);
	mount();
	const user = userEvent.setup();
	await user.click(
		await screen.findByRole("button", { name: "Edit finding 1" }),
	);
	const box = screen.getByRole("textbox", { name: "Condition" });
	await user.clear(box);
	await user.type(box, "Unsaved feedback");
	await user.click(screen.getByRole("button", { name: "Save changes" }));
	await screen.findByRole("alert", { name: "" });
	expect((box as HTMLTextAreaElement).value).toBe("Unsaved feedback");
	expect(
		(screen.getByRole("button", { name: "Save changes" }) as HTMLButtonElement)
			.disabled,
	).toBe(true);
	expect(edits).toEqual([
		{
			version: "7",
			kind: "edit_finding",
			number: 1,
			condition: "Unsaved feedback",
			consequence: "First consequence",
		},
	]);
	await user.click(screen.getByRole("button", { name: "Discard editor" }));
	await user.click(
		await screen.findByRole("button", { name: "Edit finding 1" }),
	);
	expect(
		(screen.getByRole("textbox", { name: "Condition" }) as HTMLTextAreaElement)
			.value,
	).toBe("Second condition");
	await user.click(screen.getByRole("button", { name: "Save changes" }));
	expect(edits[1]).toMatchObject({ version: "8", number: 1 });
});

test("local finish uses the displayed version and opens the completion", async () => {
	const fetcher = vi.fn(async (_path: string, init: RequestInit) =>
		Response.json(init.method === "POST" ? { runId: draft.runId } : draft),
	);
	vi.stubGlobal("fetch", fetcher);
	const router = mount();
	const user = userEvent.setup();
	await user.click(
		await screen.findByRole("button", { name: "Finish locally" }),
	);
	await user.click(
		await screen.findByRole("button", { name: "Confirm local finish" }),
	);
	await screen.findByRole("heading", { name: "Saved review history" });
	expect(router.state.location.pathname).toBe(`/history/${draft.runId}`);
	expect(
		fetcher.mock.calls.find(([, init]) => init.method === "POST")?.[1].body,
	).toBe('{"version":"7"}');
});

test("a draft completed elsewhere links to history without discarding unsaved feedback", async () => {
	let edited = false;
	vi.stubGlobal(
		"fetch",
		vi.fn(async (_path: string, init: RequestInit) => {
			if (init.method === "PATCH") {
				edited = true;
				return Response.json(
					{ code: "draft_changed", message: "Draft changed." },
					{ status: 409 },
				);
			}
			return edited
				? Response.json(
						{
							code: "review_completed",
							message: "Review completed.",
							completedRunId: draft.runId,
						},
						{ status: 409 },
					)
				: Response.json(draft);
		}),
	);
	mount();
	const user = userEvent.setup();
	await user.click(await screen.findByRole("button", { name: "Edit summary" }));
	const summary = screen.getByRole("textbox", { name: "Summary" });
	await user.clear(summary);
	await user.type(summary, "Unsaved feedback");
	await user.click(screen.getByRole("button", { name: "Save changes" }));
	expect(
		(await screen.findByRole("link", { name: "Open history" })).getAttribute(
			"href",
		),
	).toBe(`/history/${draft.runId}`);
	expect(summary).toHaveProperty("value", "Unsaved feedback");
});

test("a local finish response does not redirect after leaving the draft", async () => {
	let resolveFinish!: (response: Response) => void;
	const finish = new Promise<Response>((resolve) => {
		resolveFinish = resolve;
	});
	const cache = new QueryClient({
		defaultOptions: { queries: { retry: false } },
	});
	const fetcher = vi.fn(async (_path: string, init: RequestInit) =>
		init.method === "POST" ? finish : Response.json(draft),
	);
	vi.stubGlobal("fetch", fetcher);
	const router = mount(cache);
	const user = userEvent.setup();
	await user.click(
		await screen.findByRole("button", { name: "Finish locally" }),
	);
	await user.click(
		screen.getByRole("button", { name: "Confirm local finish" }),
	);
	await screen.findByText("Saving review…");
	await act(() => router.navigate("/setup"));
	await act(async () => {
		resolveFinish(Response.json({ runId: draft.runId }));
		await finish;
	});
	expect(cache.isMutating()).toBe(0);
	expect(router.state.location.pathname).toBe("/setup");
});
