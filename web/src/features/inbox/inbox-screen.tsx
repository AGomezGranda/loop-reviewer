import {
	useMutation,
	useMutationState,
	useQuery,
	useQueryClient,
} from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { type ReactNode, useState } from "react";
import { ApiError, errorMessage } from "@/api/client";
import { Button } from "@/components/ui/button";
import { InboxList } from "./inbox-list";
import { inboxQuery, launchRow, moveRow, scanInbox } from "./inbox-queries";
import type { InboxIntent } from "./inbox-row";
import { RepositoryHealth } from "./repository-checks";

type InboxAction = InboxIntent | "scan";
const actionKey = ["inbox-action"];

export function InboxScreen({
	onAccepted,
	reviewsLink,
	setupLink,
	disconnected,
}: {
	onAccepted: () => string;
	reviewsLink: ReactNode;
	setupLink: ReactNode;
	disconnected?: boolean;
}) {
	const cache = useQueryClient();
	const inbox = useQuery(inboxQuery);
	const [notice, setNotice] = useState<{ text: string; accepted?: boolean }>();
	const pending = useMutationState({
		filters: { mutationKey: actionKey, status: "pending" },
		select: (mutation) => mutation.state.variables as InboxAction,
	});
	const scanning = pending.includes("scan");
	const update = useMutation({
		mutationKey: actionKey,
		mutationFn: async (intent: InboxAction) => {
			if (intent === "scan") return scanInbox();
			if (intent.action === "open") return launchRow(intent.target);
			return moveRow(intent.target, intent.action === "later");
		},
		onMutate: () => setNotice(undefined),
		onSuccess: (result) => {
			if ("launchId" in result) {
				setNotice({ text: onAccepted(), accepted: true });
				return;
			}
			return cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
		},
		onError: (error) => {
			if (
				error instanceof ApiError &&
				["inbox_changed", "identity_changed"].includes(error.details.code)
			) {
				setNotice({
					text: `${error.message} Choose the row again after the list refreshes.`,
				});
				void cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
			} else {
				setNotice({ text: errorMessage(error) });
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
						disabled={scanning}
						onClick={() => update.mutate("scan")}
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
			{scanning && (
				<p role="status" className="text-sm">
					Scanning repositories…
				</p>
			)}
			{notice && (
				<p role="status" className="rounded-md border px-3 py-2 text-sm">
					<span>{notice.text}</span> {notice.accepted && reviewsLink}
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
						pending={pending.filter((intent) => intent !== "scan")}
						onAction={(intent) => update.mutate(intent)}
					/>
				</>
			)}
		</div>
	);
}
