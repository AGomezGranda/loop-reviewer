import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { Edit } from "./review-queries";

type Target = { runId: string; version: string; dirty: boolean };
export type Editor = Target &
	(
		| { kind: "edit_summary"; text: string }
		| {
				kind: "edit_finding";
				number: number;
				condition: string;
				consequence: string;
		  }
	);

export function DraftEditor({
	editor,
	frozen,
	busy,
	onChange,
	onSave,
	onDiscard,
}: {
	editor: Editor;
	frozen: boolean;
	busy: boolean;
	onChange: (editor: Editor) => void;
	onSave: (runId: string, edit: Edit) => void;
	onDiscard: () => void;
}) {
	const { runId, dirty: _dirty, ...body } = editor;
	return (
		<form
			className="space-y-3 rounded-lg border border-primary/50 bg-card p-4"
			onSubmit={(event) => {
				event.preventDefault();
				if (!frozen && !busy) onSave(runId, body);
			}}
		>
			<h2 className="font-medium">
				{editor.kind === "edit_summary"
					? "Edit summary"
					: `Edit finding ${editor.number}`}
			</h2>
			{frozen && (
				<p role="alert" className="text-sm text-destructive">
					The saved draft changed. Your text is still here. Copy or discard it,
					then select feedback from the latest saved draft.
				</p>
			)}
			{editor.kind === "edit_summary" ? (
				<label className="block space-y-1 text-sm" htmlFor="summary-edit">
					Summary
					<Textarea
						id="summary-edit"
						className="min-h-28"
						value={editor.text}
						readOnly={busy}
						onChange={(event) =>
							onChange({ ...editor, text: event.target.value, dirty: true })
						}
						required
					/>
				</label>
			) : (
				<>
					<label
						className="block space-y-1 text-sm"
						htmlFor="finding-condition"
					>
						Condition
						<Textarea
							id="finding-condition"
							value={editor.condition}
							readOnly={busy}
							required
							onChange={(event) =>
								onChange({
									...editor,
									condition: event.target.value,
									dirty: true,
								})
							}
						/>
					</label>
					<label
						className="block space-y-1 text-sm"
						htmlFor="finding-consequence"
					>
						Consequence
						<Textarea
							id="finding-consequence"
							value={editor.consequence}
							readOnly={busy}
							required
							onChange={(event) =>
								onChange({
									...editor,
									consequence: event.target.value,
									dirty: true,
								})
							}
						/>
					</label>
				</>
			)}
			<div className="grid gap-2 sm:flex">
				<Button type="submit" disabled={frozen || busy}>
					Save changes
				</Button>
				<Button
					type="button"
					variant="outline"
					disabled={busy}
					onClick={onDiscard}
				>
					Discard editor
				</Button>
			</div>
		</form>
	);
}
