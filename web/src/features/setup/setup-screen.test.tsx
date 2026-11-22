// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { SetupScreen } from "./setup-screen";

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	sessionStorage.clear();
});

test("edits saved settings, keeps inputs on failure, and shows the saved values", async () => {
	sessionStorage.setItem("loop-reviewer.capability", "test");
	const first = "https://github.com/org/first";
	const second = "https://github.com/org/second";
	const initial = {
		settingsVersion: "1",
		jiraProjectKeys: ["ABC"],
		repositoryUrls: [first],
	};
	let attempts = 0;
	vi.stubGlobal(
		"fetch",
		vi.fn(async (url: string, init: RequestInit) => {
			expect(url).toBe("/api/v1/inbox/configuration");
			if (init.method === "GET") return Response.json(initial);
			expect(JSON.parse(String(init.body))).toEqual({
				settingsVersion: "1",
				jiraProjectKeys: ["xyz"],
				repositoryUrls: [first, second],
			});
			if (++attempts === 1) {
				return Response.json(
					{ code: "github_unavailable", message: "Repository unavailable" },
					{ status: 502 },
				);
			}
			return Response.json({
				settingsVersion: "2",
				jiraProjectKeys: ["XYZ"],
				repositoryUrls: [first, second],
			});
		}),
	);

	const onSaved = vi.fn();
	render(
		<QueryClientProvider client={new QueryClient()}>
			<SetupScreen onSaved={onSaved} />
		</QueryClientProvider>,
	);
	const user = userEvent.setup();
	const projects = await screen.findByLabelText("Jira project key");
	const repositories = screen.getByLabelText("Repository URL 1");
	expect(projects).toHaveProperty("value", "ABC");
	expect(repositories).toHaveProperty("value", first);
	await user.clear(projects);
	await user.type(projects, " xyz ");
	await user.click(screen.getByRole("button", { name: "Add repository" }));
	const secondInput = screen.getByLabelText("Repository URL 2");
	expect(document.activeElement).toBe(secondInput);
	await user.paste(`${second}\nhttps://github.com/org/extra`);
	await user.click(screen.getByRole("button", { name: "Remove repository 3" }));
	const save = screen.getByRole("button", { name: "Save settings" });
	await user.click(save);
	expect((await screen.findByRole("alert")).textContent).toBe(
		"Repository unavailable",
	);
	expect(screen.getByLabelText("Repository URL 2")).toHaveProperty(
		"value",
		second,
	);
	await user.click(save);
	await screen.findByText("Watching 2 repositories for XYZ.");
	expect(projects).toHaveProperty("value", "XYZ");
	expect(onSaved).toHaveBeenCalledOnce();
});

test("uses refreshed settings while pristine and explicitly reloads after an edit conflict", async () => {
	sessionStorage.setItem("loop-reviewer.capability", "test");
	const repositoryUrls = ["https://github.com/org/first"];
	let current = {
		settingsVersion: "2",
		jiraProjectKeys: ["NEW"],
		repositoryUrls,
	};
	const sent: unknown[] = [];
	vi.stubGlobal(
		"fetch",
		vi.fn(async (_url: string, init: RequestInit) => {
			if (init.method === "GET") return Response.json(current);
			const body = JSON.parse(String(init.body));
			sent.push(body.settingsVersion);
			if (body.settingsVersion !== current.settingsVersion) {
				return Response.json(
					{ code: "inbox_changed", message: "Settings changed." },
					{ status: 409 },
				);
			}
			current = {
				...current,
				settingsVersion: "4",
				jiraProjectKeys: body.jiraProjectKeys,
			};
			return Response.json(current);
		}),
	);
	const cache = new QueryClient();
	cache.setQueryData(["inbox-configuration"], {
		...current,
		settingsVersion: "1",
		jiraProjectKeys: ["OLD"],
	});
	render(
		<QueryClientProvider client={cache}>
			<SetupScreen onSaved={() => undefined} />
		</QueryClientProvider>,
	);
	const user = userEvent.setup();
	const project = screen.getByLabelText("Jira project key");
	await waitFor(() => expect(project).toHaveProperty("value", "NEW"));
	await user.clear(project);
	await user.type(project, "MINE");
	current = { ...current, settingsVersion: "3", jiraProjectKeys: ["OTHER"] };
	const save = screen.getByRole("button", { name: "Save settings" });
	await user.click(save);
	const reload = await screen.findByRole("button", {
		name: "Reload settings and discard edits",
	});
	expect(project).toHaveProperty("value", "MINE");
	expect(save).toHaveProperty("disabled", true);
	await user.click(reload);
	await waitFor(() => expect(project).toHaveProperty("value", "OTHER"));
	await user.clear(project);
	await user.type(project, "MINE");
	await user.click(save);
	await screen.findByText("Watching 1 repository for MINE.");
	expect(sent).toEqual(["2", "3"]);
});
