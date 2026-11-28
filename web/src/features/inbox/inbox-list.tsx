import { ArrowDownUp } from "lucide-react";
import { Tabs } from "radix-ui";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { type InboxReport, isDirectlyRequested } from "./inbox-queries";
import { type InboxIntent, InboxRow } from "./inbox-row";
import { openingTime } from "./opening-time";
import { RepositoryPicker, repositoryKey } from "./repository-picker";

export function InboxList({
	report,
	pending,
	onAction,
}: {
	report: InboxReport;
	pending: InboxIntent[];
	onAction: (intent: InboxIntent) => void;
}) {
	const [key, setKey] = useState("");
	const [queue, setQueue] = useState<"active" | "later">("active");
	const [requestedOnly, setRequestedOnly] = useState(false);
	const [oldestFirst, setOldestFirst] = useState(false);
	const selected = report.repositories.find(
		(repo) => repositoryKey(repo) === key,
	);
	const queues = (["active", "later"] as const).map((name) => {
		const scoped = report[name].filter(
			(row) => !selected || repositoryKey(row.identity) === key,
		);
		const requested = scoped.filter(isDirectlyRequested);
		const rows = requestedOnly ? requested : scoped;
		rows.sort((a, b) => {
			const first = openingTime(a.openedAt);
			const second = openingTime(b.openedAt);
			if (first === second) return 0;
			if (first === null) return 1;
			if (second === null) return -1;
			return oldestFirst ? first - second : second - first;
		});
		return {
			name,
			label: name === "active" ? "Active" : "Later",
			rows,
			requestCount: requested.length,
		};
	});
	const pickerRows = requestedOnly
		? report[queue].filter(isDirectlyRequested)
		: report[queue];
	return (
		<Tabs.Root
			value={queue}
			onValueChange={(value) =>
				setQueue(value === "later" ? "later" : "active")
			}
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
				<div className="flex min-w-0 flex-wrap items-center gap-2">
					<Button
						type="button"
						variant="outline"
						size="sm"
						aria-pressed={requestedOnly}
						className="aria-pressed:border-primary/50 aria-pressed:bg-accent aria-pressed:text-primary"
						onClick={() => setRequestedOnly(!requestedOnly)}
					>
						Requested from me{" "}
						<span className="font-mono text-xs">
							{queues.find((item) => item.name === queue)?.requestCount}
						</span>
					</Button>
					<Button
						type="button"
						variant="outline"
						size="sm"
						aria-label={`Sort by opening time: ${oldestFirst ? "oldest" : "newest"} first`}
						onClick={() => setOldestFirst(!oldestFirst)}
					>
						<ArrowDownUp aria-hidden="true" />
						{oldestFirst ? "Oldest first" : "Newest first"}
					</Button>
					<RepositoryPicker
						queue={queue}
						rows={pickerRows}
						report={report}
						selected={selected}
						onSelect={setKey}
					/>
				</div>
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
									pending={pending.filter(
										(intent) => intent.target.url === row.url,
									)}
									onAction={onAction}
								/>
							))}
						</ul>
					) : (
						<p className="px-4 py-5 text-sm text-muted-foreground">
							{requestedOnly
								? "No direct review requests match this view."
								: name === "active"
									? "No pull requests are waiting for review."
									: "No deferred pull requests."}
						</p>
					)}
				</Tabs.Content>
			))}
		</Tabs.Root>
	);
}
