"""CLI interface for local testing without Slack."""

import argparse
import logging
import sys
import tempfile
from pathlib import Path

from .agent_runner import AgentRunner, format_check_results
from .config import load_config
from .github_ops import GitHubClient, GitOperations
from .security import SecurityManager


def setup_logging(level: str = "INFO") -> None:
    """Set up logging.

    Args:
        level: Logging level
    """
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def main() -> None:
    """CLI main entry point."""
    parser = argparse.ArgumentParser(
        description="Slack Coding Agent CLI - Local testing mode"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Path to local repository or owner/repo for GitHub",
    )
    parser.add_argument(
        "--task",
        required=True,
        help="Task description",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only generate plan, don't implement",
    )
    parser.add_argument(
        "--output-dir",
        help="Output directory for results (default: temporary directory)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Set up logging
    setup_logging("DEBUG" if args.verbose else "INFO")
    logger = logging.getLogger(__name__)

    # Load configuration
    try:
        config = load_config(args.config)
        logger.info(f"Loaded configuration from {args.config}")
    except Exception as e:
        print(f"❌ Failed to load configuration: {e}", file=sys.stderr)
        sys.exit(1)

    # Initialize components
    security_manager = SecurityManager(config.security)
    agent_runner = AgentRunner(
        config.agent,
        config.docker,
        security_manager,
        config.gemini.api_key,
    )

    # Determine if repo is local path or GitHub repo
    repo_path = Path(args.repo)
    is_local = repo_path.exists() and repo_path.is_dir()

    # Set up working directory
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        workdir = output_dir / "repo"
    else:
        output_dir = None
        workdir = None

    try:
        if is_local:
            logger.info(f"Using local repository: {repo_path}")
            workdir = repo_path
        else:
            # GitHub repository
            logger.info(f"Cloning GitHub repository: {args.repo}")

            if "/" not in args.repo:
                print("❌ GitHub repo must be in format: owner/repo", file=sys.stderr)
                sys.exit(1)

            owner, repo = args.repo.split("/", 1)

            # Use temporary directory if no output dir specified
            if not output_dir:
                temp_context = tempfile.TemporaryDirectory()
                workdir = Path(temp_context.name) / "repo"
            else:
                temp_context = None

            try:
                github_client = GitHubClient(config.github)
                clone_url = github_client.get_clone_url(owner, repo)
                GitOperations.clone_repository(clone_url, workdir)
            except Exception as e:
                print(f"❌ Failed to clone repository: {e}", file=sys.stderr)
                if temp_context:
                    temp_context.cleanup()
                sys.exit(1)

        # Run planning phase
        print("\n🔍 Planning phase...")
        print("=" * 60)

        try:
            plan = agent_runner.run_planning_phase(workdir, args.task)
            print("\n📋 Implementation Plan:")
            print(plan)

            if output_dir:
                plan_file = output_dir / "plan.txt"
                plan_file.write_text(plan)
                print(f"\n💾 Plan saved to: {plan_file}")

        except Exception as e:
            print(f"\n❌ Planning failed: {e}", file=sys.stderr)
            sys.exit(1)

        if args.plan_only:
            print("\n✅ Planning complete (plan-only mode)")
            return

        # Ask for approval
        print("\n" + "=" * 60)
        approval = input("\n👍 Approve plan and continue with implementation? [y/N]: ")

        if approval.lower() != "y":
            print("❌ Implementation cancelled")
            sys.exit(0)

        # Run implementation phase
        print("\n⚙️  Implementation phase...")
        print("=" * 60)

        try:
            # Create a branch for changes
            branch_name = f"cli-task-{abs(hash(args.task)) % 10000}"
            GitOperations.create_branch(workdir, branch_name)
            print(f"📝 Created branch: {branch_name}")

            # Run implementation
            summary = agent_runner.run_implementation_phase(
                workdir, args.task, plan
            )

            print("\n✨ Implementation Summary:")
            print(summary)

            # Check for changes
            if not GitOperations.has_changes(workdir):
                print("\n⚠️  No changes were made")
                return

            # Get diff
            diff = GitOperations.get_diff(workdir, cached=False)
            print("\n📊 Changes:")
            print(diff[:2000])
            if len(diff) > 2000:
                print("\n... (truncated)")

            # Security scan
            print("\n🔒 Security scan...")
            secret_warning = security_manager.scan_diff_for_secrets(diff)
            if secret_warning:
                print(f"\n⚠️  {secret_warning}")
                proceed = input("\nProceed anyway? [y/N]: ")
                if proceed.lower() != "y":
                    print("❌ Cancelled due to security concerns")
                    sys.exit(1)

            # Run checks
            print("\n🧪 Running checks...")
            check_results = agent_runner.run_checks(workdir)
            print(format_check_results(check_results))

            # Save results
            if output_dir:
                summary_file = output_dir / "summary.txt"
                summary_file.write_text(summary)

                diff_file = output_dir / "diff.patch"
                diff_file.write_text(diff)

                print(f"\n💾 Results saved to: {output_dir}")

            print("\n✅ Implementation complete!")
            print(f"📍 Branch: {branch_name}")

            # Offer to commit
            commit = input("\n💾 Commit changes? [y/N]: ")
            if commit.lower() == "y":
                commit_msg = f"{args.task}\n\nGenerated by Slack Coding Agent CLI"
                GitOperations.commit_changes(workdir, commit_msg)
                print("✅ Changes committed")

        except Exception as e:
            print(f"\n❌ Implementation failed: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            sys.exit(1)

    finally:
        # Cleanup
        if not is_local and not output_dir and 'temp_context' in locals():
            if temp_context:
                temp_context.cleanup()


if __name__ == "__main__":
    main()
