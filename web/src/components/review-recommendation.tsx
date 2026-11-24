import type { components } from "@/api/schema";

type Recommendation = components["schemas"]["FeedbackResponse"]["verdict"];

const label: Record<Recommendation, string> = {
	approved: "Approve",
	comment: "Comment",
	none: "None",
};

export function ReviewRecommendation({ verdict }: { verdict: Recommendation }) {
	return (
		<p className="text-sm text-muted-foreground">
			Original recommendation{" "}
			<strong className="font-medium text-foreground">{label[verdict]}</strong>
		</p>
	);
}
