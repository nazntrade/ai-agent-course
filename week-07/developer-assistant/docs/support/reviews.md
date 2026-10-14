# GitHub review and limits

PR review uses a connected repository, a specific PR head and an explicitly
selected AI provider. Sync imports existing CI evidence; it does not necessarily
make a new model request. Execution completed is different from quality accepted.

## GitHub API errors

An HTTP 403 alone does not prove a bad token or missing repository permission.
An explicit rate_limit response with remaining zero indicates an exhausted API
limit. Wait until the response's reset time, reduce repeated polling or requests,
and check the next safe response. Do not rotate credentials as the sole fix for
a proven rate limit. Without that evidence, obtain the safe error code and
repository access information rather than inventing an account problem.
Private repositories may require configured authorized access; credentials
must be entered privately, never pasted into the ticket.

## Existing review history

Old results remain historical when the PR head changes. A Sync button retrieves
saved CI results; a manual review explicitly starts new inference. D32's GitHub
pipeline reviews code changes. A D33 support question is sent to the separate
Support service, not GitHub Actions. GitHub stores the product code only.
