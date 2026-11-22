import { Tabs } from "radix-ui";
import { useState } from "react";
import type { InboxReport } from "./inbox-queries";
import { type InboxIntent, InboxRow } from "./inbox-row";
import { RepositoryPicker, repositoryKey } from "./repository-picker";

export function InboxList({
	report,
	busy,
	onAction,
}: {
	report: InboxReport;
	busy: boolean;
	onAction: (intent: InboxIntent) => void;
}) {
	const [key, setKey] = useState("");
	const selected = report.repositories.find(
		(repo) => repositoryKey(repo) === key,
	);
	const queues = (["active", "later"] as const).map((name) => ({
		name,
		label: name === "active" ? "Active" : "Later",
		rows: report[name].filter(
			(row) => !selected || repositoryKey(row.identity) === key,
		),
	}));
	return (
		<Tabs.Root
			defaultValue="active"
			className="min-w-0 rounded-lg border bg-card"
		>
			<div className="flex flex-wrap items-center justify-between gap-2 border-b bg-muted/40 px-3 py-2">
				<Tabs.List aria-label="Review queue" className="flex gap-1">
					{queues.map(({ name, label, rows }) => (
						<Tabs.Trigger
							key={name}
							value={name}
							aria-label={`${label} ${rows.length}`}
							className="flex items-center gap-2 rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring data-[state=active]:bg-accent data-[state=active]:text-accent-foreground"
						>
							{label}
							<span className="font-mono text-xs">{rows.length}</span>
						</Tabs.Trigger>
					))}
				</Tabs.List>
				<RepositoryPicker
					report={report}
					selected={selected}
					onSelect={setKey}
				/>
			</div>
			{queues.map(({ name, rows }) => (
				<Tabs.Content
					key={name}
					value={name}
					className="rounded-b-lg focus-visible:ring-2 focus-visible:ring-ring"
				>
					{rows.length ? (
						<ul>
							{rows.map((row) => (
								<InboxRow
									key={row.url}
									row={row}
									version={report.settingsVersion}
									later={name === "later"}
									busy={busy}
									onAction={onAction}
								/>
							))}
						</ul>
					) : (
						<p className="px-4 py-5 text-sm text-muted-foreground">
							{name === "active"
								? "No pull requests are waiting for review."
								: "No deferred pull requests."}
						</p>
					)}
				</Tabs.Content>
			))}
		</Tabs.Root>
	);
}
