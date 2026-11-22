import { ChevronDown, ListFilter } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogClose,
	DialogContent,
	DialogTitle,
	DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { InboxReport } from "./inbox-queries";

type Repository = InboxReport["repositories"][number];

export function repositoryKey(
	repo: Pick<Repository, "host" | "repositoryId" | "account">,
) {
	return `${repo.host}/${repo.repositoryId}/${repo.account}`;
}

export function RepositoryPicker({
	report,
	selected,
	onSelect,
}: {
	report: InboxReport;
	selected: Repository | undefined;
	onSelect: (key: string) => void;
}) {
	const [search, setSearch] = useState("");
	const counts = new Map<string, number>();
	for (const row of report.active) {
		const key = repositoryKey(row.identity);
		counts.set(key, (counts.get(key) ?? 0) + 1);
	}
	const options = [
		{ key: "", label: "All repositories", count: report.active.length },
		...report.repositories.map((repo) => ({
			key: repositoryKey(repo),
			label: `${repo.host}/${repo.repository}`,
			count: counts.get(repositoryKey(repo)) ?? 0,
		})),
	].filter((option) =>
		option.label.toLowerCase().includes(search.trim().toLowerCase()),
	);
	return (
		<Dialog onOpenChange={() => setSearch("")}>
			<DialogTrigger asChild>
				<Button variant="outline" size="sm" className="max-w-full">
					<ListFilter aria-hidden="true" />
					<span className="truncate">
						{selected
							? `${selected.host}/${selected.repository}`
							: "All repositories"}
					</span>
					<ChevronDown aria-hidden="true" />
				</Button>
			</DialogTrigger>
			<DialogContent className="min-w-0" aria-describedby={undefined}>
				<DialogTitle>Filter by repository</DialogTitle>
				<Input
					aria-label="Search repositories"
					placeholder="Search by repository or host"
					value={search}
					onChange={(event) => setSearch(event.target.value)}
				/>
				<div className="max-h-[50dvh] min-w-0 overflow-y-auto">
					{options.map((option) => (
						<DialogClose key={option.key} asChild>
							<Button
								variant="ghost"
								aria-label={`${option.label} ${option.count} active`}
								aria-pressed={
									option.key === (selected ? repositoryKey(selected) : "")
								}
								className="h-auto w-full justify-between gap-3 py-2 text-left aria-pressed:bg-accent aria-pressed:text-accent-foreground"
								onClick={() => onSelect(option.key)}
							>
								<span className="min-w-0 wrap-anywhere font-mono text-xs whitespace-normal">
									{option.label}
								</span>
								<span className="shrink-0 text-xs">{option.count} active</span>
							</Button>
						</DialogClose>
					))}
					{!options.length && (
						<p role="status" className="p-2 text-muted-foreground">
							No matching repositories.
						</p>
					)}
				</div>
			</DialogContent>
		</Dialog>
	);
}
