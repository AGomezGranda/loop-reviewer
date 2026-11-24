import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
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
