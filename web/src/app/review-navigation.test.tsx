// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, Link, Outlet, RouterProvider } from "react-router";
import { afterEach, expect, test, vi } from "vitest";
import { OpenReviewForm } from "@/features/review/open-review-form";
import { useReviewLaunch } from "./review-navigation";

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

function Layout() {
	return (
		<>
			<OpenReviewForm
				onAccepted={useReviewLaunch()}
				reviewsLink={<Link to="/reviews">View Reviews</Link>}
			/>
			<Link to="/setup">Setup</Link>
			<Outlet />
		</>
	);
}

test.each([
	{
		name: "same history entry",
		leaves: false,
		path: "/",
		heading: "Review inbox",
	},
	{
		name: "new history entry",
		leaves: true,
		path: "/setup",
		heading: "Inbox setup",
	},
])(
	"keeps the current route when a launch is accepted from the $name",
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
					{ path: "reviews", element: <h1>Reviews</h1> },
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
		resolveOpen(Response.json({ launchId: "7" }, { status: 202 }));
		await screen.findByRole("heading", { name: heading });
		await screen.findByText("Review submitted.");
		expect(
			screen.getByRole("link", { name: "View Reviews" }).getAttribute("href"),
		).toBe("/reviews");
		expect(
			(screen.getByLabelText("Pull request URL") as HTMLInputElement).value,
		).toBe("");
		expect(router.state.location.pathname).toBe(path);
	},
);

test("shows the backend setup guidance verbatim", async () => {
	sessionStorage.setItem("loop-reviewer.capability", "test");
	const message = "Set LOOP_REVIEWER_MAX_MODEL_SPEND_USD_MICROS.";
	vi.stubGlobal(
		"fetch",
		vi.fn(async () =>
			Response.json({ code: "review_unavailable", message }, { status: 409 }),
		),
	);
	const router = createMemoryRouter([{ path: "/", Component: Layout }]);
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
	await screen.findByText(message);
});
