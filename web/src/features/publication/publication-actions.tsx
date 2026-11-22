import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, errorMessage } from "@/api/client";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import {
	type Preview,
	type PreviewOptions,
	type PublicationIntent,
	preparePreview,
	publicationQuery,
} from "./publication-queries";

export function PreviewControl({
	runId,
	version,
	disabled,
	onPreview,
	recoveryLink,
	onConflict,
}: {
	runId: string;
	version: string;
	disabled: boolean;
	onPreview: (preview: Preview) => void;
	recoveryLink: (id: string) => React.ReactNode;
	onConflict: () => void;
}) {
	const cache = useQueryClient();
	const [action, setAction] = useState<PreviewOptions["action"] | "">("");
	const [summaryOnly, setSummaryOnly] = useState(false);
	const preview = useMutation({
		mutationFn: (options: PreviewOptions) => preparePreview(runId, options),
		onSuccess: (preview) => {
			cache.setQueryData(publicationQuery(preview.id).queryKey, {
				preview,
				status: "preview",
				receipt: null,
			});
		},
		onError: (error) => {
			if (
				error instanceof ApiError &&
				["draft_changed", "review_completed", "recovery_required"].includes(
					error.details.code,
				)
			)
				onConflict();
		},
	});
	const recoveryId =
		preview.error instanceof ApiError
			? preview.error.details.publicationId
			: undefined;
	return (
		<form
			className="space-y-3 rounded-lg border bg-card p-4"
			onSubmit={(event) => {
				event.preventDefault();
				if (action && !disabled && !preview.isPending)
					preview.mutate(
						{ version, action, summaryOnly },
						{ onSuccess: onPreview },
					);
			}}
		>
			<h2 className="font-medium">Publish to GitHub</h2>
			<fieldset
				disabled={disabled || preview.isPending}
				className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end"
			>
				<label className="grid gap-1 text-sm" htmlFor="review-action">
					GitHub action
					<select
						id="review-action"
						value={action}
						required
						onChange={(event) => {
							const value = event.target.value;
							if (value === "" || value === "comment" || value === "approve")
								setAction(value);
						}}
						className="h-8 rounded-md border bg-background px-2 focus-visible:ring-2 focus-visible:ring-ring"
					>
						<option value="">Choose an action</option>
						<option value="comment">Comment</option>
						<option value="approve">Approve</option>
					</select>
				</label>
				<label className="flex min-h-8 items-center gap-2 text-sm">
					<input
						type="checkbox"
						className="accent-primary"
						checked={summaryOnly}
						onChange={(event) => setSummaryOnly(event.target.checked)}
					/>{" "}
					Summary only
				</label>
				<Button type="submit" disabled={!action}>
					{preview.isPending ? "Preparing…" : "Prepare preview"}
				</Button>
			</fieldset>
			{preview.error && (
				<p role="alert" className="text-sm text-destructive">
					{errorMessage(preview.error)}
				</p>
			)}
			{recoveryId && recoveryLink(recoveryId)}
		</form>
	);
}

export function PublicationActions({
	record,
	pending,
	uncertain,
	canRetryUnconfirmed,
	onAction,
}: {
	record: components["schemas"]["PublicationResponse"];
	pending: boolean;
	uncertain: boolean;
	canRetryUnconfirmed: boolean;
	onAction: (intent: PublicationIntent) => void;
}) {
	const [acceptRisk, setAcceptRisk] = useState(false);
	const account = record.preview.selection.account;
	const act = (intent: PublicationIntent) => {
		setAcceptRisk(false);
		onAction(intent);
	};
	if (record.status === "published") return null;
	if (record.status === "preview") {
		return uncertain ? (
			<p role="status" className="text-sm text-destructive">
				The send result is uncertain. Make a fresh preview before deciding
				whether to send.
			</p>
		) : (
			<Button
				type="button"
				disabled={pending}
				className="w-full sm:w-auto"
				onClick={() => act({ kind: "confirm" })}
			>
				Confirm {record.preview.action === "approve" ? "approval" : "comment"}{" "}
				as @{account}
			</Button>
		);
	}
	if (record.status === "rejected") {
		return (
			<Button
				type="button"
				disabled={pending}
				className="w-full sm:w-auto"
				onClick={() => act({ kind: "retry", acceptDuplicateRisk: false })}
			>
				Retry publication as @{account}
			</Button>
		);
	}
	return (
		<div className="space-y-3">
			<Button
				type="button"
				disabled={pending}
				className="w-full sm:w-auto"
				onClick={() => act({ kind: "recover" })}
			>
				Recover publication
			</Button>
			{canRetryUnconfirmed && (
				<fieldset disabled={pending} className="space-y-3">
					<label className="flex items-start gap-2 text-sm">
						<input
							type="checkbox"
							checked={acceptRisk}
							onChange={(event) => setAcceptRisk(event.target.checked)}
							className="mt-1 accent-primary"
						/>{" "}
						I accept the risk of publishing a duplicate review.
					</label>
					<Button
						type="button"
						variant="outline"
						disabled={!acceptRisk || pending}
						onClick={() =>
							act({ kind: "retry", acceptDuplicateRisk: acceptRisk })
						}
					>
						Retry publication as @{account}
					</Button>
				</fieldset>
			)}
		</div>
	);
}
