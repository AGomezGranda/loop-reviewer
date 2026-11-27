import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { InboxRowData, RowAction } from "./inbox-queries";

export type InboxIntent = {
	target: RowAction;
	action: "open" | "later" | "restore";
};

const labels = {
	running: "Review in progress",
	draft_ready: "Private draft saved",
	failed: "Review failed",
	abandoned: "Review interrupted",
};

export function InboxRow({
	row,
	version,
	later,
	pending,
	onAction,
}: {
	row: InboxRowData;
	version: string;
	later: boolean;
	pending: InboxIntent[];
	onAction: (intent: InboxIntent) => void;
}) {
	const target = {
		url: row.url,
		identity: row.identity,
		settingsVersion: version,
	};
	return (
		<li className="flex flex-col items-start gap-2 border-b px-4 py-3 last:border-0 lg:flex-row lg:justify-between">
			<div className="min-w-0 space-y-1">
				<h3 className="wrap-anywhere font-semibold">{row.title}</h3>
				<p className="wrap-anywhere text-xs text-muted-foreground">
					<a
						className="inline-flex items-center gap-1 font-mono text-muted-foreground hover:text-primary hover:underline focus-visible:ring-2 focus-visible:ring-ring"
						href={row.url}
						target="_blank"
						rel="noreferrer"
					>
						{row.identity.host !== "github.com" && `${row.identity.host}/`}
						{row.repository} #{row.identity.number}{" "}
						<ExternalLink aria-hidden="true" className="size-3 shrink-0" />
					</a>{" "}
					· @{row.author}
				</p>
				<p className="wrap-anywhere text-xs text-muted-foreground">
					{row.reasons.join(" · ")}
					{row.lastSaved && <span> · {labels[row.lastSaved]}</span>}
					{row.stale && (
						<span className="ml-2 text-destructive">Stale result</span>
					)}
				</p>
			</div>
			<div className="flex items-center gap-1 lg:shrink-0">
				<Button
					type="button"
					disabled={pending.some((intent) => intent.action === "open")}
					onClick={() => onAction({ target, action: "open" })}
				>
					{row.lastSaved === "draft_ready" ? "Resume review" : "Open review"}
				</Button>
				<Button
					type="button"
					variant="ghost"
					disabled={pending.some((intent) => intent.action !== "open")}
					onClick={() =>
						onAction({ target, action: later ? "restore" : "later" })
					}
				>
					{later ? "Restore" : "Later"}
				</Button>
			</div>
		</li>
	);
}
