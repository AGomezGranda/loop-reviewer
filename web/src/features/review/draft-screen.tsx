import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy } from "lucide-react";
import { type ReactNode, useCallback, useRef, useState } from "react";
import { useBeforeUnload, useBlocker } from "react-router";
import { ApiError, errorMessage } from "@/api/client";
import { FindingCard } from "@/components/finding-card";
import { ReviewedRevision } from "@/components/reviewed-revision";
import {
	AlertDialog,
	AlertDialogAction,
	AlertDialogCancel,
	AlertDialogContent,
	AlertDialogDescription,
	AlertDialogFooter,
	AlertDialogHeader,
	AlertDialogTitle,
	AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { DraftEditor, type Editor } from "./draft-editor";
import {
	copyDraft,
	type Draft,
	draftQuery,
	type Edit,
	finishDraft,
	saveDraft,
} from "./review-queries";

type DraftIntent =
	| { kind: "edit"; runId: string; edit: Edit }
	| { kind: "finish"; runId: string; version: string };

export function DraftScreen({
	runId,
	onFinished,
	onChanged,
	historyLink,
	recoveryLink,
	previewControl,
	openNotice,
}: {
	runId: string;
	onFinished: (id: string) => void;
	onChanged: () => void;
	historyLink: (id: string) => ReactNode;
	recoveryLink: (id: string) => ReactNode;
	previewControl?: (draft: Draft, disabled: boolean) => ReactNode;
	openNotice?: string;
}) {
	const [editor, setEditor] = useState<Editor | null>(null);
	const [message, setMessage] = useState("");
	const [finishVersion, setFinishVersion] = useState<string | null>(null);
	const [copyFallback, setCopyFallback] = useState<string | null>(null);
	const cache = useQueryClient();
	const query = draftQuery(runId);
	const draft = useQuery(query);
	const blocker = useBlocker(Boolean(editor?.dirty));
	const leaveFocus = useRef<HTMLElement | null>(null);
	useBeforeUnload(
		useCallback(
			(event) => {
				if (editor?.dirty) event.preventDefault();
			},
			[editor?.dirty],
		),
	);

	const update = useMutation({
		mutationFn: async (intent: DraftIntent) =>
			intent.kind === "finish"
				? {
						kind: "finished" as const,
						completion: await finishDraft(intent.runId, intent.version),
					}
				: {
						kind: "saved" as const,
						draft: await saveDraft(intent.runId, intent.edit),
					},
		onMutate: () => {
			setMessage("");
			setCopyFallback(null);
		},
		onSuccess: async (result) => {
			if (result.kind === "finished") {
				void cache.invalidateQueries({ queryKey: query.queryKey });
				onChanged();
				return;
			}
			await cache.cancelQueries({ queryKey: query.queryKey });
			cache.setQueryData(query.queryKey, result.draft);
			setEditor(null);
			setMessage(`Draft saved at version ${result.draft.version}.`);
			void cache.invalidateQueries({ queryKey: query.queryKey });
			onChanged();
		},
		onError: (error) => {
			if (
				error instanceof ApiError &&
				["draft_changed", "review_completed", "recovery_required"].includes(
					error.details.code,
				)
			) {
				void cache.invalidateQueries({ queryKey: query.queryKey });
			}
		},
	});

	const copy = useMutation({
		mutationFn: async () => {
			const { markdown } = await copyDraft(runId);
			try {
				await navigator.clipboard.writeText(markdown);
				setMessage("Saved feedback copied.");
				setCopyFallback(null);
			} catch {
				setMessage("Clipboard unavailable. Select the Markdown to copy it.");
				setCopyFallback(markdown);
			}
		},
	});

	const error = update.error ?? draft.error ?? copy.error;
	const saved = draft.data;
	const readDetails =
		draft.error instanceof ApiError ? draft.error.details : undefined;
	const updateDetails =
		update.error instanceof ApiError ? update.error.details : undefined;
	const copyDetails =
		copy.error instanceof ApiError ? copy.error.details : undefined;
	const completedId =
		readDetails?.completedRunId ??
		updateDetails?.completedRunId ??
		copyDetails?.completedRunId;
	const publicationId = completedId
		? undefined
		: (saved?.publicationId ??
			readDetails?.publicationId ??
			updateDetails?.publicationId ??
			copyDetails?.publicationId);
	const stale =
		editor &&
		(editor.version !== saved?.version ||
			completedId ||
			(update.error instanceof ApiError &&
				update.error.details.code === "draft_changed"));
	const available = !completedId && !publicationId && !draft.isError;
	function edit(target: Editor | null) {
		update.reset();
		setEditor(target);
		setMessage("");
	}
	return (
		<div className="space-y-4">
			<div className="flex flex-wrap items-end justify-between gap-3">
				<div className="min-w-0">
					<h1 className="text-2xl font-semibold tracking-tight">
						Private draft
					</h1>
					{saved && (
						<p className="mt-1 text-sm text-muted-foreground">
							<code>
								Run {saved.runId} · Version {saved.version}
							</code>
						</p>
					)}
				</div>
				{saved && (
					<Button
						variant="outline"
						type="button"
						disabled={copy.isPending || Boolean(completedId)}
						onClick={() => copy.mutate()}
						className="w-full sm:w-auto"
					>
						<Copy aria-hidden="true" /> Copy feedback
					</Button>
				)}
			</div>
			{openNotice && (
				<p role="status" className="text-sm text-muted-foreground">
					{openNotice}
				</p>
			)}
			{draft.isPending && <p role="status">Loading saved draft…</p>}
			{update.isPending && (
				<p role="status" className="text-sm text-muted-foreground">
					Saving review…
				</p>
			)}
			{message && (
				<p role="status" className="text-sm text-primary">
					{message}
				</p>
			)}
			{error && !stale && (
				<p role="alert" className="text-sm text-destructive">
					{errorMessage(error)}
				</p>
			)}
			{completedId && historyLink(completedId)}
			{publicationId && (
				<div className="space-y-2 rounded-lg border bg-card p-4">
					<p className="text-sm">
						Recover publication{" "}
						<code className="wrap-anywhere">{publicationId}</code> to unlock
						this review.
					</p>
					{recoveryLink(publicationId)}
				</div>
			)}
			{copyFallback !== null && (
				<Textarea
					aria-label="Markdown to copy"
					readOnly
					value={copyFallback}
					onFocus={(event) => event.currentTarget.select()}
				/>
			)}
			{saved && (
				<>
					<a
						className="block wrap-anywhere text-sm text-primary underline"
						href={saved.selection.url}
						target="_blank"
						rel="noreferrer"
					>
						{saved.selection.url}
					</a>
					<ReviewedRevision selection={saved.selection} />
					{editor && (
						<DraftEditor
							editor={editor}
							frozen={Boolean(stale || !available)}
							busy={update.isPending}
							onChange={setEditor}
							onSave={(id, body) =>
								update.mutate({ kind: "edit", runId: id, edit: body })
							}
							onDiscard={() => edit(null)}
						/>
					)}
					<section
						className="space-y-3 rounded-lg border bg-card p-4"
						aria-label="Saved summary"
					>
						<div className="flex flex-wrap items-center justify-between gap-2">
							<h2 className="font-medium">Saved summary</h2>
							<Button
								type="button"
								variant="outline"
								size="sm"
								disabled={Boolean(editor) || !available || update.isPending}
								onClick={() =>
									edit({
										kind: "edit_summary",
										runId,
										version: saved.version,
										text: saved.feedback.summary,
										dirty: false,
									})
								}
							>
								Edit summary
							</Button>
						</div>
						<p className="text-sm text-muted-foreground">
							Recommendation{" "}
							<strong className="font-medium text-foreground">
								{saved.feedback.verdict === "approved" ? "Approve" : "Comment"}
							</strong>
						</p>
						<p className="whitespace-pre-wrap wrap-anywhere text-sm">
							{saved.feedback.summary}
						</p>
					</section>
					<section className="space-y-3" aria-label="Saved findings">
						<h2 className="font-medium">Findings</h2>
						{saved.feedback.findings.length === 0 && (
							<p className="text-sm text-muted-foreground">No findings.</p>
						)}
						{saved.feedback.findings.map((finding) => (
							<FindingCard
								key={finding.number}
								finding={finding}
								actions={
									<div className="flex flex-wrap gap-2">
										<Button
											type="button"
											variant="outline"
											size="sm"
											disabled={
												Boolean(editor) || !available || update.isPending
											}
											onClick={() =>
												edit({
													kind: "edit_finding",
													runId,
													version: saved.version,
													number: finding.number,
													condition: finding.condition,
													consequence: finding.consequence,
													dirty: false,
												})
											}
										>
											Edit finding {finding.number}
										</Button>
										<Button
											type="button"
											variant="ghost"
											size="sm"
											disabled={
												Boolean(editor) || !available || update.isPending
											}
											onClick={() =>
												update.mutate({
													kind: "edit",
													runId,
													edit: {
														kind: "drop_finding",
														number: finding.number,
														version: saved.version,
													},
												})
											}
										>
											Remove finding {finding.number}
										</Button>
									</div>
								}
							/>
						))}
					</section>
					{available && (
						<div className="space-y-3 border-t pt-4">
							{previewControl?.(saved, Boolean(editor) || update.isPending)}
							<AlertDialog
								open={finishVersion !== null}
								onOpenChange={(open) =>
									setFinishVersion(open ? saved.version : null)
								}
							>
								<AlertDialogTrigger asChild>
									<Button
										variant="outline"
										type="button"
										disabled={Boolean(editor) || update.isPending}
										className="w-full sm:w-auto"
									>
										Finish locally
									</Button>
								</AlertDialogTrigger>
								<AlertDialogContent>
									<AlertDialogHeader>
										<AlertDialogTitle>
											Finish the initial review locally?
										</AlertDialogTitle>
										<AlertDialogDescription>
											The saved feedback will appear in private history. This
											action sends nothing to GitHub.
										</AlertDialogDescription>
									</AlertDialogHeader>
									<AlertDialogFooter>
										<AlertDialogCancel>Cancel</AlertDialogCancel>
										<AlertDialogAction
											onClick={() => {
												if (finishVersion)
													update.mutate(
														{ kind: "finish", runId, version: finishVersion },
														{
															onSuccess: (result) => {
																if (result.kind === "finished")
																	onFinished(result.completion.runId);
															},
														},
													);
											}}
										>
											Confirm local finish
										</AlertDialogAction>
									</AlertDialogFooter>
								</AlertDialogContent>
							</AlertDialog>
						</div>
					)}
				</>
			)}
			<AlertDialog
				open={blocker.state === "blocked"}
				onOpenChange={(open) => {
					if (!open && blocker.state === "blocked") blocker.reset();
				}}
			>
				<AlertDialogContent
					onOpenAutoFocus={() => {
						leaveFocus.current = document.activeElement as HTMLElement | null;
					}}
					onCloseAutoFocus={(event) => {
						event.preventDefault();
						leaveFocus.current?.focus();
					}}
				>
					<AlertDialogHeader>
						<AlertDialogTitle>Leave with unsaved feedback?</AlertDialogTitle>
						<AlertDialogDescription>
							Leave this draft and discard the text you entered, or stay to keep
							editing.
						</AlertDialogDescription>
					</AlertDialogHeader>
					<AlertDialogFooter>
						<AlertDialogCancel>Keep editing</AlertDialogCancel>
						<AlertDialogAction onClick={() => blocker.proceed?.()}>
							Leave draft
						</AlertDialogAction>
					</AlertDialogFooter>
				</AlertDialogContent>
			</AlertDialog>
		</div>
	);
}
