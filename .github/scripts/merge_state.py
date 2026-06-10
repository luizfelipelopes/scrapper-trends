"""Union locally-generated state files into the freshly-pulled ones.

`state/<niche>.json` is an append-only cache of published source hrefs. The CI
job commits it back to the repo, but several niche workflows push to the same
branch concurrently, so a plain `git rebase` hits unresolvable add/add
conflicts. Doing the merge at the data level instead — union the `published`
lists — is conflict-free and order-independent.

Usage: python merge_state.py <ours_dir> <dest_dir>
  ours_dir: snapshot of the state files this run produced
  dest_dir: the working-tree state/ dir, already reset to the latest remote tip

Keep STATE_HISTORY_LIMIT in sync with scrapper_base.STATE_HISTORY_LIMIT.
"""
import json
import sys
from pathlib import Path

STATE_HISTORY_LIMIT = 300


def _published(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("published", [])
    except (json.JSONDecodeError, OSError):
        return []


def merge(ours_dir: str, dest_dir: str) -> None:
    ours, dest = Path(ours_dir), Path(dest_dir)
    dest.mkdir(exist_ok=True)

    for ours_file in sorted(ours.glob("*.json")):
        dest_file = dest / ours_file.name
        base = _published(dest_file)            # latest from remote
        seen = set(base)
        merged = base + [h for h in _published(ours_file) if h not in seen]
        dest_file.write_text(
            json.dumps({"published": merged[-STATE_HISTORY_LIMIT:]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: merge_state.py <ours_dir> <dest_dir>")
    merge(sys.argv[1], sys.argv[2])
