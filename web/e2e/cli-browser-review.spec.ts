import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

const repository = fileURLToPath(new URL("../..", import.meta.url));
const pr = "https://github.com/fixture/repo/pull/17";

function args(...command: string[]) {
	return [
		"run",
		"--project",
		repository,
		"--locked",
		"python",
		"-m",
		"tests.support.browser_review",
		...command,
	];
}

async function launchAddress(server: ChildProcess) {
	if (!server.stdout) throw new Error("Missing server stdout");
	const lines = createInterface({ input: server.stdout });
	try {
		for await (const line of lines) {
			if (line.startsWith("http://127.0.0.1:")) return line;
		}
		throw new Error("The local browser server exited before launch.");
	} finally {
		lines.close();
	}
}

test("pinned CLI reports stay private through browser edits and exact publication", async ({
	page,
}) => {
	const cwd = mkdtempSync(join(tmpdir(), "loop-reviewer-smoke-"));
	const state = join(cwd, "state");
	mkdirSync(state, { recursive: true });
	const env = {
		...process.env,
		PYTHONPATH: repository,
		XDG_STATE_HOME: state,
		XDG_CONFIG_HOME: cwd,
	};
	const cli = (...command: string[]) =>
		execFileSync("uv", args(...command), { cwd, env, encoding: "utf8" });
	const initial = cli("review", pr);
	const estimate = "Estimated generation cost · < $0.0001";
	const privateLead = "PRIVATE-LEAD-CANARY";
	const head = initial.match(/^Head: ([a-f\d]+)$/m)?.[1];
	expect(initial).toContain(estimate);
	expect(initial).toContain("Generation model: fixture/model");
	expect(initial).toMatch(
		/example\.py: unfinished.*\n.*settings\.txt: unfinished/,
	);
	const runId = initial.match(/^Run ID: (\d+)$/m)?.[1];
	if (!runId || !head) throw new Error("CLI did not report a run ID and head");
	const server = spawn("uv", args("ui"), {
		cwd,
		env,
		stdio: ["ignore", "pipe", "inherit"],
	});
	const exited = once(server, "exit");
	try {
		await page.goto(await launchAddress(server));
		await expect(
			page.getByRole("heading", { name: "Inbox setup needed" }),
		).toBeVisible();
		expect(new URL(page.url()).hash).toBe("");
		await page.getByRole("link", { name: "Setup", exact: true }).click();
		await expect(page.getByLabel("Jira project key")).toBeVisible();
		await expect(page.getByLabel("Pi reviewer model")).toHaveCount(0);
		await page.getByRole("link", { name: "Inbox", exact: true }).click();
		await page.setViewportSize({ width: 390, height: 844 });
		await page.getByLabel("Pull request URL").fill(pr);
		await page
			.getByRole("button", { name: "Open review", exact: true })
			.click();
		await page.getByRole("link", { name: "View Reviews" }).click();
		await page.getByRole("link", { name: "Open draft", exact: true }).click();
		await expect(page).toHaveURL(new RegExp(`/drafts/${runId}$`));
		const report = page.getByRole("region", {
			name: "Original reviewer report",
		});
		await expect(report.getByText(/Partial review/)).toBeVisible();
		await report.getByText(/Private leads, evidence/).click();
		await expect(report.getByText(new RegExp(privateLead))).toBeVisible();
		await report.getByText(/Private leads, evidence/).click();
		await expect(page.getByText(estimate)).toBeVisible();

		await page.getByRole("button", { name: "Edit summary" }).click();
		const edited =
			"Browser-edited feedback\n\nThe second paragraph is preserved.";
		const summary = page.getByRole("textbox", { name: "Summary", exact: true });
		await summary.fill(edited);
		const history = page.getByRole("link", { name: "History", exact: true });
		await history.focus();
		await page.keyboard.press("Enter");
		await expect(
			page
				.getByRole("alertdialog")
				.getByRole("button", { name: "Keep editing" }),
		).toBeFocused();
		await page.keyboard.press("Escape");
		await expect(history).toBeFocused();
		await expect(summary).toHaveValue(edited);
		expect(
			await page.evaluate(() => document.documentElement.scrollWidth),
		).toBe(390);
		await page.getByRole("button", { name: "Save changes" }).click();
		await expect(page.getByText("Draft saved at version 2.")).toBeVisible();

		const action = page.getByLabel("GitHub action");
		const prepare = page.getByRole("button", { name: "Prepare preview" });
		const fresh = page.getByRole("link", { name: "Make a fresh preview" });
		const body = page.getByLabel("Exact review body");
		const inline = page.getByLabel("Inline body for example.py:1");
		await action.selectOption("comment");
		await prepare.click();
		await expect(body).toContainText("Browser-edited feedback");
		await expect(body).not.toContainText(privateLead);
		const cliPreview = cli(
			"publish",
			"preview",
			runId,
			"--version",
			"2",
			"--action",
			"comment",
		);
		const cliBody = cliPreview.match(
			/----- BEGIN REVIEW BODY -----\n([\s\S]*?)\n----- END REVIEW BODY -----/,
		)?.[1];
		const normalize = (text: string) =>
			text.replace(/<!-- loop-reviewer:[a-f\d-]+ -->/, "<!-- marker -->");
		expect(cliPreview).not.toContain(privateLead);
		expect(normalize((await body.textContent()) ?? "")).toBe(
			normalize(cliBody ?? ""),
		);
		expect(await inline.textContent()).toBe(
			cliPreview.match(
				/----- BEGIN INLINE BODY -----\n([\s\S]*?)\n----- END INLINE BODY -----/,
			)?.[1],
		);
		await fresh.click();

		await action.selectOption("comment");
		await page.getByLabel("Summary only").check();
		await prepare.click();
		await expect(body).toContainText("CPython rejects the head file");
		await expect(body).not.toContainText(privateLead);
		await expect(inline).toHaveCount(0);
		await fresh.click();

		await page.getByRole("button", { name: "Remove finding 1" }).click();
		await expect(page.getByText(/Coverage is partial/)).toBeVisible();
		await expect(report.getByText("Original findings (1)")).toBeVisible();
		await expect(action).toHaveValue("");
		await action.selectOption("approve");
		await prepare.click();
		await expect(body).toContainText("Browser-edited feedback");
		await expect(body).not.toContainText(privateLead);
		await expect(body).not.toContainText("CPython rejects");
		const approvedBody = await body.textContent();
		await page
			.getByRole("button", { name: "Confirm approval as @reviewer" })
			.click();
		await expect(
			page.getByRole("status").filter({ hasText: "Published to GitHub" }),
		).toBeVisible();
		const sent = readFileSync(join(state, "sent.jsonl"), "utf8")
			.trim()
			.split("\n")
			.map((line) => JSON.parse(line));
		expect(sent).toEqual([
			{ body: approvedBody, action: "approve", head, comments: 0 },
		]);
		const completed = cli("review", pr);
		expect(completed).toContain("Published — Approval submitted");
		expect(completed).toContain("Original verified findings");
		expect(readFileSync(join(state, "events"), "utf8")).toBe("review\n");
	} finally {
		await page.close();
		server.kill("SIGINT");
		await exited;
		rmSync(cwd, { recursive: true, force: true });
	}
});

test("browser launches overlap, survive reload, and leave finished reviews in History", async ({
	page,
	context,
}) => {
	const cwd = mkdtempSync(join(tmpdir(), "loop-reviewer-parallel-"));
	const state = join(cwd, "state");
	const config = join(cwd, "loop-reviewer");
	mkdirSync(state);
	mkdirSync(config);
	writeFileSync(
		join(config, "config.toml"),
		'[inbox]\njira_project_key = "ABC"\nrepositories = ["https://github.com/fixture/repo"]\n',
	);
	const launchServer = () =>
		spawn("uv", args("ui"), {
			cwd,
			env: {
				...process.env,
				PYTHONPATH: repository,
				XDG_STATE_HOME: state,
				XDG_CONFIG_HOME: cwd,
				LOOP_REVIEWER_FIXTURE_GATES: state,
			},
			stdio: ["ignore", "pipe", "inherit"],
		});
	let server = launchServer();
	let exited = once(server, "exit");
	const second = pr.replace(/17$/, "18");
	const third = pr.replace(/17$/, "19");
	const release = (number: number) =>
		writeFileSync(join(state, `release-${number}`), "");
	const row = (url: string) =>
		page
			.getByRole("listitem")
			.filter({ has: page.getByRole("link", { name: url, exact: true }) });
	const tab = (name: string) =>
		page
			.getByRole("navigation", { name: "Workspace" })
			.getByRole("link", { name, exact: true });
	try {
		await page.setViewportSize({ width: 390, height: 844 });
		const address = new URL(await launchAddress(server));
		const capability = new URLSearchParams(address.hash.slice(1)).get(
			"capability",
		);
		await page.goto(address.href);
		await page.getByRole("button", { name: "Scan now" }).click();
		const inboxRow = page
			.getByRole("listitem")
			.filter({ hasText: "First fixture review" });
		await inboxRow.getByRole("button", { name: "Open review" }).click();
		await page.getByRole("link", { name: "View Reviews" }).click();
		await expect(row(pr).getByText("Running")).toBeVisible();

		const launch = async (url: string) => {
			await page.getByLabel("Pull request URL").fill(url);
			await page
				.getByRole("button", { name: "Open review", exact: true })
				.click();
			await expect(page.getByLabel("Pull request URL")).toHaveValue("");
		};
		await launch(second);
		await expect(row(second).getByText("Running")).toBeVisible();
		await launch(third);
		await expect(row(third).getByText("Queued")).toBeVisible();
		await expect
			.poll(
				() =>
					existsSync(join(state, "started-17")) &&
					existsSync(join(state, "started-18")),
			)
			.toBe(true);
		await page.reload();
		await expect(row(pr).getByText("Running")).toBeVisible();
		await expect(row(third).getByText("Queued")).toBeVisible();
		await page.close();
		page = await context.newPage();
		await page.setViewportSize({ width: 390, height: 844 });
		await page.goto(new URL(`/reviews${address.hash}`, address).href);
		await expect(row(pr).getByText("Running")).toBeVisible();
		await expect(row(second).getByText("Running")).toBeVisible();
		await expect(row(third).getByText("Queued")).toBeVisible();
		expect(
			await page.evaluate(() => document.documentElement.scrollWidth),
		).toBe(390);

		release(17);
		await row(pr).getByRole("link", { name: "Open draft" }).click();
		await page.getByRole("button", { name: "Finish locally" }).click();
		await page.getByRole("button", { name: "Confirm local finish" }).click();
		await expect(page.getByText("Finished locally · Private")).toBeVisible();
		await tab("History").click();
		await expect(
			page.getByRole("link", { name: "github.com/fixture/repo/pull/17" }),
		).toBeVisible();
		await tab("Reviews").click();
		await expect(row(pr)).toHaveCount(0);
		await expect(row(second).getByText("Running")).toBeVisible();
		await tab("Inbox").click();
		release(18);
		await expect
			.poll(async () => {
				const response = await page.request.get(
					new URL("/api/v1/reviews", address).href,
					{
						headers: { Authorization: `Bearer ${capability}` },
					},
				);
				const rows: Array<{ selection?: { url: string }; status: string }> =
					await response.json();
				return rows.find((row) => row.selection?.url === second)?.status;
			})
			.toBe("draft_ready");
		await expect(
			page.getByRole("heading", { name: "Review inbox" }),
		).toBeVisible();
		await tab("Reviews").click();
		await row(second).getByRole("link", { name: "Open draft" }).click();
		await page.getByLabel("GitHub action").selectOption("comment");
		await page.getByRole("button", { name: "Prepare preview" }).click();
		await page
			.getByRole("button", { name: "Confirm comment as @reviewer" })
			.click();
		await expect(
			page.getByRole("button", { name: "Recover publication" }),
		).toBeVisible();
		server.kill("SIGINT");
		await exited;
		server = launchServer();
		exited = once(server, "exit");
		const restarted = new URL(await launchAddress(server));
		await page.goto(new URL(`/reviews${restarted.hash}`, restarted).href);
		await expect(
			row(second).getByRole("link", { name: "Recover publication" }),
		).toBeVisible();
		await expect(
			row(third).getByRole("link", { name: "Open draft" }),
		).toBeVisible();
		await tab("Reviews").click();
		await row(second)
			.getByRole("link", { name: "Recover publication" })
			.click();
		await expect(
			page.getByRole("button", { name: "Recover publication" }),
		).toBeVisible();
	} finally {
		release(17);
		release(18);
		await page.close();
		server.kill("SIGINT");
		await exited;
		rmSync(cwd, { recursive: true, force: true });
	}
});
