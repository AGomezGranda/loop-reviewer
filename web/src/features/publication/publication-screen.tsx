import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { ApiError, errorMessage } from "@/api/client";
import { PublicationActions } from "./publication-actions";
import { PublicationPreview } from "./publication-preview";
import {
	operatePublication,
	type PublicationIntent,
	publicationQuery,
	unconfirmedQuery,
} from "./publication-queries";

const labels = {
	preview: "Ready for confirmation",
	unconfirmed: "Publication unconfirmed",
	rejected: "GitHub rejected the send",
	published: "Published to GitHub",
};
const rejections = {
	authentication: "GitHub authentication failed. Check your gh sign-in.",
	access: "The signed-in GitHub account lacks access.",
	validation: "GitHub rejected the review payload.",
	rate_limit: "GitHub rate-limited the review. Retry when the limit clears.",
};

export function PublicationScreen({
	id,
	onChanged,
	draftLink,
	historyLink,
	recoveryLink,
}: {
	id: string;
	onChanged: (runId: string) => void;
	draftLink: (runId: string) => ReactNode;
	historyLink: (runId: string) => ReactNode;
	recoveryLink: (publicationId: string) => ReactNode;
}) {
	const cache = useQueryClient();
	const query = publicationQuery(id);
	const publication = useQuery(query);
	const uncertaintyKey = `loop-reviewer.publication-uncertain.${id}`;
	const [uncertain, setUncertain] = useState(
		() => sessionStorage.getItem(uncertaintyKey) !== null,
	);
	function rememberUncertainty(uncertain: boolean) {
		if (uncertain) sessionStorage.setItem(uncertaintyKey, "1");
		else sessionStorage.removeItem(uncertaintyKey);
		setUncertain(uncertain);
	}
	const [canRetry, setCanRetry] = useState(false);
	const operation = useMutation({
		mutationFn: (intent: PublicationIntent) => {
			if (intent.kind !== "recover")
				sessionStorage.setItem(uncertaintyKey, "1");
			return operatePublication(id, intent);
		},
		onMutate: () => setCanRetry(false),
		onSuccess: async (result) => {
			if (result.status !== "preview") rememberUncertainty(false);
			await cache.cancelQueries({ queryKey: query.queryKey });
			cache.setQueryData(
				query.queryKey,
				(current) =>
					current && {
						...current,
						receipt: result.completion?.receipt ?? current.receipt,
						status: result.status,
					},
			);
		},
		onError: (error, intent) => {
			const details = error instanceof ApiError ? error.details : undefined;
			if (details?.code === "duplicate_risk_consent_required")
				setCanRetry(true);
			if (intent.kind === "recover") return;
			const ambiguous =
				!details ||
				details.code === "internal_error" ||
				details.code === "local_state" ||
				(details.code === "recovery_required" &&
					(!details.publicationId || details.publicationId === id));
			rememberUncertainty(ambiguous);
		},
		onSettled: async () => {
			await cache.invalidateQueries({ queryKey: ["publication"] });
			if (publication.data) onChanged(publication.data.preview.runId);
		},
	});
	const record = publication.data;
	const details =
		operation.error instanceof ApiError ? operation.error.details : undefined;
	const completedId = details?.completedRunId;
	const recoveryId =
		details?.code === "recovery_required" && details.publicationId !== id
			? details.publicationId
			: undefined;
	return (
		<div className="space-y-4">
			<h1 className="text-2xl font-semibold tracking-tight">
				Publication preview
			</h1>
			{publication.isPending && <p role="status">Loading saved preview…</p>}
			{publication.error && (
				<p role="alert" className="text-destructive">
					{errorMessage(publication.error)}
				</p>
			)}
			{record && (
				<>
					<div className="space-y-1">
						<p role="status" className="font-medium text-primary">
							{labels[record.status]}
						</p>
						<p className="wrap-anywhere text-sm text-muted-foreground">
							<code>{record.preview.id}</code>
						</p>
					</div>
					<PublicationPreview preview={record.preview} />
					<section
						aria-label="Publication decision"
						className="space-y-3 border-t pt-4"
					>
						{operation.isPending && (
							<p role="status" className="text-sm">
								Checking publication status…
							</p>
						)}
						{operation.error && (
							<p role="alert" className="text-sm text-destructive">
								{errorMessage(operation.error)}
							</p>
						)}
						{operation.data?.rejectionReason && (
							<p role="alert" className="text-sm text-destructive">
								{rejections[operation.data.rejectionReason]}
							</p>
						)}
						{!completedId &&
							!recoveryId &&
							details?.code !== "draft_changed" && (
								<PublicationActions
									record={record}
									pending={operation.isPending || publication.isError}
									uncertain={uncertain}
									canRetryUnconfirmed={canRetry}
									onAction={operation.mutate}
								/>
							)}
						{record.receipt && (
							<p className="text-sm">
								<a
									className="text-primary underline"
									href={`${record.preview.selection.url}#pullrequestreview-${record.receipt.reviewId}`}
									target="_blank"
									rel="noreferrer"
								>
									GitHub review <code>{record.receipt.reviewId}</code>
								</a>
							</p>
						)}
						{completedId || record.status === "published"
							? historyLink(completedId ?? record.preview.runId)
							: recoveryId
								? recoveryLink(recoveryId)
								: record.status !== "unconfirmed" &&
									draftLink(record.preview.runId)}
					</section>
				</>
			)}
		</div>
	);
}

export function UnconfirmedPublications({
	publicationLink,
}: {
	publicationLink: (id: string, title: string) => ReactNode;
}) {
	const unresolved = useQuery(unconfirmedQuery);
	if (unresolved.isPending)
		return <p role="status">Checking pending publications…</p>;
	if (unresolved.error)
		return (
			<p role="alert" className="text-destructive">
				{errorMessage(unresolved.error)}
			</p>
		);
	if (!unresolved.data?.length) return null;
	return (
		<section className="rounded-lg border bg-card">
			<h2 className="border-b px-4 py-3 font-medium">
				Unconfirmed publications
			</h2>
			<ul className="divide-y">
				{unresolved.data.map(({ preview }) => (
					<li key={preview.id} className="space-y-1 px-4 py-3">
						{publicationLink(
							preview.id,
							preview.selection.url.replace(/^https:\/\//, ""),
						)}
						<p className="text-sm text-muted-foreground">
							<code>
								@{preview.selection.account} · {preview.id}
							</code>
						</p>
					</li>
				))}
			</ul>
		</section>
	);
}
