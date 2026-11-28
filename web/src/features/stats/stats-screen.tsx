import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { errorMessage } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { type Stats, type StatsPeriod, statsQuery } from "./stats-queries";

type Repository = Stats["repositoryOptions"][number];

const controlStyle =
	"w-full min-w-0 rounded-md border bg-card px-2 py-1.5 text-foreground focus-visible:ring-2 focus-visible:ring-ring";
const utcDateTime = new Intl.DateTimeFormat(undefined, {
	timeZone: "UTC",
	month: "short",
	day: "numeric",
	hour: "2-digit",
	minute: "2-digit",
	hourCycle: "h23",
});
const weekMs = 7 * 86_400_000;

function repositoryLabels(options: Repository[]) {
	const stableCounts = new Map<string, number>();
	for (const repo of options) {
		if (!repo.legacyNameOnly) {
			const label = `${repo.host}/${repo.slug}`.toLowerCase();
			stableCounts.set(label, (stableCounts.get(label) ?? 0) + 1);
		}
	}
	return (repo: Repository) => {
		const label = `${repo.host}/${repo.slug}`;
		if (repo.legacyNameOnly) return `${label} · name only`;
		if ((stableCounts.get(label.toLowerCase()) ?? 0) > 1) {
			return `${label} · ID ${repo.key.slice(repo.host.length + 1)}`;
		}
		return label;
	};
}

export function StatsScreen() {
	const [period, setPeriod] = useState<StatsPeriod>("30d");
	const [repository, setRepository] = useState<string>();
	const cache = useQueryClient();
	const result = useQuery(statsQuery(period, repository));
	const options =
		result.data?.repositoryOptions ??
		cache.getQueryData(statsQuery(period).queryKey)?.repositoryOptions ??
		cache.getQueryData(statsQuery(period === "all" ? "30d" : "all").queryKey)
			?.repositoryOptions ??
		[];
	const label = repositoryLabels(options);

	return (
		<div className="space-y-5">
			<header className="space-y-1">
				<h1 className="text-2xl font-semibold tracking-tight">Stats</h1>
				<p className="text-sm text-muted-foreground">
					All accounts in this local workspace
				</p>
			</header>
			<div className="grid gap-3 text-sm sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)]">
				<label className="space-y-1 text-muted-foreground">
					<span className="block">Period</span>
					<select
						className={controlStyle}
						value={period}
						onChange={(event) =>
							setPeriod(event.target.value === "all" ? "all" : "30d")
						}
					>
						<option value="30d">Last 30 days</option>
						<option value="all">All time</option>
					</select>
				</label>
				<label className="space-y-1 text-muted-foreground">
					<span className="block">Repository</span>
					<select
						className={`${controlStyle} truncate font-mono`}
						value={repository ?? ""}
						onChange={(event) => setRepository(event.target.value || undefined)}
					>
						<option value="">All repositories</option>
						{options.map((repo) => (
							<option key={repo.key} value={repo.key}>
								{label(repo)}
							</option>
						))}
					</select>
				</label>
			</div>
			{result.isPending && <p role="status">Loading stats…</p>}
			{result.isError && (
				<div className="space-y-2">
					<p role="alert" className="text-sm text-destructive">
						{errorMessage(result.error)}
					</p>
					<Button
						type="button"
						variant="outline"
						disabled={result.isFetching}
						onClick={() => void result.refetch()}
					>
						Retry
					</Button>
				</div>
			)}
			{result.isSuccess && (
				<StatsResults data={result.data} repositoryLabel={label} />
			)}
		</div>
	);
}

function StatsResults({
	data,
	repositoryLabel,
}: {
	data: Stats;
	repositoryLabel: (repo: Repository) => string;
}) {
	const { totals, undatedCompletions } = data;
	return (
		<>
			<section aria-labelledby="stats-totals" className="space-y-2">
				<h2 id="stats-totals" className="font-medium">
					Totals
				</h2>
				<dl className="grid grid-cols-3 gap-2 sm:gap-3">
					{(
						[
							["Completed", totals.completed],
							["Published", totals.published],
							["Completed locally", totals.completedLocally],
						] as const
					).map(([name, count]) => (
						<Card key={name} size="sm" className="gap-1 px-2 sm:px-3">
							<dt className="text-xs text-muted-foreground">{name}</dt>
							<dd className="font-mono text-xl font-semibold tabular-nums">
								{count}
							</dd>
						</Card>
					))}
				</dl>
				{totals.completed === 0 && (
					<p className="text-sm text-muted-foreground">
						{data.repositoryOptions.length === 0
							? "No reviews have been completed in this workspace."
							: "No reviews were completed in this period."}
					</p>
				)}
				{undatedCompletions > 0 && (
					<p className="text-sm text-muted-foreground">
						{undatedCompletions}{" "}
						{undatedCompletions === 1
							? "completion has no recorded date"
							: "completions have no recorded date"}{" "}
						and only appears in all-time totals.
					</p>
				)}
			</section>
			<WeeklyActivity weeks={data.weeks} />
			<section aria-labelledby="stats-repositories" className="space-y-2">
				<h2 id="stats-repositories" className="font-medium">
					Repository breakdown
				</h2>
				{data.repositories.length === 0 ? (
					<p className="text-sm text-muted-foreground">
						No repositories with completions in this period.
					</p>
				) : (
					<ol className="divide-y rounded-lg border bg-card">
						{data.repositories.map(({ repository, counts }) => (
							<li
								key={repository.key}
								className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 px-3 py-2 text-sm"
							>
								<span className="wrap-anywhere font-mono">
									{repositoryLabel(repository)}
								</span>
								<span className="text-muted-foreground">
									<strong className="font-medium text-foreground">
										{counts.completed} completed
									</strong>{" "}
									· {counts.published} published · {counts.completedLocally}{" "}
									local
								</span>
							</li>
						))}
					</ol>
				)}
			</section>
		</>
	);
}

function WeeklyActivity({ weeks }: { weeks: Stats["weeks"] }) {
	const peak = Math.max(1, ...weeks.map(({ counts }) => counts.completed));
	return (
		<section aria-labelledby="stats-weeks" className="space-y-2">
			<div>
				<h2 id="stats-weeks" className="font-medium">
					Weekly activity
				</h2>
				<p className="text-sm text-muted-foreground">
					Last 30 days · UTC · Weeks start Monday
				</p>
			</div>
			<ul className="divide-y rounded-lg border bg-card">
				{weeks.map(({ start, end, counts }) => {
					const from = new Date(start);
					const to = new Date(end);
					const partial = to.getTime() - from.getTime() < weekMs;
					return (
						<li key={start} className="space-y-1.5 px-3 py-2 text-sm">
							<div className="flex flex-wrap justify-between gap-x-3">
								<span className="text-muted-foreground">
									{utcDateTime.format(from)} – {utcDateTime.format(to)}
									{partial && " · partial week"}
								</span>
								<span className="font-mono tabular-nums">
									{counts.completed} completed
								</span>
							</div>
							<div
								aria-hidden="true"
								className="h-1 overflow-hidden rounded-full bg-muted"
							>
								<div
									className="h-full bg-primary"
									style={{ width: `${(counts.completed / peak) * 100}%` }}
								/>
							</div>
						</li>
					);
				})}
			</ul>
		</section>
	);
}
