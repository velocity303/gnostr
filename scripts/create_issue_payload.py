#!/usr/bin/env python3
import json
import os
from pathlib import Path


def main():
    repo_path = os.environ.get("REPO_PATH", "")
    branch = os.environ.get("BRANCH", "main")
    commit_sha = os.environ.get("COMMIT_SHA", "unknown")

    test_report = Path("test_report.log").read_text()

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

---
*This issue was automatically created by the CI pipeline.*
"""

    # Remove labels to avoid API issues
    payload = {
        "title": f"CI Failure: Repair needed for {branch}",
        "body": body,
    }

    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
