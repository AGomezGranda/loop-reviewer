import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { errorMessage } from "@/api/client";
import { FindingCard } from "@/components/finding-card";
import { GenerationCost } from "@/components/generation-cost";
import { ReviewRecommendation } from "@/components/review-recommendation";
import { ReviewedRevision } from "@/components/reviewed-revision";
import { ReviewerReport } from "@/components/reviewer-report";
import { Button } from "@/components/ui/button";
import { completionQuery, historyQuery } from "./history-queries";

export function HistoryScreen({
	completionLink,
	unresolved,
}: {
	completionLink: (id: string, text: string) => ReactNode;
	unresolved: ReactNode;
}) {
	const history = useInfiniteQuery(historyQuery);
	const completions =
		history.data?.pages.flatMap((page) => page.completions) ?? [];
	return (
		<div className="space-y-4">
			<h1 className="text-2xl font-semibold tracking-tight">Review history</h1>
			{unresolved}
			{history.isPending && <p role="status">Loading completions…</p>}
			{history.error && (
				<p role="alert" className="text-destructive">
					{errorMessage(history.error)}
				</p>
			)}
			{history.isSuccess && completions.length === 0 && (
				<p className="text-sm text-muted-foreground">
					No reviews have been completed.
				</p>
			)}
			{completions.length > 0 && (
				<ul className="divide-y rounded-lg border bg-card">
					{completions.map((item) => (
						<li key={item.runId} className="space-y-1 px-4 py-3">
							{completionLink(
								item.runId,
								item.selection.url.replace(/^https:\/\//, ""),
							)}
							<p className="text-sm text-muted-foreground">
								{item.disposition === "published"
									? "Published to GitHub"
									: "Finished locally"}{" "}
								· <code>@{item.selection.account}</code>
							</p>
						</li>
					))}
				</ul>
			)}
			{history.hasNextPage && (
				<Button
					type="button"
					variant="outline"
					disabled={history.isFetchingNextPage}
					onClick={() => void history.fetchNextPage()}
				>
					{history.isFetchingNextPage ? "Loading…" : "Load more"}
				</Button>
			)}
		</div>
	);
}

export function CompletionScreen({
	runId,
	backLink,
}: {
	runId: string;
	backLink: ReactNode;
}) {
	const completion = useQuery(completionQuery(runId));
	const item = completion.data;
	return (
		<div className="space-y-4">
			{backLink}
			<h1 className="text-2xl font-semibold tracking-tight">
				Completed review
			</h1>
			{completion.isPending && <p role="status">Loading completion…</p>}
			{completion.error && (
				<p role="alert" className="text-destructive">
					{errorMessage(completion.error)}
				</p>
			)}
			{item && (
				<>
					<div className="space-y-1">
						<p className="font-medium text-primary">
							{item.receipt
								? "Published to GitHub"
								: "Finished locally · Private"}
						</p>
						<p className="text-sm text-muted-foreground">
							<code>
								Run {item.runId} · Version {item.version}
							</code>
						</p>
						<a
							className="block wrap-anywhere text-sm text-primary underline"
							href={item.selection.url}
							target="_blank"
							rel="noreferrer"
						>
							{item.selection.url}
						</a>
						{item.receipt && (
							<a
								className="text-sm text-primary underline"
								href={`${item.selection.url}#pullrequestreview-${item.receipt.reviewId}`}
								target="_blank"
								rel="noreferrer"
							>
								{item.receipt.action === "approve"
									? "GitHub approval"
									: "GitHub comment"}{" "}
								<code>{item.receipt.reviewId}</code>
							</a>
						)}
					</div>
					<ReviewedRevision selection={item.selection} />
					<GenerationCost {...item} />
					<ReviewerReport report={item.report} />
					<section className="space-y-2 rounded-lg border bg-card p-4">
						<h2 className="font-medium">Final feedback</h2>
						{!item.report && (
							<ReviewRecommendation verdict={item.feedback.verdict} />
						)}
						<p className="whitespace-pre-wrap wrap-anywhere text-sm">
							{item.feedback.summary}
						</p>
					</section>
					{item.feedback.findings.map((finding) => (
						<FindingCard finding={finding} key={finding.number} />
					))}
				</>
			)}
		</div>
	);
}
