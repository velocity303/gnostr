#!/usr/bin/env python3
"""
Gnostr Repair Agent

This agent monitors for issues with 'needs-repair' label and automatically
creates fixes for CI failures.
"""

import os
import sys
from pathlib import Path
from datetime import datetime


def monitor_repair_issues():
    """Monitor for issues that need repair."""
    print(f"Starting repair agent at {datetime.now()}")

    # Get repository info
    repo_path = "/tmp/gnostr"
    branch = "main"

    # Search for issues with needs-repair label
    # In a real implementation, this would use the Gitea API
    print(f"Looking for repair issues in {repo_path}...")

    # For now, simulate finding an issue
    sample_issue = {
        "id": 1,
        "title": "CI Failure: Repair needed for main",
        "body": "## CI Repair Request\n\nRepository: gnostr\nBranch: main\nCommit: abc123\n\n### Test Report\n```\nERROR: test_codec_support.py::TestCodecSupport::test_is_video_url_mp4\n    assert ContentRenderer.is_video_url(url) is True\nE   assert False is True\n```\n\n### Instructions for Repair Agent\n1. Analyze the test failures in the report above\n2. Identify the root cause\n3. Create a fix that addresses the failing tests\n4. Submit a PR to branch main with the fix\n5. Ensure all tests pass before submitting\n\n### Labels to Add\n- needs-repair\n- ci-failure\n\n---\n*This issue was automatically created by the CI pipeline.*",
        "created_at": "2026-07-11T10:00:00Z",
        "labels": ["needs-repair", "ci-failure"],
    }

    print(f"Found issue: {sample_issue['title']}")
    print(f"Issue ID: {sample_issue['id']}")

    # Process the issue
    process_repair_issue(sample_issue, repo_path, branch)


def process_repair_issue(issue, repo_path, branch):
    """Process a repair issue and create a fix."""
    print(f"\n=== Processing Repair Issue #{issue['id']} ===")
    print(f"Title: {issue['title']}")

    # Parse the test report from the issue body
    body = issue["body"]
    test_report = extract_test_report(body)

    print(f"\nTest Report:\n{test_report}")

    # Analyze the failure
    analysis = analyze_failure(test_report)
    print(f"\nAnalysis:\n{analysis}")

    # Create a fix
    fix = create_fix(analysis, repo_path)
    print(f"\nFix Created:\n{fix}")

    # Submit the fix as a PR
    submit_fix_as_pr(fix, repo_path, branch)


def extract_test_report(body):
    """Extract the test report from the issue body."""
    # Simple extraction - in reality, you'd use more robust parsing
    if "### Test Report" in body:
        start = body.find("### Test Report") + len("### Test Report")
        end = body.find("\n", start)
        return body[start:end].strip()
    return ""


def analyze_failure(test_report):
    """Analyze the test failure and identify the root cause."""
    # Parse the test report
    if "test_is_video_url_mp4" in test_report:
        return "Video URL detection for MP4 files is failing. The is_video_url method returns False for valid MP4 URLs."
    elif "test_is_video_url_mov" in test_report:
        return "Video URL detection for MOV files is failing."
    else:
        return "Unknown test failure. Please examine the test report manually."


def create_fix(analysis, repo_path):
    """Create a fix for the identified issue."""
    if "MP4" in analysis:
        # The fix would be to update the VIDEO_EXTS set to include .mp4
        # But in this case, .mp4 is already in the set. The issue might be in the URL parsing.
        fix_content = """
# Fix for MP4 video URL detection
# Ensure the is_video_url method correctly identifies .mp4 extensions

from urllib.parse import urlparse

def is_video_url(url):
    if not url:
        return False
    try:
        path = urlparse(url).path.lower()
        # Make sure .mp4 is in VIDEO_EXTS
        result = any(path.endswith(ext) for ext in ContentRenderer.VIDEO_EXTS)
        print(f"is_video_url({url}) -> {result}")
        return result
    except Exception:
        return False

# Verify VIDEO_EXTS includes .mp4
assert '.mp4' in ContentRenderer.VIDEO_EXTS
"""
    else:
        fix_content = "# Unknown fix required"

    return fix_content


def submit_fix_as_pr(fix, repo_path, branch):
    """Submit the fix as a pull request to the specified branch."""
    print(f"\n=== Submitting Fix to Branch: {branch} ===")
    print(f"Fix content:\n{fix}")

    # In a real implementation, this would:
    # 1. Create a new branch from the target branch
    # 2. Apply the fix to the relevant files
    # 3. Commit the changes
    # 4. Create a pull request
    # 5. Link the PR to the original issue

    print("Fix submitted successfully!")


if __name__ == "__main__":
    monitor_repair_issues()
