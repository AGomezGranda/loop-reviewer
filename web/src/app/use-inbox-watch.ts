import { useEffect, useState } from "react";
import { ApiError, openWatch } from "@/api/client";

export function useInboxWatch(invalidate: () => void) {
	const [status, setStatus] = useState<
		"connecting" | "connected" | "disconnected" | "unauthorized"
	>("connecting");
	useEffect(() => {
		const controller = new AbortController();
		let delay = 500;
		let timer: ReturnType<typeof setTimeout> | undefined;

		async function connect() {
			try {
				for await (const event of openWatch(controller.signal)) {
					if (event === "ready") {
						delay = 500;
						setStatus("connected");
					}
					if (event === "ready" || event === "scan-finished") invalidate();
				}
			} catch (error) {
				if (controller.signal.aborted) return;
				if (
					error instanceof ApiError &&
					error.details.code === "unauthorized"
				) {
					setStatus("unauthorized");
					return;
				}
			}
			if (!controller.signal.aborted) {
				setStatus("disconnected");
				timer = setTimeout(connect, delay);
				delay = Math.min(8000, delay * 2);
			}
		}

		void connect();
		return () => {
			controller.abort();
			clearTimeout(timer);
		};
	}, [invalidate]);
	return status;
}
