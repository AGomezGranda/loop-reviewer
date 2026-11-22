# Publish an edited draft under the developer’s chosen GitHub identity

Starting a review authorizes preparing a private draft. The developer can edit the summary, edit or drop individual findings, copy the result, and finish locally. Publication requires a separate choice because the feedback and any approval appear under the developer’s name. Both interfaces show the final text, verdict to be submitted, GitHub host and account before submission.

The coordinator publishes the selected draft as one GitHub review through the developer’s authenticated `gh` installation. The action names the effect and account, for example “Publish 2 comments as @alex” or “Approve as @alex.” The reviewer’s verdict is a recommendation. The developer chooses whether to comment or approve. For their own PR, they can publish a comment or finish locally. An approval on their own PR stays local.

Publication consent covers the exact draft, action, reviewed revision, host and account shown. The coordinator binds the write and any retry to that identity. If the account is unavailable, keep the draft and identify the account needed. Editing the approved text or changing the action or identity requires a fresh publication decision.

Setup shows the active GitHub host and account alongside reviewer readiness. It identifies the next step when the developer needs to install `gh`, sign in or configure the reviewer. Discovery and inbox actions stay available when the reviewer is missing. Both interfaces show the setup step wherever starting a review is unavailable.
