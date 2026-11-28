import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import type { InboxRowData, RowAction } from "./inbox-queries";
import { openingTime } from "./opening-time";

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

function openingAge(opened: Date) {
	const minutes = Math.max(
		0,
		Math.floor((Date.now() - opened.getTime()) / 60_000),
	);
	if (!minutes) return "just now";
	if (minutes < 60) return `${minutes}m ago`;
	if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
	return `${Math.floor(minutes / 1440)}d ago`;
}

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
	const epoch = openingTime(row.openedAt);
	const opened = epoch === null ? null : new Date(epoch * 1000);
	const target = {
		url: row.url,
		identity: row.identity,
		settingsVersion: version,
	};
	return (
		<li className="flex flex-col items-start gap-2 border-b px-4 py-3 last:border-0 lg:flex-row lg:justify-between">
			<div className="min-w-0 space-y-1">
				<h3 className="wrap-anywhere font-semibold">{row.title}</h3>
				<p className="group/meta wrap-anywhere text-xs text-muted-foreground">
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
					· @{row.author} ·{" "}
					{opened ? (
						<time
							dateTime={opened.toISOString()}
							title={opened.toLocaleString()}
						>
							Opened{" "}
							<span className="group-focus-within/meta:hidden">
								{openingAge(opened)}
							</span>
							<span className="hidden group-focus-within/meta:inline">
								{opened.toLocaleString()}
							</span>
						</time>
					) : (
						"Opening time unavailable"
					)}
				</p>
				<p className="wrap-anywhere text-xs text-muted-foreground">
					{row.reasonDetails.map((reason, index) => (
						<span key={`${reason.code}:${reason.label}`}>
							{index > 0 && " · "}
							{reason.code === "direct_request" ? (
								<span className="rounded-sm border border-primary/35 bg-accent px-1.5 py-0.5 text-xs font-medium text-primary">
									Requested from me
								</span>
							) : (
								reason.label
							)}
						</span>
					))}
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
