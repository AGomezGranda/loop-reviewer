const usd = new Intl.NumberFormat("en-US", {
	style: "currency",
	currency: "USD",
	minimumFractionDigits: 4,
	maximumFractionDigits: 4,
	useGrouping: false,
});

export function GenerationCost({
	generationModel,
	estimatedCostUsd,
}: {
	generationModel: string | null;
	estimatedCostUsd: number | null;
}) {
	return (
		<div className="space-y-1 text-sm text-muted-foreground">
			{generationModel && (
				<p>
					Generation model{" "}
					<code className="wrap-anywhere">{generationModel}</code>
				</p>
			)}
			<p>
				{estimatedCostUsd === null
					? "Cost unavailable"
					: `Estimated generation cost · ${estimatedCostUsd > 0 && estimatedCostUsd < 0.0001 ? "< $0.0001" : usd.format(estimatedCostUsd)}`}
			</p>
		</div>
	);
}
