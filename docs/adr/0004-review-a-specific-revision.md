# Hand the reviewer a specific PR revision

The coordinator records the repository, PR number, target branch, base tip, merge base and head commit at selection. It passes these to the reviewer port. The adapter obtains the checkout or inputs required to examine the diff from that merge base to that head. This keeps the engine replaceable and gives the developer a precise account of what was examined.

The port returns a recommended verdict, a Markdown summary and optional findings. Present the highest-impact finding first. Each finding describes the triggering condition and consequence and links to the reviewed diff where possible. The publisher places retained findings inline where their locations are usable and includes the remainder in the final summary. Dropped findings must also be absent from the publication preview and submission.

If the target branch, merge base or head changes during review, keep the earlier draft local, explain what changed and offer to review the current revision. Recheck all three immediately before publication and retry. A base tip advancing on the same branch with an unchanged merge base and head leaves the reviewed comparison valid. Submit the reviewed head explicitly as the GitHub review’s commit so a concurrent push cannot silently attach feedback to a different revision.
