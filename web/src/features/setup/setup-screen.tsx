import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderGit2, GitFork, Plus, Trash2 } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { ApiError, errorMessage, request } from "@/api/client";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

type Configuration = components["schemas"]["InboxConfiguration"];
const configurationKey = ["inbox-configuration"];

function repositoryRow(url: string) {
	return { id: crypto.randomUUID(), url };
}

function SetupForm({
	initial,
	onSaved,
	onReload,
}: {
	initial: Configuration;
	onSaved: (saved: Configuration) => void;
	onReload: () => Promise<unknown>;
}) {
	const pristine = useMemo(
		() => ({
			version: initial.settingsVersion,
			revision: initial.configRevision,
			model: initial.reviewerModel ?? "",
			project: initial.jiraProjectKeys.join(", "),
			repositories: (initial.repositoryUrls.length
				? initial.repositoryUrls
				: [""]
			).map(repositoryRow),
		}),
		[initial],
	);
	const [edits, setEdits] = useState<typeof pristine | null>(null);
	const { version, revision, model, project, repositories } = edits ?? pristine;
	const hasInbox =
		Boolean(pristine.project || project.trim()) ||
		repositories.some((row) => row.url.trim());
	const [focusRow, setFocusRow] = useState<string>();
	const save = useMutation({
		mutationFn: (body: components["schemas"]["ConfigureInboxRequest"]) =>
			request("post", "/api/v1/inbox/configuration", { body }),
		onSuccess: (saved) => {
			setEdits(null);
			onSaved(saved);
		},
	});
	const reload = useMutation({
		mutationFn: onReload,
		onSuccess: () => {
			setEdits(null);
			save.reset();
		},
	});
	const conflict =
		version !== initial.settingsVersion ||
		revision !== initial.configRevision ||
		(save.error instanceof ApiError &&
			save.error.details.code === "inbox_changed");
	const busy = save.isPending || reload.isPending;
	function change(
		fields: Partial<
			Pick<typeof pristine, "model" | "project" | "repositories">
		>,
	) {
		setEdits({ version, revision, model, project, repositories, ...fields });
		if (!conflict) save.reset();
	}

	return (
		<form
			className="overflow-hidden rounded-lg border bg-card"
			onSubmit={(event) => {
				event.preventDefault();
				if (busy || conflict) return;
				save.mutate({
					settingsVersion: version,
					configRevision: revision,
					reviewerModel: model.trim(),
					jiraProjectKeys: hasInbox ? [project.trim()] : [],
					repositoryUrls: hasInbox
						? repositories.map((row) => row.url.trim())
						: [],
				});
			}}
		>
			<fieldset
				className="min-w-0"
				aria-labelledby="repositories-heading"
				disabled={busy}
			>
				<div className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/40 px-4 py-3">
					<h2
						id="repositories-heading"
						className="flex items-center gap-2 font-semibold"
					>
						<FolderGit2 className="size-4 text-primary" aria-hidden="true" />
						Watched repositories
					</h2>
					<Button
						type="button"
						variant="outline"
						size="sm"
						onClick={() => {
							const row = repositoryRow("");
							change({ repositories: [...repositories, row] });
							setFocusRow(row.id);
						}}
					>
						<Plus aria-hidden="true" /> Add repository
					</Button>
				</div>
				<div className="divide-y">
					{repositories.map((row, index) => (
						<div
							key={row.id}
							className="flex items-center gap-2 px-3 py-2 sm:px-4"
						>
							<GitFork
								className="hidden size-4 shrink-0 text-muted-foreground sm:block"
								aria-hidden="true"
							/>
							<Input
								type="url"
								aria-label={`Repository URL ${index + 1}`}
								className="min-w-0 bg-background font-mono"
								placeholder="https://github.com/OWNER/REPO"
								required={hasInbox}
								spellCheck={false}
								ref={(input) => {
									if (input && row.id === focusRow) {
										input.focus();
										setFocusRow(undefined);
									}
								}}
								value={row.url}
								onChange={(event) => {
									change({
										repositories: repositories.map((item) =>
											item.id === row.id
												? { ...row, url: event.target.value }
												: item,
										),
									});
								}}
								onPaste={(event) => {
									const urls = event.clipboardData
										.getData("text")
										.trim()
										.split(/\r?\n/)
										.map((line) => line.trim())
										.filter(Boolean);
									if (urls.length < 2) return;
									event.preventDefault();
									change({
										repositories: repositories.flatMap((item) =>
											item.id === row.id ? urls.map(repositoryRow) : [item],
										),
									});
								}}
							/>
							<Button
								type="button"
								variant="ghost"
								size="icon"
								title="Remove repository"
								className="text-muted-foreground hover:text-destructive"
								disabled={repositories.length === 1}
								aria-label={`Remove repository ${index + 1}`}
								onClick={() => {
									change({
										repositories: repositories.filter(
											(item) => item.id !== row.id,
										),
									});
									setFocusRow(
										repositories[index + 1]?.id ?? repositories[index - 1]?.id,
									);
								}}
							>
								<Trash2 aria-hidden="true" />
							</Button>
						</div>
					))}
				</div>
			</fieldset>
			<div className="flex flex-wrap items-center justify-between gap-3 border-t px-4 py-3">
				<div>
					<label className="text-sm font-medium" htmlFor="jira-project">
						Jira project key
					</label>
					<p id="shared-project" className="text-xs text-muted-foreground">
						Applies to all watched repositories.
					</p>
				</div>
				<Input
					id="jira-project"
					aria-describedby="shared-project"
					className="w-32 bg-background font-mono"
					placeholder="ABC"
					spellCheck={false}
					required={hasInbox}
					disabled={busy}
					value={project}
					onChange={(event) => {
						change({ project: event.target.value });
					}}
				/>
			</div>

			<div className="flex flex-wrap items-center justify-between gap-3 border-t px-4 py-3">
				<div>
					<label className="text-sm font-medium" htmlFor="reviewer-model">
						Pi reviewer model
					</label>
					<p id="model-format" className="text-xs text-muted-foreground">
						Use PROVIDER/MODEL for new drafts.
					</p>
				</div>
				<Input
					id="reviewer-model"
					aria-describedby="model-format"
					className="w-64 bg-background font-mono"
					placeholder="PROVIDER/MODEL"
					spellCheck={false}
					disabled={busy}
					value={model}
					onChange={(event) => {
						change({ model: event.target.value });
					}}
				/>
			</div>

			<div className="flex flex-wrap items-center gap-3 border-t bg-muted/40 px-4 py-3">
				<Button type="submit" disabled={busy || conflict}>
					Save settings
				</Button>
				{save.isPending && (
					<p role="status" className="text-sm">
						Saving settings…
					</p>
				)}
				{conflict && (
					<Button
						type="button"
						variant="outline"
						disabled={busy}
						onClick={() => reload.mutate()}
					>
						{reload.isPending
							? "Reloading settings…"
							: "Reload settings and discard edits"}
					</Button>
				)}
				{save.data &&
					save.data.configRevision === initial.configRevision &&
					!conflict && (
						<p role="status" className="text-sm">
							{hasInbox
								? `Watching ${repositories.length} ${repositories.length === 1 ? "repository" : "repositories"} for ${project}.`
								: "Settings saved."}
						</p>
					)}
			</div>
			{(conflict || save.error || reload.error) && (
				<p role="alert" className="border-t px-4 py-3 text-sm text-destructive">
					{reload.error || (save.error && !conflict)
						? errorMessage(reload.error ?? save.error)
						: "Saved settings changed. Reload the latest settings to continue."}
				</p>
			)}
		</form>
	);
}

export function SetupScreen({
	checks,
	onSaved,
}: {
	checks?: ReactNode;
	onSaved: () => void;
}) {
	const cache = useQueryClient();
	const configuration = useQuery({
		queryKey: configurationKey,
		refetchInterval: 5_000,
		queryFn: ({ signal }) =>
			request("get", "/api/v1/inbox/configuration", { signal }),
	});

	return (
		<div className="space-y-4">
			<h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
			{configuration.error && (
				<p role="alert" className="text-destructive">
					{errorMessage(configuration.error)}
				</p>
			)}
			{configuration.data ? (
				<SetupForm
					initial={configuration.data}
					onReload={() => configuration.refetch({ throwOnError: true })}
					onSaved={(saved) => {
						void cache.cancelQueries({ queryKey: configurationKey });
						cache.setQueryData(configurationKey, saved);
						onSaved();
					}}
				/>
			) : (
				!configuration.error && <p role="status">Loading setup…</p>
			)}
			{checks}
		</div>
	);
}
