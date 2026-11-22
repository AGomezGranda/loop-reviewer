import { ReviewedRevision } from "@/components/reviewed-revision";
import type { Preview } from "./publication-queries";

export function PublicationPreview({ preview }: { preview: Preview }) {
	return (
		<div className="space-y-4">
			<div className="space-y-1 text-sm">
				<p>
					Action{" "}
					<strong className="text-primary">
						{preview.action === "approve" ? "Approve" : "Comment"}
					</strong>
				</p>
				<p>
					Destination{" "}
					<a
						className="wrap-anywhere text-primary underline"
						href={preview.selection.url}
						target="_blank"
						rel="noreferrer"
					>
						{preview.selection.url}
					</a>
				</p>
				<p className="text-muted-foreground">
					<code>
						Run {preview.runId} · Version {preview.version}
					</code>
				</p>
			</div>
			<ReviewedRevision selection={preview.selection} />
			<section aria-label="Review body" className="space-y-2">
				<h2 className="font-medium">Review body</h2>
				<figure aria-label="Exact review body">
					<pre className="rounded-lg border bg-card p-4 font-mono text-sm whitespace-pre-wrap wrap-anywhere">
						{preview.body}
					</pre>
				</figure>
			</section>
			{preview.comments.length > 0 && (
				<section aria-label="Inline comments" className="space-y-3">
					<h2 className="font-medium">Inline comments</h2>
					{preview.comments.map((comment) => (
						<div
							key={`${comment.path}:${comment.line}`}
							className="rounded-lg border bg-card"
						>
							<h3 className="border-b px-4 py-2 text-sm wrap-anywhere">
								<code>
									{comment.path}:{comment.line}
								</code>
							</h3>
							<figure
								aria-label={`Inline body for ${comment.path}:${comment.line}`}
							>
								<pre className="p-4 font-mono text-sm whitespace-pre-wrap wrap-anywhere">
									{comment.body}
								</pre>
							</figure>
						</div>
					))}
				</section>
			)}
		</div>
	);
}
