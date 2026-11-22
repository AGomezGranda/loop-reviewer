import {
	focusManager,
	QueryClient,
	QueryClientProvider,
} from "@tanstack/react-query";
import { RouterProvider } from "react-router/dom";
import type { createWorkspaceRouter } from "./routes";

focusManager.setEventListener((onFocus) => {
	const refresh = () => onFocus();
	window.addEventListener("focus", refresh);
	document.addEventListener("visibilitychange", refresh);
	return () => {
		window.removeEventListener("focus", refresh);
		document.removeEventListener("visibilitychange", refresh);
	};
});

const queryClient = new QueryClient({
	defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
});

export function App({
	router,
}: {
	router: ReturnType<typeof createWorkspaceRouter>;
}) {
	return (
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>
	);
}
