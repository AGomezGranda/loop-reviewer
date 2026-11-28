const maxDateSeconds = 8_640_000_000_000;

export function openingTime(epochSeconds: number | null): number | null {
	return epochSeconds !== null && Math.abs(epochSeconds) <= maxDateSeconds
		? epochSeconds
		: null;
}
