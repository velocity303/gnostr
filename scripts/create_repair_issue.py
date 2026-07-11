#!/usr/bin/env python3
"""Create a Gitea issue for CI repair work."""

import json
import os
import sys
from pathlib import Path


def create_repair_issue(repo_path, branch, commit_sha, test_report):
    """Create an issue in the Gitea repository with repair details."""

    # Get repository metadata
    repo_parts = repo_path.split("/")
    if len(repo_parts) < 2:
        print(f"Invalid repo path: {repo_path}")
        return False

    owner = repo_parts[0]
    repo = repo_parts[1]

    # Read Gitea credentials from environment
    gitea_token = os.environ.get("GITEA_TOKEN")
    gitea_host = os.environ.get("GITEA_HOST", "192.168.5.134:2222")

    if not gitea_token:
        print("GITEA_TOKEN not set")
        return False

    # Construct issue content
    issue_title = f"CI Failure: Repair needed for {branch}"
    issue_body = f"""
## CI Repair Request

**Repository:** {repo_path}
**Branch:** {branch}
**Commit:** {commit_sha}

### Test Report
```
{test_report}
```

### Instructions for Repair Agent
1. Analyze the test failures in the report above
2. Identify the root cause
3. Create a fix that addresses the failing tests
4. Submit a PR to branch `{branch}` with the fix
5. Ensure all tests pass before submitting

### Labels to Add
- needs-repair
- ci-failure

---
*This issue was automatically created by the CI pipeline.*
"""

    # Gitea API endpoint
    api_url = f"https://{gitea_token}@{gitea_host}/api/v1/repos/{owner}/{repo}/issues"

    # Create issue payload
    payload = {
        "title": issue_title,
        "body": issue_body,
        "labels": ["needs-repair", "ci-failure"],
        "assignee": "hermes",  # Or whatever the agent username is
    }

    # Note: This is a simplified version. In reality, you'd use requests library
    # and handle authentication properly.
    print(f"Would create issue: {issue_title}")
    print(f"Payload: {json.dumps(payload, indent=2)}")

    return True


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print(
            "Usage: create_repair_issue.py <repo_path> <branch> <commit_sha> <test_report>"
        )
        sys.exit(1)

    repo_path = sys.argv[1]
    branch = sys.argv[2]
    commit_sha = sys.argv[3]
    test_report = sys.argv[4]

    create_repair_issue(repo_path, branch, commit_sha, test_report)
