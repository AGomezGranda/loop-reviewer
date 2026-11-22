const storageKey = "loop-reviewer.capability";

export function initializeCapability(): string | null {
	const fragment = new URLSearchParams(window.location.hash.slice(1));
	const capability = fragment.get("capability");
	if (fragment.has("capability")) {
		window.history.replaceState(
			window.history.state,
			"",
			window.location.pathname + window.location.search,
		);
		if (capability) window.sessionStorage.setItem(storageKey, capability);
		else window.sessionStorage.removeItem(storageKey);
	}
	return window.sessionStorage.getItem(storageKey);
}

export function getCapability(): string | null {
	return window.sessionStorage.getItem(storageKey);
}
