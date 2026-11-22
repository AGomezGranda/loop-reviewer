import { Activity, ChevronDown } from "lucide-react";
import type { InboxReport } from "./inbox-queries";

const labels = {
	success: "Up to date",
	failed: "Scan failed",
	pending: "Waiting for a scan",
};

export function RepositoryChecks({ report }: { report: InboxReport }) {
	return (
		<section
			aria-label="Repository checks"
			className="rounded-lg border bg-card"
		>
			<div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
				<h2 className="font-medium">Repository checks</h2>
				<code className="wrap-anywhere text-sm text-primary">
					{report.prefix}
				</code>
			</div>
			<ul className="divide-y">
				{report.repositories.map((repo) => (
					<li
						key={`${repo.host}/${repo.repositoryId}`}
						className="grid gap-1 px-4 py-3 text-sm sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]"
					>
						<div className="min-w-0">
							<p className="wrap-anywhere font-medium">{repo.repository}</p>
							<p className="wrap-anywhere text-muted-foreground">
								{repo.host} · @{repo.account}
							</p>
						</div>
						<div>
							<p
								className={
									repo.status === "failed"
										? "text-destructive"
										: "text-foreground"
								}
							>
								{labels[repo.status]}
							</p>
							<p className="text-muted-foreground">
								Last successful check{" "}
								{repo.lastSuccess ? (
									<time dateTime={repo.lastSuccess}>
										{new Date(repo.lastSuccess).toLocaleString()}
									</time>
								) : (
									"—"
								)}
							</p>
							{repo.error && (
								<p className="mt-1 wrap-anywhere text-destructive">
									{repo.error}
								</p>
							)}
						</div>
					</li>
				))}
			</ul>
		</section>
	);
}

export function RepositoryHealth({ report }: { report: InboxReport }) {
	const repos = report.repositories;
	const failed = repos.filter((repo) => repo.status === "failed");
	const pending = repos.filter((repo) => repo.status === "pending").length;
	return (
		<details className="group rounded-lg border bg-card">
			<summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-xs focus-visible:ring-2 focus-visible:ring-ring">
				<Activity aria-hidden="true" className="size-4 text-muted-foreground" />
				<span className="font-medium">
					{repos.length} {repos.length === 1 ? "repository" : "repositories"}
				</span>
				<span className="text-muted-foreground">
					{repos.length - failed.length - pending} up to date
				</span>
				{pending > 0 && <span>{pending} pending</span>}
				{failed.length > 0 && (
					<span className="wrap-anywhere text-destructive">
						{failed.length === 1 ? failed[0].repository : failed.length} failed
					</span>
				)}
				<span className="ml-auto flex items-center gap-1 text-primary">
					<span className="group-open:hidden">View checks</span>
					<span className="hidden group-open:inline">Hide checks</span>
					<ChevronDown
						aria-hidden="true"
						className="size-3.5 group-open:rotate-180"
					/>
				</span>
			</summary>
			<RepositoryChecks report={report} />
		</details>
	);
}
