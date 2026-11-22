import type { ReactNode } from "react";
import type { components } from "@/api/schema";

export type Finding = components["schemas"]["FindingResponse"];

export function FindingCard({
	finding,
	actions,
}: {
	finding: Finding;
	actions?: ReactNode;
}) {
	return (
		<article className="space-y-3 rounded-lg border bg-card p-4">
			<div className="flex flex-wrap items-center justify-between gap-2">
				<h3 className="font-medium">
					Finding {finding.number}{" "}
					<span className="ml-2 text-xs text-muted-foreground capitalize">
						{finding.severity} severity
					</span>
				</h3>
				{actions}
			</div>
			{finding.path && (
				<code className="block wrap-anywhere text-sm text-primary">
					{finding.path}:{finding.line}
				</code>
			)}
			<div className="grid gap-3 text-sm">
				<div>
					<p className="text-xs text-muted-foreground">Condition</p>
					<p className="whitespace-pre-wrap wrap-anywhere">
						{finding.condition}
					</p>
				</div>
				<div>
					<p className="text-xs text-muted-foreground">Consequence</p>
					<p className="whitespace-pre-wrap wrap-anywhere">
						{finding.consequence}
					</p>
				</div>
			</div>
		</article>
	);
}
