import { type ChildProcess, execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import {
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
const launcher = join(repository, "tests/support/browser_review.py");
const pr = "https://github.com/fixture/repo/pull/17";

function args(...command: string[]) {
	return [
		"run",
		"--project",
		repository,
		"--locked",
		"python",
		launcher,
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

test("CLI draft edits are shared with the browser and the exact CLI publication preview", async ({
	page,
}) => {
	const cwd = mkdtempSync(join(tmpdir(), "loop-reviewer-smoke-"));
	const state = join(cwd, "state");
	mkdirSync(state, { recursive: true });
	const env = { ...process.env, XDG_STATE_HOME: state, XDG_CONFIG_HOME: cwd };
	const cli = (...command: string[]) =>
		execFileSync("uv", args(...command), { cwd, env, encoding: "utf8" });
	const initial = cli("review", pr);
	const estimate = "Estimated generation cost · $0.0234";
	expect(initial).toContain(estimate);
	expect(initial).toContain("Generation model: provider/model");
	const runId = initial.match(/^Run ID: (\d+)$/m)?.[1];
	expect(runId).toBeTruthy();
	if (!runId) throw new Error("CLI did not report a run ID");
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
		const model = page.getByLabel("Pi reviewer model");
		await model.fill("provider/browser-model");
		await page.getByRole("button", { name: "Save settings" }).click();
		await expect(
			page.getByText("Settings saved.", { exact: true }),
		).toBeVisible();
		const config = join(cwd, "loop-reviewer", "config.toml");
		const saved = readFileSync(config, "utf8");
		expect(saved).toContain('model = "provider/browser-model"');
		writeFileSync(config, saved.replace("browser-model", "file-model"));
		await expect(model).toHaveValue("provider/file-model", { timeout: 10_000 });
		await page.getByRole("link", { name: "Inbox", exact: true }).click();
		await page.setViewportSize({ width: 390, height: 844 });
		await page.getByLabel("Pull request URL").fill(pr);
		await page
			.getByRole("button", { name: "Open review", exact: true })
			.click();
		await expect(page).toHaveURL(new RegExp(`/drafts/${runId}$`));
		await expect(page.getByText("Original CLI feedback")).toBeVisible();
		await expect(page.getByText(estimate)).toBeVisible();
		await expect(
			page.getByText("provider/model", { exact: true }),
		).toBeVisible();

		await page.getByRole("button", { name: "Edit summary" }).click();
		const edited =
			"Browser-edited feedback\n\nThe second paragraph is preserved.";
		await page
			.getByRole("textbox", { name: "Summary", exact: true })
			.fill(edited);
		const history = page.getByRole("link", { name: "History", exact: true });
		await history.focus();
		await page.keyboard.press("Enter");
		const leaveDialog = page.getByRole("alertdialog");
		await expect(
			leaveDialog.getByRole("button", { name: "Keep editing" }),
		).toBeFocused();
		await page.keyboard.press("Escape");
		await expect(history).toBeFocused();
		await expect(
			page.getByRole("textbox", { name: "Summary", exact: true }),
		).toHaveValue(edited);
		expect(
			await page.evaluate(() => document.documentElement.scrollWidth),
		).toBe(390);
		await page.getByRole("button", { name: "Save changes" }).click();
		await expect(
			page.getByRole("status").filter({ hasText: "Draft saved at version 2." }),
		).toBeVisible();
		await expect(page.getByText(estimate)).toBeVisible();

		await page.getByLabel("GitHub action").selectOption("comment");
		await page.getByRole("button", { name: "Prepare preview" }).click();
		const body = page.getByLabel("Exact review body");
		await expect(body).toContainText("Browser-edited feedback");
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
		expect(cliPreview).not.toContain(estimate);
		const normalize = (text: string) =>
			text.replace(/<!-- loop-reviewer:[a-f\d-]+ -->/, "<!-- marker -->");
		expect(normalize((await body.textContent()) ?? "")).toBe(
			normalize(cliBody ?? ""),
		);
		expect(
			await page.getByLabel("Inline body for example.py:8").textContent(),
		).toBe(
			cliPreview.match(
				/----- BEGIN INLINE BODY -----\n([\s\S]*?)\n----- END INLINE BODY -----/,
			)?.[1],
		);
		await page.getByRole("link", { name: "Make a fresh preview" }).click();
		await page.setViewportSize({ width: 1280, height: 900 });
		const finish = page.getByRole("button", { name: "Finish locally" });
		await finish.focus();
		await page.keyboard.press("Enter");
		await expect(
			page.getByRole("alertdialog").getByRole("button", { name: "Cancel" }),
		).toBeFocused();
		await page.keyboard.press("Escape");
		await expect(finish).toBeFocused();
		await finish.click();
		await page.getByRole("button", { name: "Confirm local finish" }).click();
		await expect(page).toHaveURL(new RegExp(`/history/${runId}$`));
		await expect(page.getByText("Finished locally · Private")).toBeVisible();
		await page.reload();
		await expect(page.getByText("Finished locally · Private")).toBeVisible();
		await expect(page.getByText(edited)).toBeVisible();
		await expect(page.getByText(estimate)).toBeVisible();
		await expect(
			page.getByText("provider/model", { exact: true }),
		).toBeVisible();
		const completed = cli("review", pr);
		expect(completed).toContain(estimate);
		expect(completed).toContain("Generation model: provider/model");
		expect(readFileSync(join(state, "events"), "utf8")).toBe("review\n");
	} finally {
		await page.close();
		server.kill("SIGINT");
		await exited;
		rmSync(cwd, { recursive: true, force: true });
	}
});
