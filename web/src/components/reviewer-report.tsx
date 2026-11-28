import type { components } from "@/api/schema";
import { ReviewRecommendation } from "@/components/review-recommendation";

type Report = components["schemas"]["HostedReport"];

export function ReviewerReport({ report }: { report: Report | null }) {
	if (!report)
		return (
			<p className="text-sm text-muted-foreground">
				Coverage unavailable for this legacy review.
			</p>
		);
	if (report.status === "failed") return null;
	const unfinished = report.file_coverage.filter(
		(row) => row.outcome === "unfinished",
	).length;
	const excluded = report.file_coverage.filter(
		(row) => row.outcome === "excluded",
	).length;
	const usage = report.usage;
	const threads = new Map(
		report.discussion_references.map((item) => [item.item_id, item.permalink]),
	);
	return (
		<section
			className="space-y-2 rounded-lg border bg-card p-4"
			aria-label="Original reviewer report"
		>
			<h2 className="font-medium">Original reviewer report · Private</h2>
			<ReviewRecommendation verdict={report.recommendation} />
			<p className="text-sm">
				{report.status === "partial" ? "Partial review" : "Complete review"} ·{" "}
				<code>{report.scope}</code> · Stop: {report.stop_reason} · Discussion:{" "}
				{report.discussion_status}
			</p>
			<p className="text-sm text-muted-foreground">
				{usage.model_calls} model calls · {usage.input_tokens} input /{" "}
				{usage.output_tokens} output tokens · {usage.estimated_cost_usd_micros}{" "}
				USD micros estimated · {usage.unaccounted_reserved_usd_micros} held ·
				Accounting fault: {usage.accounting_fault ? "yes" : "no"}
			</p>
			<details>
				<summary className="cursor-pointer text-sm">
					File coverage: {report.file_coverage.length - unfinished - excluded}{" "}
					complete, {unfinished} unfinished, {excluded} excluded
				</summary>
				<ul className="space-y-1 pt-2 text-sm">
					{report.file_coverage.map((row) => (
						<li key={row.file_id} className="wrap-anywhere">
							<code>{(row.new ?? row.old)?.path}</code> · {row.outcome} ·{" "}
							{row.highest_completed_check}
							{row.reason && ` (${row.reason})`}
						</li>
					))}
				</ul>
			</details>
			<details>
				<summary className="cursor-pointer text-sm">
					Original findings ({report.findings.length})
				</summary>
				<ol className="space-y-1 pt-2 text-sm">
					{report.findings.map((item) => (
						<li key={item.id}>
							<code>{item.id}</code> · {item.severity} · {item.trigger} —{" "}
							{item.consequence}
						</li>
					))}
				</ol>
			</details>
			<details>
				<summary className="cursor-pointer text-sm">
					Private leads, evidence, and existing-thread references
				</summary>
				<ul className="space-y-2 pt-2 text-sm">
					{report.private_leads.map((lead) => (
						<li key={lead.id}>
							<code>{lead.id}</code> · {lead.trigger} — {lead.consequence}.
							Missing: {lead.missing_fact}
						</li>
					))}
					{report.evidence_index.map((item) => (
						<li key={item.id}>
							<code>{item.id}</code>
							{item.kind === "source" ? (
								<pre className="whitespace-pre-wrap wrap-anywhere">
									{item.text}
								</pre>
							) : (
								<>
									{" "}
									· {item.result} · {item.message}
								</>
							)}
						</li>
					))}
					{report.linked_existing_threads.map((link) => {
						const url = threads.get(link.thread_id);
						return (
							<li key={link.candidate_id}>
								Existing thread{" "}
								{url?.startsWith("https://") ? (
									<a
										href={url}
										target="_blank"
										rel="noreferrer"
										className="text-primary underline"
									>
										<code>{link.thread_id}</code>
									</a>
								) : (
									<code>{link.thread_id}</code>
								)}
							</li>
						);
					})}
				</ul>
			</details>
		</section>
	);
}
