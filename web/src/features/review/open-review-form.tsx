import { useMutation } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { type ReactNode, useState } from "react";
import { errorMessage } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { launchReview } from "./review-queries";

export function OpenReviewForm({
	onAccepted,
	reviewsLink,
}: {
	onAccepted: () => string;
	reviewsLink: ReactNode;
}) {
	const [url, setUrl] = useState("");
	const [notice, setNotice] = useState("");
	const open = useMutation({
		mutationFn: launchReview,
		onMutate: () => setNotice(""),
		onSuccess: () => {
			setUrl("");
			setNotice(onAccepted());
		},
		onError: (error) => setNotice(errorMessage(error)),
	});
	return (
		<form
			className="min-w-0 space-y-2"
			onSubmit={(event) => {
				event.preventDefault();
				if (!open.isPending) open.mutate(url);
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
			</div>
			{(notice || open.isPending) && (
				<p role="status" className="text-sm text-muted-foreground">
					<span>{notice || "Submitting review…"}</span>{" "}
					{open.isSuccess && reviewsLink}
				</p>
			)}
		</form>
	);
}
