#!/usr/bin/env python3
"""
PoC: Git Diff Change Extractor
Extracts changed files + diff details between two git refs and writes:
  - diff_report.md   (human-readable report: files, stats, per-file hunks)
  - changed_files.csv (machine-readable: file, status, additions, deletions)

Usage:
  python3 diff_report.py                      # last commit (HEAD~1..HEAD)
  python3 diff_report.py BASE HEAD            # e.g. origin/main HEAD
  python3 diff_report.py BASE HEAD --repo /path/to/repo
"""
import argparse
import csv
import subprocess
import sys
from datetime import datetime, timezone

STATUS_MAP = {
    "A": "added", "M": "modified", "D": "deleted",
    "R": "renamed", "C": "copied", "T": "type-changed",
}


EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's well-known empty tree


def run_git(repo, *args):
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout


def ref_exists(repo, ref):
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        capture_output=True, text=True,
    ).returncode == 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("base", nargs="?", default="HEAD~1")
    p.add_argument("head", nargs="?", default="HEAD")
    p.add_argument("--repo", default=".")
    p.add_argument("--out-md", default="diff_report.md")
    p.add_argument("--out-csv", default="changed_files.csv")
    p.add_argument("--out-lines-csv", default="changed_lines.csv")
    p.add_argument("--max-patch-lines", type=int, default=400,
                   help="truncate per-file patch in the report after N lines")
    args = p.parse_args()

    if not ref_exists(args.repo, args.head):
        sys.exit(f"Head ref '{args.head}' not found. Are you inside a git repo "
                 f"with at least one commit? Try: git log --oneline")

    base_is_commit = ref_exists(args.repo, args.base)
    if not base_is_commit:
        print(f"NOTE: base ref '{args.base}' not found (single-commit repo?). "
              f"Diffing against the empty tree — all files will show as added.")
        args.base = EMPTY_TREE
    rng = f"{args.base}..{args.head}"

    # --- 1. changed files with status (A/M/D/R...) ---
    name_status = run_git(args.repo, "diff", "--name-status", "-M", rng).strip()
    files = []  # (status_code, path, old_path_or_None)
    for line in name_status.splitlines():
        parts = line.split("\t")
        code = parts[0][0]  # R100 -> R
        if code in ("R", "C"):
            files.append((code, parts[2], parts[1]))
        else:
            files.append((code, parts[1], None))

    # --- 2. per-file additions/deletions ---
    numstat = run_git(args.repo, "diff", "--numstat", "-M", rng).strip()
    stats = {}
    for line in numstat.splitlines():
        add, rm, path = line.split("\t", 2)
        if "=>" in path:  # rename notation "old => new" / "dir/{a => b}/f"
            path = path.split("=>")[-1].strip().rstrip("}").strip()
            # reconstruct full new path for the "{a => b}" form
        stats[path.split("{")[0] + path.split("}")[-1] if "{" in path else path] = (
            0 if add == "-" else int(add),
            0 if rm == "-" else int(rm),
        )

    def stat_for(path):
        if path in stats:
            return stats[path]
        for k, v in stats.items():  # fallback fuzzy match for renames
            if k.endswith(path) or path.endswith(k):
                return v
        return (0, 0)

    # --- 3. commit metadata ---
    if base_is_commit:
        commits = run_git(args.repo, "log", "--oneline", rng).strip()
    else:
        commits = run_git(args.repo, "log", "--oneline", args.head).strip()
    head_info = run_git(args.repo, "log", "-1", "--format=%h|%an|%ad|%s",
                        "--date=iso", args.head).strip()
    h_sha, h_author, h_date, h_msg = (head_info.split("|", 3) + ["", "", "", ""])[:4]

    # --- 4. CSV output ---
    with open(args.out_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "status", "additions", "deletions", "renamed_from",
                    "base", "head", "head_sha", "author", "generated_at_utc"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for code, path, old in files:
            add, rm = stat_for(path)
            w.writerow([path, STATUS_MAP.get(code, code), add, rm, old or "",
                        args.base, args.head, h_sha, h_author, now])

    # --- 4b. line-level CSV: every added/removed line with its exact content ---
    raw = run_git(args.repo, "diff", "--unified=0", "-M", rng)
    line_rows = []
    cur_file, old_file, old_ln, new_ln = None, None, 0, 0
    for ln in raw.splitlines():
        if ln.startswith("+++ "):
            nf = ln[4:].strip()
            cur_file = old_file if nf == "/dev/null" else nf.removeprefix("b/")
        elif ln.startswith("--- "):
            of = ln[4:].strip()
            old_file = None if of == "/dev/null" else of.removeprefix("a/")
        elif ln.startswith("@@"):
            # @@ -old_start[,count] +new_start[,count] @@
            try:
                parts = ln.split()
                old_ln = int(parts[1].split(",")[0].lstrip("-"))
                new_ln = int(parts[2].split(",")[0].lstrip("+"))
            except (IndexError, ValueError):
                old_ln = new_ln = 0
        elif ln.startswith("+") and not ln.startswith("+++"):
            line_rows.append((cur_file, "added", new_ln, ln[1:]))
            new_ln += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            line_rows.append((cur_file, "removed", old_ln, ln[1:]))
            old_ln += 1

    with open(args.out_lines_csv, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "change", "line_number", "content",
                    "head_sha", "author"])
        for file_, chg, num, content in line_rows:
            w.writerow([file_, chg, num, content, h_sha, h_author])

    # --- 5. Markdown report ---
    total_add = sum(stat_for(pth)[0] for _, pth, _ in files)
    total_rm = sum(stat_for(pth)[1] for _, pth, _ in files)
    lines = [
        "# Diff Change Report",
        "",
        f"**Range:** `{rng}`  |  **Head:** `{h_sha}` — {h_msg}",
        f"**Author:** {h_author}  |  **Date:** {h_date}",
        f"**Files changed:** {len(files)}  |  **+{total_add} / -{total_rm} lines**",
        "",
        "## Commits in range",
        "```",
        commits or "(none)",
        "```",
        "",
        "## Changed files",
        "",
        "| # | File | Status | + | - |",
        "|---|------|--------|---|---|",
    ]
    for i, (code, path, old) in enumerate(files, 1):
        add, rm = stat_for(path)
        label = STATUS_MAP.get(code, code)
        if old:
            label += f" (from `{old}`)"
        lines.append(f"| {i} | `{path}` | {label} | {add} | {rm} |")

    lines += ["", "## Per-file changes", ""]
    for code, path, old in files:
        lines.append(f"### `{path}` — {STATUS_MAP.get(code, code)}")
        patch = run_git(args.repo, "diff", "-M", rng, "--", old or path, path
                        ) if old else run_git(args.repo, "diff", rng, "--", path)
        plines = patch.splitlines()
        if len(plines) > args.max_patch_lines:
            plines = plines[:args.max_patch_lines] + [
                f"... (truncated, {len(plines) - args.max_patch_lines} more lines)"]
        lines += ["```diff", *plines, "```", ""]

    with open(args.out_md, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"OK: {len(files)} changed file(s), +{total_add}/-{total_rm}")
    print(f"Wrote {args.out_md}, {args.out_csv} and {args.out_lines_csv} "
          f"({len(line_rows)} changed line(s))")


if __name__ == "__main__":
    main()