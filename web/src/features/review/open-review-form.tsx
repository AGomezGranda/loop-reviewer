import { useMutation } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { useState } from "react";
import { errorMessage } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type OpenOutcome, openReview } from "./review-queries";

export function OpenReviewForm({
	onOpened,
}: {
	onOpened: (outcome: OpenOutcome) => string | undefined;
}) {
	const [url, setUrl] = useState("");
	const [notice, setNotice] = useState("");
	const open = useMutation({
		mutationFn: ({
			url,
			retry,
		}: {
			url: string;
			retry: boolean;
			onOpened: typeof onOpened;
		}) => openReview(url, retry),
		onMutate: () => setNotice(""),
		onSuccess: (outcome, request) => setNotice(request.onOpened(outcome) ?? ""),
		onError: (error) => setNotice(errorMessage(error)),
	});
	const canRetry =
		open.variables?.url === url &&
		(open.data?.kind === "failure" ||
			(open.data?.kind === "unavailable" && open.data.reason === "abandoned"));
	return (
		<form
			className="min-w-0 space-y-2"
			onSubmit={(event) => {
				event.preventDefault();
				open.mutate({ url, retry: false, onOpened });
			}}
		>
			<label htmlFor="one-off-url" className="text-xs font-medium">
				Pull request URL
			</label>
			<div className="flex flex-col gap-2 sm:flex-row">
				<Input
					id="one-off-url"
					type="url"
					placeholder="https://github.com/owner/repo/pull/123"
					value={url}
					required
					readOnly={open.isPending}
					onChange={(event) => {
						setUrl(event.target.value);
						setNotice("");
					}}
				/>
				<Button type="submit" className="shrink-0" disabled={open.isPending}>
					{open.isPending ? "Opening…" : "Open review"}{" "}
					<ArrowRight aria-hidden="true" />
				</Button>
				{canRetry && (
					<Button
						type="button"
						variant="outline"
						disabled={open.isPending}
						onClick={() => open.mutate({ url, retry: true, onOpened })}
					>
						Retry review
					</Button>
				)}
			</div>
			{(notice || open.isPending) && (
				<p role="status" className="text-sm text-muted-foreground">
					{notice || "Opening private review…"}
				</p>
			)}
		</form>
	);
}
