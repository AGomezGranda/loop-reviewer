// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, Link, Outlet, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { OpenReviewForm } from "@/features/review/open-review-form";
import { useReviewNavigation } from "./review-navigation";

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

function Layout() {
	return (
		<>
			<OpenReviewForm onOpened={useReviewNavigation()} />
			<Link to="/setup">Setup</Link>
			<Outlet />
		</>
	);
}

test.each([
	{
		name: "same history entry",
		leaves: false,
		path: "/history/7",
		heading: "Completed review",
	},
	{
		name: "new history entry",
		leaves: true,
		path: "/setup",
		heading: "Inbox setup",
	},
])(
	"routes one-off results from the $name",
	async ({ leaves, path, heading }) => {
		sessionStorage.setItem("loop-reviewer.capability", "test");
		let resolveOpen!: (response: Response) => void;
		const response = new Promise<Response>((resolve) => {
			resolveOpen = resolve;
		});
		vi.stubGlobal(
			"fetch",
			vi.fn(() => response),
		);
		const router = createMemoryRouter([
			{
				Component: Layout,
				children: [
					{ index: true, element: <h1>Review inbox</h1> },
					{ path: "setup", element: <h1>Inbox setup</h1> },
					{ path: "history/:runId", element: <h1>Completed review</h1> },
				],
			},
		]);
		render(
			<QueryClientProvider client={new QueryClient()}>
				<RouterProvider router={router} />
			</QueryClientProvider>,
		);
		const user = userEvent.setup();
		await user.type(
			screen.getByLabelText("Pull request URL"),
			"https://github.com/org/repo/pull/7",
		);
		await user.click(screen.getByRole("button", { name: "Open review" }));
		if (leaves) await user.click(screen.getByRole("link", { name: "Setup" }));
		resolveOpen(
			Response.json({ kind: "completion", completion: { runId: "7" } }),
		);
		await screen.findByRole("heading", { name: heading });
		if (leaves)
			await screen.findByText("The completed review is available in History.");
		expect(router.state.location.pathname).toBe(path);
	},
);
