import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { type ReactNode, useState } from "react";
import { ApiError, errorMessage } from "@/api/client";
import { Button } from "@/components/ui/button";
import { InboxList } from "./inbox-list";
import { inboxQuery, moveRow, openRow, scanInbox } from "./inbox-queries";
import type { InboxIntent } from "./inbox-row";
import { RepositoryHealth } from "./repository-checks";

type Outcome = Awaited<ReturnType<typeof openRow>>;

export function InboxScreen({
	onOpened,
	setupLink,
	disconnected,
}: {
	onOpened: (outcome: Outcome) => string | undefined;
	setupLink: ReactNode;
	disconnected?: boolean;
}) {
	const cache = useQueryClient();
	const inbox = useQuery(inboxQuery);
	const [notice, setNotice] = useState("");
	const update = useMutation({
		mutationFn: async ({
			intent,
		}: {
			intent: InboxIntent | "scan";
			onOpened: typeof onOpened;
		}) => {
			if (intent === "scan") return scanInbox();
			if (intent.action === "open") return openRow(intent.target);
			return moveRow(intent.target, intent.action === "later");
		},
		onMutate: () => setNotice(""),
		onSuccess: (result, request) => {
			if (result.kind === "report" || result.kind === "unconfigured") {
				cache.setQueryData(inboxQuery.queryKey, result);
			} else {
				setNotice(request.onOpened(result) ?? "");
			}
			void cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
		},
		onError: (error) => {
			if (
				error instanceof ApiError &&
				["inbox_changed", "identity_changed"].includes(error.details.code)
			) {
				setNotice(
					`${error.message} Choose the row again after the list refreshes.`,
				);
				void cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
			} else {
				setNotice(errorMessage(error));
			}
		},
	});
	const report = inbox.data;
	return (
		<div className="space-y-4">
			<div className="flex flex-wrap items-center justify-between gap-3">
				<h1 className="text-2xl font-semibold tracking-tight">Review inbox</h1>
				<div className="flex flex-wrap items-center gap-3">
					{report?.kind === "report" && setupLink}
					<Button
						type="button"
						variant="outline"
						disabled={update.isPending}
						onClick={() => update.mutate({ intent: "scan", onOpened })}
					>
						<RefreshCw aria-hidden="true" /> Scan now
					</Button>
				</div>
			</div>
			{disconnected && (
				<p role="status" className="text-sm text-destructive">
					Inbox watch disconnected. Scan now is available.
				</p>
			)}
			{update.isPending && (
				<p role="status" className="text-sm">
					{update.variables?.intent === "scan"
						? "Scanning repositories…"
						: update.variables?.intent.action === "open"
							? "Opening review…"
							: "Updating inbox…"}
				</p>
			)}
			{notice && (
				<p role="status" className="rounded-md border px-3 py-2 text-sm">
					{notice}
				</p>
			)}
			{inbox.isPending && <p role="status">Loading inbox…</p>}
			{inbox.isError && (
				<p role="alert" className="text-destructive">
					{errorMessage(inbox.error)}
				</p>
			)}
			{report?.kind === "unconfigured" && (
				<section className="space-y-2 rounded-lg border bg-card p-4">
					<h2 className="font-medium">Inbox setup needed</h2>
					<p className="text-sm text-muted-foreground">
						Configure repositories to discover pull requests here, or open a
						pull request from its URL.
					</p>
					{setupLink}
				</section>
			)}
			{report?.kind === "report" && (
				<>
					<RepositoryHealth report={report} />
					<InboxList
						report={report}
						busy={update.isPending}
						onAction={(intent) => update.mutate({ intent, onOpened })}
					/>
				</>
			)}
		</div>
	);
}
