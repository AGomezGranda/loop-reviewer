import type { components } from "@/api/schema";

type Selection = components["schemas"]["SelectionResponse"];

export function ReviewedRevision({ selection }: { selection: Selection }) {
	const rows = [
		["Account", `@${selection.account}`],
		["Target branch", selection.targetBranch],
		["Base tip", selection.baseTip],
		["Merge base", selection.mergeBase],
		["Head", selection.head],
	];
	return (
		<dl className="grid gap-x-6 gap-y-2 rounded-lg border bg-card px-4 py-3 text-sm sm:grid-cols-[max-content_minmax(0,1fr)]">
			{rows.map(([label, value]) => (
				<div className="contents" key={label}>
					<dt className="text-muted-foreground">{label}</dt>
					<dd>
						<code className="wrap-anywhere">{value}</code>
					</dd>
				</div>
			))}
		</dl>
	);
}
