#!/usr/bin/env python3
"""Create a Gitea issue with proper JSON escaping."""

import json
import os
import sys
from pathlib import Path


def main():
    # Get environment variables
    repo_path = os.environ.get("REPO_PATH", "VelocityNet/gnostr")
    branch = os.environ.get("BRANCH", "main")
    commit_sha = os.environ.get("COMMIT_SHA", "unknown")

    # Read test report from file
    test_report_file = Path("test_report.log")
    if test_report_file.exists():
        test_report = test_report_file.read_text()
    else:
        test_report = "No test report available"

    # Build the body with proper escaping
    body = f"""## CI Repair Request

Repository: {repo_path}
Branch: {branch}
Commit: {commit_sha}

### Test Report
```
{test_report}
```

### Instructions for Repair Agent
1. Analyze the test failures in the report above
2. Identify the root cause
3. Create a fix that addresses the failing tests
4. Submit a PR to branch {branch} with the fix
5. Ensure all tests pass before submitting

### Labels to Add
- needs-repair
- ci-failure

---
*This issue was automatically created by the CI pipeline.*
"""

    # Build the payload
    payload = {
        "title": f"CI Failure: Repair needed for {branch}",
        "body": body,
        "labels": ["needs-repair", "ci-failure"],
    }

    # Output JSON
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
