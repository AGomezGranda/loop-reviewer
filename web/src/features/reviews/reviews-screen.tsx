import { useMutation, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { errorMessage } from "@/api/client";
import { Button } from "@/components/ui/button";
import { retryReview, reviewsQuery, revisionNotice } from "./reviews-queries";

const labels = {
	queued: "Queued",
	running: "Running",
	draft_ready: "Draft ready",
	failed: "Failed",
	interrupted: "Interrupted",
	recovery_required: "Publication unconfirmed",
	status_unavailable: "Status unavailable",
	completed: "Completed",
};

const retryGuidance = {
	inbox: "Choose the current row in the Inbox to launch again.",
	url: "Submit the pull request URL again to relaunch.",
	retry:
		"Check the saved attempt before retrying, or submit the PR URL to review the current revision.",
};

function RetryReview({
	runId,
	rerun = false,
	onRetried,
}: {
	runId: string;
	rerun?: boolean;
	onRetried: () => void;
}) {
	const retry = useMutation({
		mutationFn: () => retryReview(runId),
		onSettled: onRetried,
	});
	return (
		<div className="space-y-1">
			<Button
				type="button"
				variant="outline"
				disabled={retry.isPending}
				onClick={() => retry.mutate()}
			>
				{retry.isPending
					? "Submitting…"
					: rerun
						? "Re-run analysis"
						: "Retry review"}
			</Button>
			{retry.error && (
				<p role="alert" className="text-sm text-destructive">
					{errorMessage(retry.error)}
				</p>
			)}
		</div>
	);
}

export function ReviewsScreen({
	draftLink,
	recoveryLink,
	historyLink,
	onRetried,
}: {
	draftLink: (id: string, notice: string | undefined) => ReactNode;
	recoveryLink: (id: string) => ReactNode;
	historyLink: (id: string) => ReactNode;
	onRetried: () => void;
}) {
	const reviews = useQuery(reviewsQuery);
	return (
		<div className="space-y-4">
			<h1 className="text-2xl font-semibold tracking-tight">Reviews</h1>
			{reviews.isPending && <p role="status">Loading reviews…</p>}
			{reviews.error && (
				<p role="alert" className="text-sm text-destructive">
					{errorMessage(reviews.error)}
				</p>
			)}
			{reviews.data?.length === 0 && (
				<p className="text-sm text-muted-foreground">No reviews to show.</p>
			)}
			{!!reviews.data?.length && (
				<ul className="divide-y rounded-lg border bg-card">
					{reviews.data.map((row) => {
						const url = row.kind === "saved" ? row.selection.url : row.url;
						const notice =
							row.kind === "saved" ? revisionNotice(row.changes) : undefined;
						return (
							<li
								key={row.kind === "saved" ? row.runId : row.launchId}
								className="flex flex-wrap items-start justify-between gap-3 px-4 py-3"
							>
								<div className="min-w-0 space-y-1">
									<p className="text-sm font-medium">
										{row.kind === "saved" &&
										row.previousDraft &&
										row.status === "draft_ready"
											? "Previous draft"
											: labels[row.status]}
									</p>
									<a
										href={url}
										target="_blank"
										rel="noreferrer"
										className="wrap-anywhere font-mono text-sm text-primary underline underline-offset-4"
									>
										{url}
									</a>
									{row.selection && (
										<p className="wrap-anywhere text-xs text-muted-foreground">
											<code>@{row.selection.account}</code> ·{" "}
											<code>{row.selection.targetBranch}</code> · head{" "}
											<code title={row.selection.head}>
												{row.selection.head.slice(0, 12)}
											</code>{" "}
											· merge base{" "}
											<code title={row.selection.mergeBase}>
												{row.selection.mergeBase.slice(0, 12)}
											</code>
										</p>
									)}
									{notice && <p className="text-sm">{notice}</p>}
									{row.kind === "saved" && row.status === "failed" && (
										<p className="wrap-anywhere text-sm text-destructive">
											{row.failure}
										</p>
									)}
									{row.kind === "launch" && row.error && (
										<>
											<p className="wrap-anywhere text-sm text-destructive">
												{row.error.message}
											</p>
											<p className="text-sm text-muted-foreground">
												{retryGuidance[row.source]}
											</p>
										</>
									)}
								</div>
								{row.kind === "saved" &&
									row.status === "draft_ready" &&
									draftLink(row.runId, notice)}
								{row.kind === "saved" &&
									row.status === "recovery_required" &&
									row.publicationId &&
									recoveryLink(row.publicationId)}
								{row.kind === "saved" && row.retryAction && (
									<RetryReview
										runId={row.runId}
										rerun={row.retryAction === "rerun"}
										onRetried={onRetried}
									/>
								)}
								{row.kind === "launch" &&
									row.completedRunId &&
									historyLink(row.completedRunId)}
							</li>
						);
					})}
				</ul>
			)}
		</div>
	);
}
