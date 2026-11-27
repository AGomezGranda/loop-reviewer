import { useQueryClient } from "@tanstack/react-query";
import { useLayoutEffect, useRef } from "react";
import { useLocation, useNavigate } from "react-router";
import { inboxQuery } from "@/features/inbox/inbox-queries";
import { reviewsQuery } from "@/features/reviews/reviews-queries";

export function useRouteNavigation() {
	const navigate = useNavigate();
	const { key } = useLocation();
	const currentKey = useRef<string | null>(null);
	useLayoutEffect(() => {
		currentKey.current = key;
		return () => {
			currentKey.current = null;
		};
	}, [key]);

	return (path: string, state?: { notice?: string }) => {
		if (currentKey.current !== key) return false;
		void navigate(path, { state });
		return true;
	};
}

export function useReviewLaunch() {
	const cache = useQueryClient();

	return () => {
		void cache.invalidateQueries({ queryKey: reviewsQuery.queryKey });
		void cache.invalidateQueries({ queryKey: inboxQuery.queryKey });
		return "Review submitted.";
	};
}
